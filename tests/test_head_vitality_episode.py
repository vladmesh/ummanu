"""VitalityEpisode reducer: the plan's invariants a-h, each with a named test.

The reducer is pure, so every test drives it directly with hand-built snapshots and an explicit
``now`` -- the same way the shadow wiring feeds it, minus the I/O.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace as dataclass_replace

from ummanu.dispatch.head_vitality import (
    HeadVitalityError,
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    TurnState,
    VitalitySnapshot,
)
from ummanu.dispatch.head_vitality_episode import (
    DEFAULT_VITALITY_THRESHOLDS,
    EPISODE_VERSION,
    VitalityEpisode,
    VitalityThresholds,
    VitalityVerdict,
    reduce_vitality,
)

RUN_ID = "run-1"
THRESHOLDS = VitalityThresholds(suspect_after=300.0, confirm_after=600.0)


def heartbeat(
    observed_at: float,
    *,
    process: ProcessState = ProcessState.RUNNING,
    run_id: str = RUN_ID,
    available: bool = True,
) -> VitalitySnapshot:
    """A pid-heartbeat snapshot with only the Process axis filled."""
    if not available:
        return VitalitySnapshot(
            run_id=run_id,
            source=SnapshotSource.PID_HEARTBEAT,
            observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason="pid heartbeat is inconclusive: not-yet-written",
        )
    return VitalitySnapshot(
        run_id=run_id,
        source=SnapshotSource.PID_HEARTBEAT,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        process=process,
    )


def provider(
    observed_at: float,
    *,
    progress: ProgressState = ProgressState.QUIET,
    run_id: str = RUN_ID,
    cursor: str = "12:abc",
    available: bool = True,
) -> VitalitySnapshot:
    """A provider-cursor snapshot with only the Progress axis filled."""
    if not available:
        return VitalitySnapshot(
            run_id=run_id,
            source=SnapshotSource.PROVIDER_CURSOR,
            observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason="provider cursor is not admitted",
        )
    return VitalitySnapshot(
        run_id=run_id,
        source=SnapshotSource.PROVIDER_CURSOR,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        progress=progress,
        cursor=cursor,
    )


def pane(observed_at: float, *, idle: bool = True, run_id: str = RUN_ID) -> VitalitySnapshot:
    """An advisory pane snapshot with only the Turn axis filled."""
    return VitalitySnapshot(
        run_id=run_id,
        source=SnapshotSource.PANE_ADVISORY,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        turn=TurnState.IDLE if idle else TurnState.ACTIVE,
    )


def episode(**overrides: object) -> VitalityEpisode:
    """A baseline episode with the fields a continuation test needs to name."""
    fields: dict[str, object] = {
        "run_id": RUN_ID,
        "started_at": 0.0,
        "verdict": VitalityVerdict.HEALTHY_QUIET,
        "updated_at": 0.0,
    }
    fields.update(overrides)
    return VitalityEpisode(**fields)  # type: ignore[arg-type]


class IdentityTests(unittest.TestCase):
    """Invariant (a): snapshots from another run never touch an episode."""

    def test_a_foreign_run_snapshot_is_dropped_and_named_in_basis(self) -> None:
        """A misrouted reading must not inject its verdict into this run's history."""
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)
        reduced = reduce_vitality(
            previous,
            [
                heartbeat(400.0),
                provider(400.0),
                # Another run's advancement, stale or misrouted into this batch.
                provider(999.0, progress=ProgressState.ADVANCING, run_id="run-other"),
            ],
            now=400.0,
            thresholds=THRESHOLDS,
        )

        # The foreign advancement must not end this episode's suspicion.
        self.assertEqual(reduced.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(reduced.activity_epoch, 0)
        self.assertEqual(reduced.last_progress_at, 0.0)
        self.assertTrue(any("dropped-foreign-run" in token for token in reduced.basis))

    def test_a_wholesale_identity_change_starts_a_fresh_episode(self) -> None:
        previous = episode(verdict=VitalityVerdict.CONFIRMED_STALL, confirmed_since=900.0)
        reduced = reduce_vitality(
            previous,
            [heartbeat(1000.0, run_id="run-respawned")],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        # A respawned head is not the old stall's continuation.
        self.assertEqual(reduced.run_id, "run-respawned")
        self.assertNotEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(reduced.last_progress_at, 0.0)
        self.assertTrue(any("identity-changed" in token for token in reduced.basis))

    def test_a_mixed_batch_keeps_the_episode_run_and_drops_the_rest(self) -> None:
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0, run_id="run-other")],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.run_id, RUN_ID)
        self.assertTrue(any("dropped-foreign-run" in token for token in reduced.basis))


