"""Unit tests for the single destructive guard (card S1-4).

The guard is the last fence between a persisted verdict and a destructive step, so its
tests are mutation-resistant by construction: every refusal class is exercised through
the public ``assert_destructive_allowed``, and the "flipping any refusal to allowed must
be caught" requirement is met by asserting the exact refusal class per shape -- a future
edit that turns one class into ``allowed`` fails its named test, not a generic
``assertFalse``.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from ummanu.dispatch.head_vitality_episode import (
    VitalityEpisode,
    VitalityVerdict,
)
from ummanu.dispatch.head_vitality_guard import (
    GuardRefusal,
    assert_destructive_allowed,
)

RUN_ID = "run-current"
OTHER_RUN_ID = "run-elsewhere"
NOW = 2_000_000.0


def episode(
    verdict: VitalityVerdict = VitalityVerdict.CONFIRMED_STALL,
    *,
    run_id: str = RUN_ID,
    started_at: float = NOW - 10_000.0,
    evidence: dict[str, str] | None = None,
    last_progress_at: float = 0.0,
    unavailable: dict[str, float] | None = None,
) -> VitalityEpisode:
    """One persisted episode shaped like the reducer writes it."""
    return VitalityEpisode(
        run_id=run_id,
        verdict=verdict,
        started_at=started_at,
        suspected_since=0.0,
        confirmed_since=started_at if verdict is VitalityVerdict.CONFIRMED_STALL else 0.0,
        last_progress_at=last_progress_at,
        last_progress_source="",
        evidence_cursors=evidence or {},
        unavailable_since=unavailable or {},
        basis=("quiet:9600s@provider_cursor",),
        reason="",
        recovery_rung=0,
        activity_epoch=0,
        updated_at=NOW - 1.0,
        stall_frozen_since=0.0,
    )


class AllowedCasesTests(unittest.TestCase):
    """The two verdicts that may destroy, and the shapes that keep them allowed."""

    def test_a_witnessed_confirmed_stall_is_allowed(self) -> None:
        decision = assert_destructive_allowed(
            episode(evidence={"provider_cursor": "fake:unchanged"}),
            "worker-respawn",
            NOW,
        )
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.refusal)
        self.assertEqual(decision.verdict, "confirmed_stall")
        self.assertEqual(decision.episode_run_id, RUN_ID)

    def test_a_dead_verdict_is_allowed(self) -> None:
        decision = assert_destructive_allowed(episode(VitalityVerdict.DEAD), "worker-reclaim", NOW)
        self.assertTrue(decision.allowed)

    def test_a_pid_only_confirmation_past_the_outer_ceiling_is_allowed(self) -> None:
        # No evidence cursor and no progress stamp: the pid-only arm earned this one.
        decision = assert_destructive_allowed(
            episode(),
            "worker-respawn",
            NOW,
            pid_only_outer_ceiling_seconds=5_400.0,
        )
        self.assertTrue(decision.allowed, decision.reason)


class RefusalClassesTests(unittest.TestCase):
    """Every refusal class, each asserted by name so a flip to allowed is caught."""

    def test_a_missing_episode_is_refused(self) -> None:
        for shape in (None, {}, "healthy_quiet", 42):
            decision = assert_destructive_allowed(shape, "worker-respawn", NOW)
            self.assertFalse(decision.allowed)
            self.assertIs(decision.refusal, GuardRefusal.MISSING_EPISODE, repr(shape))

    def test_an_unreadable_clock_is_refused_as_missing_evidence(self) -> None:
        for bad_now in (None, float("nan"), float("inf"), "soon", True):
            decision = assert_destructive_allowed(episode(), "worker-respawn", bad_now)
            self.assertFalse(decision.allowed)
            self.assertIs(decision.refusal, GuardRefusal.MISSING_EPISODE, repr(bad_now))

    def test_an_episode_of_another_run_is_refused(self) -> None:
        decision = assert_destructive_allowed(
            episode(),
            "worker-respawn",
            NOW,
            current_run_id=OTHER_RUN_ID,
        )
        self.assertIs(decision.refusal, GuardRefusal.FOREIGN_RUN)
        self.assertIn(RUN_ID, decision.reason)
        self.assertIn(OTHER_RUN_ID, decision.reason)

    def test_healthy_active_is_refused(self) -> None:
        decision = assert_destructive_allowed(
            episode(VitalityVerdict.HEALTHY_ACTIVE),
            "worker-respawn",
            NOW,
        )
        self.assertIs(decision.refusal, GuardRefusal.HEALTHY_ACTIVE)

    def test_healthy_quiet_is_refused(self) -> None:
        decision = assert_destructive_allowed(
            episode(VitalityVerdict.HEALTHY_QUIET),
            "worker-respawn",
            NOW,
        )
        self.assertIs(decision.refusal, GuardRefusal.HEALTHY_QUIET)

    def test_unverifiable_is_refused(self) -> None:
        decision = assert_destructive_allowed(
            episode(VitalityVerdict.UNVERIFIABLE),
            "worker-respawn",
            NOW,
        )
        self.assertIs(decision.refusal, GuardRefusal.UNVERIFIABLE)

    def test_suspended_is_refused(self) -> None:
        decision = assert_destructive_allowed(
            episode(VitalityVerdict.SUSPENDED),
            "worker-respawn",
            NOW,
        )
        self.assertIs(decision.refusal, GuardRefusal.SUSPENDED)
        self.assertIn("SIGCONT", decision.reason)

    def test_a_retained_head_is_refused(self) -> None:
        """secretary-1539: a process the dispatcher parked itself is not a head to recover."""
        decision = assert_destructive_allowed(
            episode(VitalityVerdict.RETAINED),
            "worker-respawn",
            NOW,
        )
        self.assertIs(decision.refusal, GuardRefusal.RETAINED)
        self.assertIn("retention", decision.reason)

    def test_a_suspected_stall_never_reaches_destruction(self) -> None:
        decision = assert_destructive_allowed(
            episode(VitalityVerdict.SUSPECTED_STALL),
            "worker-respawn",
            NOW,
        )
        self.assertIs(decision.refusal, GuardRefusal.SUSPECTED_STALL)

    def test_an_unknown_future_verdict_is_refused_on_principle(self) -> None:
        # Simulate a rung a future card adds without teaching the guard.
        stranger = replace(episode(), verdict=VitalityVerdict.SUSPECTED_STALL)
        object.__setattr__(stranger, "verdict", "quantum_stall")
        decision = assert_destructive_allowed(stranger, "worker-respawn", NOW)
        self.assertFalse(decision.allowed)
        self.assertIn("not one the guard knows", decision.reason)

    def test_a_pid_only_confirmation_before_the_outer_ceiling_is_refused(self) -> None:
        # started 5400s ago, ceiling 9000s: the pid-only arm confirmed, the ceiling has
        # not elapsed, so the guard holds the line the old clock would have crossed.
        young = episode(started_at=NOW - 5_400.0)
        decision = assert_destructive_allowed(
            young,
            "worker-respawn",
            NOW,
            pid_only_outer_ceiling_seconds=9_000.0,
        )
        self.assertIs(decision.refusal, GuardRefusal.PID_ONLY_CEILING_UNELAPSED)

    def test_a_witnessed_confirmation_is_not_held_by_the_pid_only_rule(self) -> None:
        # The provider answered and then went silent: two-channel evidence, no ceiling hold.
        witnessed = episode(
            started_at=NOW - 5_400.0,
            evidence={"provider_cursor": "fake:unchanged"},
        )
        decision = assert_destructive_allowed(
            witnessed,
            "worker-respawn",
            NOW,
            pid_only_outer_ceiling_seconds=9_000.0,
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_child_and_journal_cursors_do_not_bypass_the_outer_ceiling(self) -> None:
        for source in ("execution_child", "supervisor_journal"):
            with self.subTest(source=source):
                decision = assert_destructive_allowed(
                    episode(started_at=NOW - 5_400.0, evidence={source: "cursor"}),
                    "worker-respawn", NOW, pid_only_outer_ceiling_seconds=9_000.0,
                )
                self.assertIs(decision.refusal, GuardRefusal.PID_ONLY_CEILING_UNELAPSED)

    def test_a_confirmation_earned_while_the_provider_is_dark_is_held_too(self) -> None:
        """secretary-1543: the source answered once, then went dark, then the episode aged.

        The reducer's dark ceiling means such an episode can now reach ConfirmedStall on the
        pid alone. The evidence behind the destructive step is therefore one channel again, so
        the outer ceiling holds it exactly as it holds the never-witnessed shape.
        """
        went_dark = episode(
            started_at=NOW - 5_400.0,
            evidence={"provider_cursor": "fake:unchanged"},
            unavailable={"provider_cursor": NOW - 3_000.0},
        )
        decision = assert_destructive_allowed(
            went_dark,
            "worker-respawn",
            NOW,
            pid_only_outer_ceiling_seconds=9_000.0,
        )
        self.assertIs(decision.refusal, GuardRefusal.PID_ONLY_CEILING_UNELAPSED)
        self.assertIn("provider_cursor dark", decision.reason)

        # Past the outer ceiling the same episode is allowed: the hold is a delay, not a veto.
        old_enough = assert_destructive_allowed(
            went_dark,
            "worker-respawn",
            NOW,
            pid_only_outer_ceiling_seconds=1_000.0,
        )
        self.assertTrue(old_enough.allowed, old_enough.reason)

    def test_a_progress_stamp_counts_as_witnessing_even_without_a_cursor(self) -> None:
        stamped = episode(started_at=NOW - 5_400.0, last_progress_at=NOW - 9_000.0)
        decision = assert_destructive_allowed(
            stamped,
            "worker-respawn",
            NOW,
            pid_only_outer_ceiling_seconds=9_000.0,
        )
        self.assertTrue(decision.allowed, decision.reason)


class MutationResistanceTests(unittest.TestCase):
    """The tests a careless edit must fail: every refusal maps to its named assertion."""

    def test_every_refusal_class_is_exercised(self) -> None:
        exercised = {
            GuardRefusal.MISSING_EPISODE,
            GuardRefusal.FOREIGN_RUN,
            GuardRefusal.HEALTHY_ACTIVE,
            GuardRefusal.HEALTHY_QUIET,
            GuardRefusal.SUSPENDED,
            GuardRefusal.RETAINED,
            GuardRefusal.UNVERIFIABLE,
            GuardRefusal.SUSPECTED_STALL,
            GuardRefusal.PID_ONLY_CEILING_UNELAPSED,
        }
        # A refusal class nobody tests is a refusal nobody can trust: keep the map and
        # the tests in lockstep.
        self.assertEqual(exercised, set(GuardRefusal))

    def test_the_refused_map_covers_every_non_destructive_verdict(self) -> None:
        from ummanu.dispatch.head_vitality_guard import _REFUSED_VERDICTS

        destructive = {VitalityVerdict.CONFIRMED_STALL, VitalityVerdict.DEAD}
        self.assertEqual(
            set(_REFUSED_VERDICTS),
            set(VitalityVerdict) - destructive,
            "every non-destructive verdict must be named in the refusal map explicitly",
        )

    def test_the_decision_travels_as_bounded_telemetry(self) -> None:
        decision = assert_destructive_allowed(
            episode(VitalityVerdict.HEALTHY_QUIET),
            "worker-respawn",
            NOW,
        )
        payload = decision.to_json()
        self.assertEqual(
            payload,
            {
                "action": "worker-respawn",
                "allowed": False,
                "refusal": "healthy-quiet",
                "reason": decision.reason,
                "verdict": "healthy_quiet",
                "episode_run_id": RUN_ID,
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