class ProcessAxisTests(unittest.TestCase):
    """Invariant (b): Dead and Suspended outrank the other axes."""

    def test_dead_wins_regardless_of_advancing_progress_and_busy_pane(self) -> None:
        reduced = reduce_vitality(
            None,
            [
                heartbeat(1000.0, process=ProcessState.DEAD),
                provider(1000.0, progress=ProgressState.ADVANCING),
                pane(1000.0, idle=False),
            ],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.DEAD)

    def test_suspended_is_its_own_verdict_and_never_a_stall(self) -> None:
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)
        reduced = reduce_vitality(
            previous,
            [heartbeat(1000.0, process=ProcessState.SUSPENDED), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.SUSPENDED)
        self.assertNotEqual(reduced.verdict, VitalityVerdict.SUSPECTED_STALL)

    def test_a_declared_retention_makes_the_same_stop_signal_its_own_verdict(self) -> None:
        """secretary-1539: the kernel's `T` is one fact with two possible owners.

        `retain_worker` parks a finished worker on purpose while the gate waits for CI. Read as
        `Suspended` that parking is a head to revive, and the watchdog's SIGCONT then wakes the
        session the gate is holding still. The caller -- the only party that knows whose stop
        signal it is -- declares it, and the verdict says so.
        """
        batch = [heartbeat(1000.0, process=ProcessState.SUSPENDED), provider(1000.0)]

        held = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS, retained=True)
        loose = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)

        self.assertEqual(held.verdict, VitalityVerdict.RETAINED)
        self.assertEqual(loose.verdict, VitalityVerdict.SUSPENDED)
        self.assertIn("retained@pid_heartbeat", held.basis)
        self.assertIn("suspended@pid_heartbeat", loose.basis)
        # Both are the same parked process, so both freeze the stall clocks identically; only
        # the recorded reason distinguishes them for an operator reading head-status.
        self.assertEqual(held.stall_frozen_since, loose.stall_frozen_since)
        self.assertIn("retention", held.reason)
        self.assertNotIn("retention", loose.reason)

    def test_a_retention_never_launders_death_or_a_running_process(self) -> None:
        """The suppression is of the wake, not of the truth.

        Death still outranks the retention -- a retained head that is provably gone is `Dead`,
        which is what the confirm-or-replace path needs to hear. And a retention whose process
        is running again is not `Retained` either: it reduces on the ordinary ladder, so the
        caller's own "no longer confirmably suspended" failure is still reached.
        """
        dead = reduce_vitality(
            None,
            [heartbeat(1000.0, process=ProcessState.DEAD), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
            retained=True,
        )
        self.assertEqual(dead.verdict, VitalityVerdict.DEAD)

        running = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0, progress=ProgressState.ADVANCING)],
            now=1000.0,
            thresholds=THRESHOLDS,
            retained=True,
        )
        self.assertEqual(running.verdict, VitalityVerdict.HEALTHY_ACTIVE)

    def test_the_retention_declaration_is_a_boolean_or_it_is_refused(self) -> None:
        with self.assertRaises(HeadVitalityError):
            reduce_vitality(
                None,
                [heartbeat(1000.0, process=ProcessState.SUSPENDED)],
                now=1000.0,
                thresholds=THRESHOLDS,
                retained="yes",  # type: ignore[arg-type]
            )

    def test_suspension_freezes_the_stall_clocks(self) -> None:
        """The secretary-1061 shape: a worker parked in T for 27 minutes must not be read as
        six hours of quiet. The freeze is stamped, and resumed time is shifted past it."""
        previous = episode(last_progress_at=100.0)
        frozen = reduce_vitality(
            previous,
            [heartbeat(120.0, process=ProcessState.SUSPENDED), provider(120.0)],
            now=120.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(frozen.stall_frozen_since, 120.0)

        # The process resumes at 3000.0 -- 2880s of suspended time that feeds no threshold.
        resumed = reduce_vitality(
            frozen,
            [heartbeat(3000.0), provider(3000.0)],
            now=3000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(resumed.stall_frozen_since, 0.0)
        self.assertEqual(resumed.last_progress_at, 100.0 + 2880.0)
        # 3000 - (100+2880) = 20s of real quiet: far below suspicion.
        self.assertEqual(resumed.verdict, VitalityVerdict.HEALTHY_QUIET)


class ProgressTests(unittest.TestCase):
    """Invariant (c): advancement from any strong source ends a stall episode immediately."""

    def test_advancement_ends_a_confirmed_episode_and_resets_the_clocks(self) -> None:
        previous = episode(
            verdict=VitalityVerdict.CONFIRMED_STALL,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=0.0,
        )
        reduced = reduce_vitality(
            previous,
            [provider(1000.0, progress=ProgressState.ADVANCING), heartbeat(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        self.assertEqual(reduced.suspected_since, 0.0)
        self.assertEqual(reduced.confirmed_since, 0.0)
        self.assertEqual(reduced.last_progress_at, 1000.0)
        self.assertEqual(reduced.last_progress_source, SnapshotSource.PROVIDER_CURSOR.value)
        self.assertEqual(reduced.activity_epoch, 1)

    def test_advancement_from_a_first_observation_counts_as_progress_too(self) -> None:
        # The first provider reading carries no comparison, but its cursor is evidence the
        # source answered; only a *later* reduction can call it advancement. Here the pid
        # heartbeat alone is present, so the verdict rests at healthy without inventing quiet.
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(reduced.last_progress_at, 0.0)


class UnavailableTests(unittest.TestCase):
    """Invariant (d): a dark source freezes evidence; all-strong-dark is Unverifiable."""

    def test_an_unavailable_source_does_not_advance_stall_timers_inside_the_ceiling(self) -> None:
        """Inside ``dark_ceiling`` a dark channel still freezes: unavailable is not no-progress.

        Before secretary-1543 this test ran the second tick at now=2000 and asserted
        ``HealthyQuiet`` there -- i.e. that the freeze was unbounded. That is the defect
        ``issue:7bff833fef6d9d9b404d`` froze on, so the window is now bounded and the
        unbounded half of the old assertion moved to
        ``test_a_dark_progress_source_stops_freezing_past_the_ceiling`` below. What the freeze
        is FOR is unchanged and still asserted here: a broken channel never ages a head on its
        own, and the dark stamp is kept so the ceiling can be measured from it.
        """
        previous = episode(last_progress_at=0.0)
        first = reduce_vitality(
            previous,
            [heartbeat(100.0), provider(100.0, available=False)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        # The provider stays dark, and the quiet is already past `suspect_after`.
        second = reduce_vitality(
            first,
            [heartbeat(400.0), provider(400.0, available=False)],
            now=400.0,
            thresholds=THRESHOLDS,
        )

        # The pid heartbeat answers "alive, no progress source" -- which is not quiet evidence,
        # so inside the ceiling the verdict does not age toward a stall.
        self.assertEqual(second.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertIn(SnapshotSource.PROVIDER_CURSOR.value, second.unavailable_since)
        self.assertEqual(second.unavailable_since[SnapshotSource.PROVIDER_CURSOR.value], 100.0)
        self.assertIn("dark:300s@provider_cursor", second.basis)

    def test_a_returning_source_leaves_the_unavailable_map(self) -> None:
        first = reduce_vitality(
            episode(),
            [heartbeat(100.0), provider(100.0, available=False)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        second = reduce_vitality(
            first,
            [heartbeat(200.0), provider(200.0)],
            now=200.0,
            thresholds=THRESHOLDS,
        )

        self.assertNotIn(SnapshotSource.PROVIDER_CURSOR.value, second.unavailable_since)
        self.assertEqual(second.evidence_cursors.get(SnapshotSource.PROVIDER_CURSOR.value), "12:abc")

    def test_all_strong_sources_dark_is_unverifiable(self) -> None:
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0, available=False), pane(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.UNVERIFIABLE)

    def test_a_confirmed_episode_is_not_laundered_by_its_observers_going_blind(self) -> None:
        confirmed = episode(verdict=VitalityVerdict.CONFIRMED_STALL, confirmed_since=900.0)
        reduced = reduce_vitality(
            confirmed,
            [heartbeat(1000.0, available=False), provider(1000.0, available=False)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_a_confirmation_earned_then_blind_keeps_its_phase_onset(self) -> None:
        """Confirmation is stamped the tick it is earned; when the observers then go dark the
        preserved verdict must still carry that onset, so a later policy can audit how long the
        phase has run."""
        previous = episode(last_progress_at=0.0)
        confirmed = reduce_vitality(
            previous,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(confirmed.confirmed_since, 900.0)

        # Every strong source goes dark; the confirmation survives, with its onset.
        blind = reduce_vitality(
            confirmed,
            [heartbeat(2000.0, available=False), provider(2000.0, available=False)],
            now=2000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(blind.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(blind.confirmed_since, 900.0)

    def test_a_confirmation_survives_a_dark_provider_while_the_pid_keeps_answering(self) -> None:
        """The witnessed-freeze arm must never launder an earned confirmation.

        Invariant (g) has no provider-shaped exception: the confirmation was earned on real
        admitted quiet, and a provider going dark afterwards while the pid keeps answering
        says nothing that could end it. The freeze arm must therefore preserve the verdict
        AND its onset -- not demote the phase to HealthyQuiet with dangling stamps (the
        S1-3 review's MAJOR 1), and not keep aging a phase that is already terminal.
        """
        previous = episode(last_progress_at=0.0)
        confirmed = reduce_vitality(
            previous,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(confirmed.confirmed_since, 900.0)
        self.assertEqual(confirmed.suspected_since, 300.0)

        # The provider goes dark mid-episode; the pid keeps answering Running.
        dark = reduce_vitality(
            confirmed,
            [heartbeat(1300.0), provider(1300.0, available=False)],
            now=1300.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(dark.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(dark.confirmed_since, 900.0)
        self.assertEqual(dark.suspected_since, 300.0)
        self.assertIn(
            "preserved-confirmation:provider-dark-pid-alive",
            dark.basis,
        )
        # Much later ticks change nothing: no aging past a terminal phase.
        later = reduce_vitality(
            dark,
            [heartbeat(900_000.0), provider(900_000.0, available=False)],
            now=900_000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(later.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(later.confirmed_since, 900.0)

        # And only the four authorised endings still move it: real progress first.
        resumed = reduce_vitality(
            dark,
            [heartbeat(900_100.0), provider(900_100.0, progress=ProgressState.ADVANCING, cursor="13:def")],
            now=900_100.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(resumed.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        self.assertEqual(resumed.confirmed_since, 0.0)

    def test_a_suspicion_survives_a_dark_provider_while_the_pid_keeps_answering(self) -> None:
        """The freeze arm preserves an earned SuspectedStall instead of demoting it.

        The S1-3 review follow-up: an earned ``SuspectedStall`` whose provider then went
        dark was rewritten to ``HealthyQuiet`` with ``suspected_since`` left stamped -- a
        dangling onset on a phase the verdict no longer claimed. The rule a dark source
        obeys everywhere else applies here too: it never advances AND never rewinds a
        phase. Mutation-resistant by construction: flipping the preserved verdict back to
        HealthyQuiet fails both the verdict assertion and its onset's.
        """
        previous = episode(last_progress_at=0.0)
        suspected = reduce_vitality(
            previous,
            [heartbeat(400.0), provider(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(suspected.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(suspected.suspected_since, 300.0)

        # The provider goes dark mid-suspicion; the pid keeps answering Running.
        dark = reduce_vitality(
            suspected,
            [heartbeat(500.0), provider(500.0, available=False)],
            now=500.0,
            thresholds=THRESHOLDS,
        )

        # The suspicion stands frozen: same phase, same onset, no aging past it.
        self.assertEqual(dark.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(dark.suspected_since, 300.0)
        self.assertEqual(dark.confirmed_since, 0.0)
        self.assertIn("preserved-suspicion:provider-dark-pid-alive", dark.basis)

        # Inside the freeze window the phase neither advances nor rewinds.
        held = reduce_vitality(
            dark,
            [heartbeat(1_000.0), provider(1_000.0, available=False)],
            now=1_000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(held.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(held.suspected_since, 300.0)
        self.assertEqual(held.confirmed_since, 0.0)

        # Past `dark_ceiling` (dark since 500, ceiling 600) the freeze ends and the episode ages
        # on the pid's own sustained answer, exactly as a run whose provider never spoke does.
        # Until secretary-1543 this assertion read "later ticks change nothing", on the reasoning
        # that confirmation must never be inherited from a channel outage. It is not inherited
        # from one: it is earned on the same pid-only evidence `issue:06dcf6cb` already ages, and
        # the alternative -- an unbounded suspicion nothing can end -- is the state that left
        # secretary-1517 claimed for 65 minutes. Nothing destructive follows from it here either:
        # the guard still holds a confirmation earned with no answering progress source behind
        # the role's outer ceiling.
        later = reduce_vitality(
            dark,
            [heartbeat(900_000.0), provider(900_000.0, available=False)],
            now=900_000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(later.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(later.suspected_since, 300.0)
        self.assertEqual(later.confirmed_since, 900.0)
        self.assertIn("provider_cursor has been dark for", later.reason)

        # And only the four authorised endings still move it: real progress first.
        resumed = reduce_vitality(
            later,
            [heartbeat(900_100.0), provider(900_100.0, progress=ProgressState.ADVANCING, cursor="13:def")],
            now=900_100.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(resumed.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        self.assertEqual(resumed.suspected_since, 0.0)
        self.assertEqual(resumed.confirmed_since, 0.0)


class AbsentProgressChannelTests(unittest.TestCase):
    """secretary-1543 round 2: a source that produces NO snapshot is dark, not answering.

    Darkness used to be recorded only from an answer of absence (a snapshot that says
    UNAVAILABLE). The two production status shapes that carry a live heartbeat and no
    ``provider_progress`` key at all -- an exact live heartbeat whose pane the inventory lost, a
    pane that is not connected -- produce no provider snapshot whatsoever, so they left
    ``unavailable_since`` empty while the cursor from the tick that did answer stayed on file.
    The episode then read as witnessed-and-not-dark: it skipped the freeze window this card
    designed and the guard counted one channel as two.
    """

    def _witnessed(self) -> VitalityEpisode:
        return reduce_vitality(
            episode(),
            [heartbeat(10.0), provider(10.0)],
            now=10.0,
            thresholds=THRESHOLDS,
        )

    def test_a_witnessed_source_that_stops_answering_at_all_is_stamped_dark(self) -> None:
        witnessed = self._witnessed()
        self.assertEqual(witnessed.unavailable_since, {})
        self.assertIn(SnapshotSource.PROVIDER_CURSOR.value, witnessed.evidence_cursors)

        absent = reduce_vitality(witnessed, [heartbeat(20.0)], now=20.0, thresholds=THRESHOLDS)

        self.assertEqual(absent.unavailable_since[SnapshotSource.PROVIDER_CURSOR.value], 20.0)
        self.assertIn(f"absent@{SnapshotSource.PROVIDER_CURSOR.value}", absent.basis)
        self.assertEqual(absent.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertIn("not answering", absent.reason)

    def test_the_absent_channel_ages_on_the_same_ceiling_not_immediately(self) -> None:
        """The window, not a shortcut: 15 minutes of heartbeat alone used to confirm at once."""
        absent = reduce_vitality(self._witnessed(), [heartbeat(20.0)], now=20.0, thresholds=THRESHOLDS)

        # The source went dark at t=20.0, so darkness is measured from there. At t=400.0 it has
        # been dark for 380s, inside the 600s ceiling, and the episode stays quiet. At t=700.0 it
        # has been dark for 680s, past that ceiling, and the ladder resumes.
        held = reduce_vitality(absent, [heartbeat(400.0)], now=400.0, thresholds=THRESHOLDS)
        self.assertEqual(held.verdict, VitalityVerdict.HEALTHY_QUIET, "inside the dark ceiling")

        aged = reduce_vitality(held, [heartbeat(700.0)], now=700.0, thresholds=THRESHOLDS)
        self.assertEqual(aged.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertIn("provider_cursor has been dark for", aged.reason)

    def test_a_source_that_answers_again_leaves_the_dark_map(self) -> None:
        absent = reduce_vitality(self._witnessed(), [heartbeat(20.0)], now=20.0, thresholds=THRESHOLDS)

        back = reduce_vitality(
            absent,
            [heartbeat(30.0), provider(30.0, cursor="13:def", progress=ProgressState.ADVANCING)],
            now=30.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(back.unavailable_since, {})
        self.assertEqual(back.verdict, VitalityVerdict.HEALTHY_ACTIVE)

    def test_a_source_never_witnessed_is_not_invented_as_dark(self) -> None:
        """The issue 656 pid-only arm keeps its own words: nothing was ever heard from."""
        pid_only = reduce_vitality(episode(), [heartbeat(10.0)], now=10.0, thresholds=THRESHOLDS)
        aged = reduce_vitality(pid_only, [heartbeat(1000.0)], now=1000.0, thresholds=THRESHOLDS)

        self.assertEqual(aged.unavailable_since, {})
        self.assertEqual(aged.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertIn("running with no progress evidence", aged.reason)


class QuietRestartTests(unittest.TestCase):
    """secretary-1543 round 2: a conversational rung really restarts the quiet clock.

    The dispatcher's report nudge rewrote ``started_at``, but the reducer measures quiet from the
    LATER of the last observed progress and the last restart, so an episode that had ever seen
    the provider advance was re-confirmed on the very next tick -- removing the one rung that
    stands between a quiet head and a respawn.
    """

    def test_the_restart_stamp_moves_the_reference_past_an_older_progress(self) -> None:
        advanced = reduce_vitality(
            episode(),
            [heartbeat(10.0), provider(10.0, progress=ProgressState.ADVANCING)],
            now=10.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(advanced.last_progress_at, 10.0)

        # The dispatcher's nudge, as it writes it: verdict reset and the clock restarted at 1000.
        nudged = dataclass_replace(advanced, quiet_since=1000.0, started_at=1000.0)
        soon = reduce_vitality(
            nudged,
            [heartbeat(1100.0), provider(1100.0)],
            now=1100.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(soon.verdict, VitalityVerdict.HEALTHY_QUIET, "100s of grace, not 1090s")
        # And the head's real progress history is still on file for the operator.
        self.assertEqual(soon.last_progress_at, 10.0)
        later = reduce_vitality(
            soon,
            [heartbeat(1400.0), provider(1400.0)],
            now=1400.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(later.verdict, VitalityVerdict.SUSPECTED_STALL, "the grace is bounded")

    def test_the_restart_survives_a_round_trip_through_the_durable_form(self) -> None:
        stamped = episode(quiet_since=1234.0)
        self.assertEqual(VitalityEpisode.from_json(stamped.to_json()).quiet_since, 1234.0)
        # Records written before the field existed carry no restart, which is what they mean.
        legacy = {key: value for key, value in stamped.to_json().items() if key != "quiet_since"}
        self.assertEqual(VitalityEpisode.from_json(legacy).quiet_since, 0.0)


class DarkCeilingTests(unittest.TestCase):
    """secretary-1543: a witnessed-but-dark progress source freezes for a BOUNDED window."""

    def test_a_dark_progress_source_stops_freezing_past_the_ceiling(self) -> None:
        """The window the old unbounded freeze had no end to.

        The provider answers once (so the episode has witnessed it), then goes dark while the
        pid keeps saying Running. Inside ``dark_ceiling`` the episode is frozen healthy; past it
        the ladder climbs on the pid's own sustained answer.
        """
        first = reduce_vitality(
            episode(last_progress_at=0.0),
            [heartbeat(10.0), provider(10.0)],
            now=10.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(first.verdict, VitalityVerdict.HEALTHY_QUIET)
        dark_at = 20.0
        frozen = reduce_vitality(
            first,
            [heartbeat(dark_at), provider(dark_at, available=False)],
            now=dark_at,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(frozen.unavailable_since[SnapshotSource.PROVIDER_CURSOR.value], dark_at)

        # Quiet is well past both thresholds, but the source has been dark for only 590s.
        held = reduce_vitality(
            frozen,
            [heartbeat(610.0), provider(610.0, available=False)],
            now=610.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(held.verdict, VitalityVerdict.HEALTHY_QUIET)

        # One second past the ceiling the freeze ends and the ladder resumes where the quiet
        # actually is: 621s of it, past `suspect_after` and below `suspect+confirm`.
        aged = reduce_vitality(
            held,
            [heartbeat(621.0), provider(621.0, available=False)],
            now=621.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(aged.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertIn("provider_cursor has been dark for 601s", aged.reason)
        confirmed = reduce_vitality(
            aged,
            [heartbeat(950.0), provider(950.0, available=False)],
            now=950.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_the_ceiling_lets_the_ladder_climb_one_rung_at_a_time(self) -> None:
        """Past the ceiling the head is suspected first, and only later confirmed."""
        dark = reduce_vitality(
            episode(last_progress_at=0.0),
            [heartbeat(0.0), provider(0.0, available=False)],
            now=0.0,
            thresholds=THRESHOLDS,
        )
        # 600s: the ceiling has elapsed exactly, quiet is 600s -- past suspect, below confirm.
        suspected = reduce_vitality(
            dark,
            [heartbeat(600.0), provider(600.0, available=False)],
            now=600.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(suspected.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(suspected.suspected_since, 300.0)
        confirmed = reduce_vitality(
            suspected,
            [heartbeat(900.0), provider(900.0, available=False)],
            now=900.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(confirmed.confirmed_since, 900.0)

    def test_a_returning_source_ends_the_dark_window_and_the_next_one_starts_fresh(self) -> None:
        """The ceiling measures THIS outage, not the sum of every outage in the episode."""
        dark = reduce_vitality(
            episode(last_progress_at=0.0),
            [heartbeat(0.0), provider(0.0, available=False)],
            now=0.0,
            thresholds=THRESHOLDS,
        )
        back = reduce_vitality(
            dark,
            [heartbeat(100.0), provider(100.0)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        self.assertNotIn(SnapshotSource.PROVIDER_CURSOR.value, back.unavailable_since)
        dark_again = reduce_vitality(
            back,
            [heartbeat(200.0), provider(200.0, available=False)],
            now=200.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(dark_again.unavailable_since[SnapshotSource.PROVIDER_CURSOR.value], 200.0)
        # 500s into the SECOND outage: still frozen, though the episode's quiet is 700s old and
        # the first outage plus this one add up to well past the ceiling.
        still_frozen = reduce_vitality(
            dark_again,
            [heartbeat(700.0), provider(700.0, available=False)],
            now=700.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(still_frozen.verdict, VitalityVerdict.HEALTHY_QUIET)

    def test_a_retained_head_is_exempt_from_the_ceiling(self) -> None:
        """A deliberately parked worker is not woken by this card's aging (secretary-1539)."""
        parked = episode(last_progress_at=0.0)
        for tick in (0.0, 600.0, 5_000.0, 90_000.0):
            parked = reduce_vitality(
                parked,
                [
                    heartbeat(tick, process=ProcessState.SUSPENDED),
                    provider(tick, available=False),
                ],
                now=tick,
                thresholds=THRESHOLDS,
                retained=True,
            )
            self.assertEqual(parked.verdict, VitalityVerdict.RETAINED, tick)
            self.assertEqual(parked.suspected_since, 0.0)
            self.assertEqual(parked.confirmed_since, 0.0)


class AnswerOwedTests(unittest.TestCase):
    """secretary-1543: a rejected report plus an ended turn is an explicit signal."""

    def test_a_rejected_report_then_an_ended_turn_suspects_at_once(self) -> None:
        working = reduce_vitality(
            episode(last_progress_at=0.0),
            [heartbeat(100.0), provider(100.0), pane(100.0, idle=False)],
            now=100.0,
            thresholds=THRESHOLDS,
            answer_owed_since=50.0,
        )
        # The turn is still in flight: nothing is concluded from the rejection alone.
        self.assertEqual(working.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(working.last_turn, "active")

        ended = reduce_vitality(
            working,
            [heartbeat(120.0), provider(120.0), pane(120.0, idle=True)],
            now=120.0,
            thresholds=THRESHOLDS,
            answer_owed_since=50.0,
        )
        # 70s after the rejection -- far below `suspect_after` -- and already suspected.
        self.assertEqual(ended.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(ended.turn_ended_at, 120.0)
        self.assertIn("answer-owed:turn-ended", ended.basis)
        self.assertIn("rejected report", ended.reason)

    def test_the_same_ended_turn_with_no_owed_answer_concludes_nothing(self) -> None:
        """The advisory reading is exactly as weightless as it always was."""
        working = reduce_vitality(
            episode(last_progress_at=0.0),
            [heartbeat(100.0), provider(100.0), pane(100.0, idle=False)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        ended = reduce_vitality(
            working,
            [heartbeat(120.0), provider(120.0), pane(120.0, idle=True)],
            now=120.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(ended.verdict, VitalityVerdict.HEALTHY_QUIET)

    def test_a_head_that_never_took_a_turn_is_not_suspected_by_a_starting_pane(self) -> None:
        """secretary-1542 measured a starting Codex head answering idle throughout."""
        starting = episode(last_progress_at=0.0)
        for tick in (10.0, 40.0, 90.0):
            starting = reduce_vitality(
                starting,
                [heartbeat(tick), provider(tick, available=False), pane(tick, idle=True)],
                now=tick,
                thresholds=THRESHOLDS,
                answer_owed_since=5.0,
            )
            self.assertEqual(starting.verdict, VitalityVerdict.HEALTHY_QUIET, tick)
            self.assertEqual(starting.turn_ended_at, 0.0)

    def test_progress_after_the_rejection_answers_it(self) -> None:
        ended = reduce_vitality(
            episode(last_progress_at=0.0, last_turn="active"),
            [
                heartbeat(200.0),
                provider(200.0, progress=ProgressState.ADVANCING, cursor="13:def"),
                pane(200.0, idle=True),
            ],
            now=200.0,
            thresholds=THRESHOLDS,
            answer_owed_since=50.0,
        )
        self.assertEqual(ended.verdict, VitalityVerdict.HEALTHY_ACTIVE)
        quiet_again = reduce_vitality(
            ended,
            [heartbeat(260.0), provider(260.0, cursor="13:def"), pane(260.0, idle=True)],
            now=260.0,
            thresholds=THRESHOLDS,
            answer_owed_since=50.0,
        )
        # The head advanced after the rejection, so the owed answer no longer convicts it.
        self.assertEqual(quiet_again.verdict, VitalityVerdict.HEALTHY_QUIET)

    def test_the_owed_answer_never_lowers_a_verdict_and_is_validated(self) -> None:
        confirmed = episode(
            verdict=VitalityVerdict.CONFIRMED_STALL,
            last_progress_at=0.0,
            confirmed_since=900.0,
            last_turn="active",
        )
        still = reduce_vitality(
            confirmed,
            [heartbeat(2_000.0), provider(2_000.0), pane(2_000.0, idle=True)],
            now=2_000.0,
            thresholds=THRESHOLDS,
            answer_owed_since=1_000.0,
        )
        self.assertEqual(still.verdict, VitalityVerdict.CONFIRMED_STALL)
        for bad in (-1.0, float("nan"), float("inf"), True, "now"):
            with self.subTest(answer_owed_since=bad), self.assertRaises(HeadVitalityError):
                reduce_vitality(
                    None,
                    [heartbeat(10.0)],
                    now=10.0,
                    thresholds=THRESHOLDS,
                    answer_owed_since=bad,
                )


class Ummanu1517Tests(unittest.TestCase):
    """The exact incident shape: live Codex PID, idle pane, provider cursor never admitted."""

    def _tick(self, previous, at: float):
        return reduce_vitality(
            previous,
            [
                heartbeat(at),
                # `provider_progress_for_run` answers unavailable for a Codex head with no bound
                # v1 baseline; the pane answers idle throughout, which proves nothing.
                provider(at, available=False),
                pane(at, idle=True),
            ],
            now=at,
            thresholds=THRESHOLDS,
        )

    def test_the_frozen_head_is_suspected_and_then_confirmed_within_the_hour(self) -> None:
        state = None
        seen: dict[str, float] = {}
        # One tick a minute for 65 minutes, the span the card actually sat claimed.
        for minute in range(0, 66):
            at = float(minute * 60)
            state = self._tick(state, at)
            seen.setdefault(state.verdict.value, at)
        self.assertEqual(seen["healthy_quiet"], 0.0)
        # The freeze ends at `dark_ceiling` (600s), and quiet is already past `suspect_after`.
        self.assertEqual(seen["suspected_stall"], 600.0)
        self.assertEqual(seen["confirmed_stall"], 900.0)
        self.assertNotIn("dead", seen)
        self.assertEqual(state.verdict, VitalityVerdict.CONFIRMED_STALL)
        # The operator-facing reason names the dark source and how long it has been dark.
        self.assertIn("provider_cursor not answering for", state.reason)
        # And the pane's idle answer stayed corroboration, never evidence.
        self.assertIn("advisory:idle@pane_advisory", state.basis)

    def test_the_incident_shape_is_frozen_healthy_before_the_ceiling(self) -> None:
        state = None
        for minute in range(0, 10):
            state = self._tick(state, float(minute * 60))
        self.assertEqual(state.verdict, VitalityVerdict.HEALTHY_QUIET)


class AdvisoryTests(unittest.TestCase):
    """Invariant (e): pane evidence corroborates and never convicts."""

    def test_advisory_idle_alone_stays_unverifiable_and_never_suspected(self) -> None:
        reduced = reduce_vitality(
            None,
            [pane(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.UNVERIFIABLE)

    def test_advisory_idle_for_hours_cannot_reach_a_stall_verdict(self) -> None:
        first = reduce_vitality(None, [pane(0.0)], now=0.0, thresholds=THRESHOLDS)
        reduced = reduce_vitality(first, [pane(100_000.0)], now=100_000.0, thresholds=THRESHOLDS)

        self.assertEqual(reduced.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertTrue(any(token.startswith("advisory:") for token in reduced.basis))

    def test_advisory_corroboration_appears_in_basis_alongside_strong_quiet(self) -> None:
        reduced = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0), pane(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertTrue(any(token.startswith("advisory:") for token in reduced.basis))
        self.assertEqual(reduced.verdict, VitalityVerdict.HEALTHY_QUIET)


class QuietLadderTests(unittest.TestCase):
    """Invariant (f): quiet accumulates from the progress reference, not from ticks."""

    def test_quiet_climbs_healthy_then_suspected_then_confirmed(self) -> None:
        previous = episode(last_progress_at=0.0)
        quiet = reduce_vitality(
            previous,
            [heartbeat(100.0), provider(100.0)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(quiet.verdict, VitalityVerdict.HEALTHY_QUIET)

        suspected = reduce_vitality(
            quiet,
            [heartbeat(400.0), provider(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(suspected.verdict, VitalityVerdict.SUSPECTED_STALL)
        self.assertEqual(suspected.suspected_since, 300.0)

        confirmed = reduce_vitality(
            suspected,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(confirmed.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(confirmed.confirmed_since, 900.0)

    def test_timers_are_measured_from_last_progress_not_from_the_last_tick(self) -> None:
        """Ticks are irregular: a 10-minute gap between ticks must not shrink the quiet the
        episode charges, and a burst of ticks must not stretch it either."""
        previous = episode(last_progress_at=0.0)
        burst = previous
        for tick in (10.0, 20.0, 30.0, 40.0):
            burst = reduce_vitality(
                burst,
                [heartbeat(tick), provider(tick)],
                now=tick,
                thresholds=THRESHOLDS,
            )
        self.assertEqual(burst.verdict, VitalityVerdict.HEALTHY_QUIET)

        # One long-silent stretch lands exactly on the threshold the wall clock says.
        crossed = reduce_vitality(
            burst,
            [heartbeat(300.0), provider(300.0)],
            now=300.0,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(crossed.verdict, VitalityVerdict.SUSPECTED_STALL)

    def test_fresh_advancement_restarts_the_quiet_reference(self) -> None:
        previous = episode(last_progress_at=0.0)
        quiet = reduce_vitality(
            previous,
            [heartbeat(100.0), provider(100.0)],
            now=100.0,
            thresholds=THRESHOLDS,
        )
        advanced = reduce_vitality(
            quiet,
            [heartbeat(200.0), provider(200.0, progress=ProgressState.ADVANCING, cursor="13:def")],
            now=200.0,
            thresholds=THRESHOLDS,
        )
        # Quiet measured from the new progress, not from the episode start.
        still_healthy = reduce_vitality(
            advanced,
            [heartbeat(400.0), provider(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(advanced.last_progress_at, 200.0)
        self.assertEqual(still_healthy.verdict, VitalityVerdict.HEALTHY_QUIET)


class HysteresisTests(unittest.TestCase):
    """Invariant (g): confirmation is sticky, and the verdict always carries basis and run."""

    def test_one_quiet_tick_does_not_bounce_a_confirmed_episode_back(self) -> None:
        confirmed = episode(
            verdict=VitalityVerdict.CONFIRMED_STALL,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=0.0,
        )
        reduced = reduce_vitality(
            confirmed,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_only_progress_suspension_death_or_identity_ends_a_confirmation(self) -> None:
        confirmed = episode(
            verdict=VitalityVerdict.CONFIRMED_STALL,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=0.0,
        )
        ends = [
            ("progress", [heartbeat(1000.0), provider(1000.0, progress=ProgressState.ADVANCING)]),
            ("suspended", [heartbeat(1000.0, process=ProcessState.SUSPENDED), provider(1000.0)]),
            ("dead", [heartbeat(1000.0, process=ProcessState.DEAD), provider(1000.0)]),
            ("identity", [heartbeat(1000.0, run_id="run-respawned")]),
        ]
        for name, batch in ends:
            with self.subTest(ended_by=name):
                reduced = reduce_vitality(confirmed, batch, now=1000.0, thresholds=THRESHOLDS)
                self.assertNotEqual(reduced.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_every_verdict_carries_its_run_and_a_basis(self) -> None:
        batches = [
            [heartbeat(1000.0), provider(1000.0)],
            [heartbeat(1000.0, process=ProcessState.DEAD)],
            [heartbeat(1000.0, available=False)],
        ]
        for batch in batches:
            reduced = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)
            with self.subTest(verdict=reduced.verdict):
                self.assertEqual(reduced.run_id, RUN_ID)
                self.assertTrue(reduced.basis)


class PurityTests(unittest.TestCase):
    """Invariant (h): deterministic and pure -- no I/O, no clock, same inputs, same output."""

    def test_same_inputs_reduce_to_the_same_episode(self) -> None:
        batch = [heartbeat(100.0), provider(100.0)]
        previous = episode(last_progress_at=0.0)
        self.assertEqual(
            reduce_vitality(previous, batch, now=100.0, thresholds=THRESHOLDS),
            reduce_vitality(previous, batch, now=100.0, thresholds=THRESHOLDS),
        )

    def test_the_previous_episode_is_not_mutated_by_a_reduction(self) -> None:
        previous = episode(
            verdict=VitalityVerdict.SUSPECTED_STALL,
            suspected_since=300.0,
            last_progress_at=0.0,
        )
        frozen_previous = replace_episode(previous)
        reduce_vitality(
            previous,
            [heartbeat(1000.0), provider(1000.0, progress=ProgressState.ADVANCING)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(previous, frozen_previous)

    def test_snapshot_order_in_a_batch_does_not_change_the_verdict(self) -> None:
        first = reduce_vitality(
            None,
            [heartbeat(1000.0), provider(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )
        second = reduce_vitality(
            None,
            [provider(1000.0), heartbeat(1000.0)],
            now=1000.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(first, second)

    def test_snapshot_order_does_not_change_the_written_basis(self) -> None:
        """The whole episode, basis included, is a function of the batch -- not of the order
        the channels happened to answer in."""
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)
        forward = reduce_vitality(
            previous,
            [heartbeat(400.0), provider(400.0), pane(400.0), provider(401.0, run_id="run-other")],
            now=400.0,
            thresholds=THRESHOLDS,
        )
        backward = reduce_vitality(
            previous,
            [provider(401.0, run_id="run-other"), pane(400.0), provider(400.0), heartbeat(400.0)],
            now=400.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(forward, backward)
        self.assertEqual(forward.basis, backward.basis)

    def test_two_dark_sources_write_their_unavailable_basis_in_source_order(self) -> None:
        """The review follow-up: with both strong sources dark in one tick the ``unavailable``
        tokens were emitted in batch order, so a reversed batch wrote a different basis for the
        same facts. The sources are walked sorted, and this pins the episode -- basis included --
        as equal under reversal."""
        batch = [
            provider(1000.0, available=False),
            heartbeat(1000.0, available=False),
            pane(1000.0),
        ]
        forward = reduce_vitality(None, batch, now=1000.0, thresholds=THRESHOLDS)
        backward = reduce_vitality(None, list(reversed(batch)), now=1000.0, thresholds=THRESHOLDS)

        unavailable_tokens = [token for token in forward.basis if token.startswith("unavailable@")]
        # Both dark sources are named, and named in sorted-source order regardless of the order
        # the channels failed in.
        self.assertEqual(
            unavailable_tokens,
            ["unavailable@pid_heartbeat", "unavailable@provider_cursor"],
        )
        self.assertEqual(forward.verdict, VitalityVerdict.UNVERIFIABLE)
        self.assertEqual(forward, backward)
        self.assertEqual(forward.basis, backward.basis)

    def test_no_snapshots_leaves_the_previous_episode_untouched(self) -> None:
        previous = episode(verdict=VitalityVerdict.SUSPECTED_STALL, suspected_since=300.0)

        reduced = reduce_vitality(previous, [], now=1000.0, thresholds=THRESHOLDS)

        self.assertEqual(reduced, previous)


class SerialisationTests(unittest.TestCase):
    def test_a_full_episode_round_trips_through_json_text(self) -> None:
        full = VitalityEpisode(
            run_id=RUN_ID,
            verdict=VitalityVerdict.CONFIRMED_STALL,
            started_at=0.0,
            suspected_since=300.0,
            confirmed_since=900.0,
            last_progress_at=100.0,
            last_progress_source=SnapshotSource.PROVIDER_CURSOR.value,
            evidence_cursors={SnapshotSource.PROVIDER_CURSOR.value: "14:def"},
            unavailable_since={SnapshotSource.PANE_ADVISORY.value: 500.0},
            basis=("quiet:1000s@provider_cursor", "confirmed-stall"),
            reason="strong quiet for 1000s with no advancement",
            recovery_rung=0,
            activity_epoch=3,
            updated_at=1000.0,
            stall_frozen_since=0.0,
        )

        restored = VitalityEpisode.from_json(json.loads(json.dumps(full.to_json())))

        self.assertEqual(restored, full)

    def test_a_fresh_episode_round_trips_with_its_empty_maps(self) -> None:
        fresh = VitalityEpisode(run_id=RUN_ID, started_at=5.0, updated_at=5.0)

        restored = VitalityEpisode.from_json(json.loads(json.dumps(fresh.to_json())))

        self.assertEqual(restored, fresh)
        self.assertEqual(restored.evidence_cursors, {})
        self.assertEqual(restored.unavailable_since, {})

    def test_from_json_refuses_payloads_that_change_meaning(self) -> None:
        base = VitalityEpisode(run_id=RUN_ID, started_at=1.0, updated_at=1.0).to_json()
        cases = [
            {**base, "version": EPISODE_VERSION + 1},
            {**base, "version": True},
            {**base, "version": 1.0},
            "not-an-object",
            {**base, "verdict": "totally-fine"},
            {**base, "run_id": ""},
            {**base, "recovery_rung": "zero"},
            {**base, "recovery_rung": -1},
            {**base, "activity_epoch": -1},
            {**base, "evidence_cursors": {"provider_cursor": 12}},
            {**base, "unavailable_since": {"provider_cursor": "recently"}},
            {**base, "basis": "quiet"},
            {**base, "basis": ["quiet", 7]},
            {**base, "updated_at": None},
            {**base, "updated_at": float("nan")},
            {**base, "suspected_since": "300"},
            # An integer beyond the float range: float() raises OverflowError, refused as inf.
            {**base, "updated_at": 10**400},
            {**base, "unavailable_since": {"provider_cursor": 10**400}},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(HeadVitalityError):
                VitalityEpisode.from_json(payload)


class ThresholdTests(unittest.TestCase):
    def test_thresholds_must_be_positive_finite_numbers(self) -> None:
        for bad in (0, -5, float("nan"), float("inf"), "300", True):
            with self.subTest(suspect_after=bad), self.assertRaises(HeadVitalityError):
                VitalityThresholds(suspect_after=bad, confirm_after=600.0)

    def test_defaults_align_with_the_watchdogs_idle_ceiling(self) -> None:
        from ummanu.dispatch.watchdog import IDLE_STALL_DEFAULT

        self.assertEqual(DEFAULT_VITALITY_THRESHOLDS.suspect_after, float(IDLE_STALL_DEFAULT))
        self.assertEqual(DEFAULT_VITALITY_THRESHOLDS.confirm_after, 2.0 * float(IDLE_STALL_DEFAULT))
        self.assertEqual(
            (DEFAULT_VITALITY_THRESHOLDS.suspect_after, DEFAULT_VITALITY_THRESHOLDS.confirm_after),
            (300.0, 600.0),
        )


def replace_episode(value: VitalityEpisode) -> VitalityEpisode:
    """A structural copy used to prove reductions never mutate their input."""
    return VitalityEpisode.from_json(json.loads(json.dumps(value.to_json())))


if __name__ == "__main__":
    unittest.main()
