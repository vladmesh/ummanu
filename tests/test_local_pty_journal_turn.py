"""secretary-1739: the supervisor journal answers the Turn axis, and an idle head that owes its
answer is confirmed stalled in minutes.

The incident is secretary-1727 round 2 (run `9c6b884b…`). The continuation turn opened at
23:34:39Z and the supervisor closed it on quiet at 23:43:09Z, after the provider lost its API
connection mid-response. The head then sat at its prompt, owing its report, with no journal record
for 3,549 s. Its episode read `healthy_active` for about nineteen minutes of that silence and
`healthy_quiet` for about forty more; `suspected_stall` came at 00:42:12Z. The Turn axis was
`Unknown` throughout, because no `local-pty` status carried one.

`tests/fixtures/local_pty_journals/secretary_1727_9c6b884b.jsonl.gz` is that run's journal,
trimmed: every record except four in five `provider.progressed` inside a run of them (the last
before any other record is kept), and `run.started` without its `command`, which carries the
head's memory token. Sequence numbers and times are the originals.
"""

from __future__ import annotations

import copy
import gzip
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ummanu.dispatch import review as dispatcher_review, wait_vitality
from ummanu.dispatch.head_vitality import (
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    TurnState,
    VitalitySnapshot,
    snapshots_from_status,
)
from ummanu.dispatch.head_vitality_episode import (
    CHILD_ACTIVITY_CEILING_DEFAULT,
    DEFAULT_VITALITY_THRESHOLDS,
    IDLE_TURN_ADAPTERS,
    IDLE_TURN_CONFIRM_DEFAULT,
    IDLE_TURN_SUSPECT_DEFAULT,
    VitalityEpisode,
    VitalityVerdict,
    recovery_outlook,
    reduce_vitality,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.worker_lifecycle import head_run_binding
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from ummanu.runtime.local_pty_head import head_run_turn_reading

FIXTURE = Path(__file__).parent / "fixtures" / "local_pty_journals" / "secretary_1727_9c6b884b.jsonl.gz"
RUN_1727 = "9c6b884b688e447faa10f811ce3b727c"
SUSPECT_IDLE = DEFAULT_VITALITY_THRESHOLDS.idle_turn_suspect_after
CONFIRM_IDLE = DEFAULT_VITALITY_THRESHOLDS.idle_turn_confirm_after
REF = "ummanu-9739"
TICK = 65.0
# 2026-09-24T23:34:39Z: the continuation's submit opens turn 4 (seq 2956).
TURN_4_STARTED = 1790292879.669232
# 2026-09-24T23:43:07.612Z: the provider transcript's last line before the silence.
TRANSCRIPT_LAST = 1790293387.612
# 2026-09-24T23:43:09.672Z: `turn.finished reason=quiet` (seq 3712).
TURN_4_FINISHED = 1790293389.67212
# 2026-09-25T00:42:16.727Z: the watchdog's report nudge lands and opens turn 5 (seq 3713/3714).
NUDGE_LANDED = 1790296936.726257
# 2026-09-24T23:34:40Z: the tick that resumed the retained worker stamps `worker_started_at`.
RESUMED_AT = 1790292880.0


def _fixture_records() -> list[dict[str, Any]]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def _line(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class _JournalDir:
    """A local-pty root with one run directory whose journal the test writes."""

    def __init__(self, test: unittest.TestCase, run_id: str) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run_id = run_id
        self.path = protocol.run_dir_for(self.root, run_id) / protocol.JOURNAL_NAME
        self.path.parent.mkdir(parents=True)

    def write(self, records: list[dict[str, Any]], *, raw_tail: bytes = b"") -> None:
        self.path.write_bytes(b"".join(_line(record) for record in records) + raw_tail)

    def read(self, **kwargs: Any) -> dict[str, Any]:
        return head_run_turn_reading(self.root, self.run_id, **kwargs)


def _record(seq: int, kind: str, at: float, run_id: str = "run-a", **fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, "seq": seq, "run_id": run_id, "kind": kind, "at": at, **fields}


def _clean_journal(run_id: str = "run-a", base: float = 1_790_000_000.0) -> list[dict[str, Any]]:
    """Bring-up, one delivered turn with progress, and the turn closed on quiet."""
    return [
        _record(1, "run.started", base, run_id, role="worker"),
        _record(2, "input.accepted", base + 5, run_id, subject="worker-launch", bytes=10),
        _record(3, "turn.started", base + 5, run_id, subject="worker-launch", turn=1),
        _record(4, "provider.progressed", base + 30, run_id, turn=1, output_bytes=100),
        _record(5, "provider.progressed", base + 90, run_id, turn=1, output_bytes=100),
        _record(6, "turn.finished", base + 120, run_id, turn=1, reason="quiet", output_bytes=200),
    ]


class SupervisorJournalSourceTests(unittest.TestCase):
    """`head_run_turn_reading`: the journal answered as data, and every failure as unavailable."""

    def test_an_open_turn_is_active_since_its_turn_started(self) -> None:
        journal = _JournalDir(self, "run-a")
        journal.write(_clean_journal()[:5])
        reading = journal.read()
        self.assertEqual(reading["state"], "observed")
        self.assertEqual(reading["run_id"], "run-a")
        self.assertEqual(reading["turn"], "active")
        self.assertEqual(reading["turn_since"], 1_790_000_005.0)
        self.assertEqual((reading["progress_seq"], reading["progress_at"]), (5, 1_790_000_090.0))

    def test_a_closed_turn_is_idle_since_it_finished(self) -> None:
        journal = _JournalDir(self, "run-a")
        journal.write(_clean_journal())
        reading = journal.read()
        self.assertEqual((reading["turn"], reading["turn_since"]), ("idle", 1_790_000_120.0))

    def test_a_delivery_after_the_turn_finished_restarts_the_idle_clock(self) -> None:
        # `input.accepted` is written when the bytes land, before the turn is opened for them.
        journal = _JournalDir(self, "run-a")
        journal.write(
            [*_clean_journal(), _record(7, "input.accepted", 1_790_000_400.0, subject="worker-continuation")]
        )
        reading = journal.read()
        self.assertEqual((reading["turn"], reading["turn_since"]), ("idle", 1_790_000_400.0))

    def test_every_window_that_cannot_answer_is_unavailable_with_a_reason(self) -> None:
        clean = _clean_journal()
        cases: dict[str, tuple[list[dict[str, Any]], bytes]] = {
            "no journal yet": ([], b""),
            "bring-up, no turn yet": (clean[:1], b""),
            "delivered, no turn yet": (clean[:2], b""),
            "torn last line": (clean, b'{"schema_version":1,"seq":7'),
            "malformed line": (clean[:3] + clean[4:], b"not json\n"),
            "invalid utf-8": (clean, b"\xff\xfe\xfd\n"),
            "out of order": ([*clean[:4], clean[5], clean[4]], b""),
            "another run only": (_clean_journal("run-b"), b""),
            "another run mixed in": (
                [*clean, _record(7, "turn.started", 1_790_000_130.0, "run-b", turn=2)],
                b"",
            ),
            "head exited": ([*clean, _record(7, "run.exited", 1_790_000_130.0, exit_code=0)], b""),
        }
        for name, (records, tail) in cases.items():
            with self.subTest(name):
                journal = _JournalDir(self, "run-a")
                if records or tail:
                    journal.write(records, raw_tail=tail)
                reading = journal.read()
                self.assertEqual(reading["state"], "unavailable", reading)
                self.assertTrue(reading["reason"])
                self.assertNotIn("turn", reading)

    def test_a_window_from_mid_history_answers_only_when_it_holds_a_turn_boundary(self) -> None:
        # Every real worker journal outgrows the tail within minutes (1727's is 873 KB), so a
        # partial window is the ordinary case, not a failure.
        journal = _JournalDir(self, "run-a")
        padding = [
            _record(
                seq, "provider.progressed", 1_790_000_000.0 + seq, turn=1, output_bytes=100, note="x" * 200
            )
            for seq in range(4, 60)
        ]
        records = [*_clean_journal()[:3], *padding]
        journal.write([*records, _record(60, "turn.finished", 1_790_000_070.0, turn=1, reason="quiet")])
        anchored = journal.read(max_bytes=2048)
        self.assertEqual((anchored["turn"], anchored["turn_since"]), ("idle", 1_790_000_070.0))
        journal.write(records)
        unanchored = journal.read(max_bytes=2048)
        self.assertEqual(unanchored["state"], "unavailable")
        self.assertIn("mid-history", unanchored["reason"])

    def test_an_unreadable_journal_is_unavailable_not_an_exception(self) -> None:
        journal = _JournalDir(self, "run-a")
        journal.path.mkdir()
        self.assertEqual(journal.read()["state"], "unavailable")
        self.assertEqual(head_run_turn_reading(journal.root, "../escape")["state"], "unavailable")

    def test_an_infinite_sequence_is_a_malformed_record_for_every_reader(self) -> None:
        # `json.loads` reads a bare `Infinity`; `int(inf)` raised out of `read_tail` before.
        from ummanu.runtime.head import local_pty

        journal = _JournalDir(self, "run-a")
        journal.write(
            _clean_journal(),
            raw_tail=_line(_record(7, "turn.started", 1.0)).replace(b'"seq":7', b'"seq":Infinity'),
        )
        self.assertEqual(local_pty.read_tail(journal.path).malformed, 1)
        self.assertEqual(journal.read()["state"], "unavailable")

    def test_the_1727_journal_reads_idle_since_the_turn_finished(self) -> None:
        journal = _JournalDir(self, RUN_1727)
        journal.write([record for record in _fixture_records() if record["seq"] <= 3712])
        reading = journal.read(max_bytes=16 * 1024)
        self.assertEqual(reading["state"], "observed")
        self.assertEqual((reading["turn"], reading["turn_since"]), ("idle", TURN_4_FINISHED))


class SupervisorJournalSnapshotTests(unittest.TestCase):
    """`from_supervisor_journal`: one normaliser, a non-advisory snapshot, Process never touched."""

    def reading(self, **overrides: Any) -> dict[str, Any]:
        return {
            "state": "observed",
            "run_id": "run-a",
            "turn": "idle",
            "turn_since": 1_000.0,
            "progress_seq": 7,
            **overrides,
        }

    def snapshot(self, reading: Any, previous: str = "", observed_at: float = 2_000.0) -> VitalitySnapshot:
        return VitalitySnapshot.from_supervisor_journal(
            reading, run_id="run-a", previous_cursor=previous, observed_at=observed_at
        )

    def test_turn_and_progress_map_from_the_reading(self) -> None:
        first = self.snapshot(self.reading(turn="active"))
        self.assertIs(first.source, SnapshotSource.SUPERVISOR_JOURNAL)
        self.assertFalse(first.advisory)
        self.assertIs(first.availability, SourceAvailability.AVAILABLE)
        self.assertIs(first.turn, TurnState.ACTIVE)
        self.assertIs(first.progress, ProgressState.UNKNOWN)
        self.assertIs(first.process.value, "unknown")
        self.assertEqual(first.turn_since, 1_000.0)
        moved = self.snapshot(self.reading(progress_seq=9), previous=first.cursor or "")
        self.assertIs(moved.progress, ProgressState.ADVANCING)
        still = self.snapshot(self.reading(), previous=first.cursor or "")
        self.assertIs(still.progress, ProgressState.QUIET)
        self.assertIs(still.turn, TurnState.IDLE)
        backwards = self.snapshot(self.reading(progress_seq=3), previous=first.cursor or "")
        self.assertIs(backwards.availability, SourceAvailability.UNAVAILABLE)

    def test_the_snapshot_survives_serialisation_and_old_payloads_load(self) -> None:
        snapshot = self.snapshot(self.reading())
        self.assertEqual(VitalitySnapshot.from_json(snapshot.to_json()), snapshot)
        old = snapshot.to_json()
        del old["turn_since"]
        self.assertEqual(VitalitySnapshot.from_json(old).turn_since, 0.0)

    def test_status_carrying_the_journal_yields_one_snapshot_of_it(self) -> None:
        snapshots = snapshots_from_status(
            {"supervisor_journal": self.reading()}, run_id="run-a", observed_at=2_000.0
        )
        self.assertEqual([snapshot.source for snapshot in snapshots], [SnapshotSource.SUPERVISOR_JOURNAL])
        self.assertEqual(snapshots_from_status({"pid_status": None}, run_id="run-a", observed_at=2_000.0), [])

    def test_hostile_values_in_every_field_are_unavailable_never_raised(self) -> None:
        hostile: list[Any] = [
            None,
            True,
            False,
            "12",
            1e300,
            -1,
            10**400,
            math.nan,
            math.inf,
            -math.inf,
            [],
            {},
        ]
        for field in ("state", "run_id", "turn", "turn_since", "progress_seq"):
            for value in [*hostile, "missing"]:
                reading = self.reading()
                if value == "missing":
                    del reading[field]
                else:
                    reading[field] = value
                with self.subTest(field=field, value=repr(value)[:20]):
                    snapshot = self.snapshot(reading)
                    self.assertIs(snapshot.availability, SourceAvailability.UNAVAILABLE)
                    self.assertIs(snapshot.turn, TurnState.UNKNOWN)
                    self.assertTrue(snapshot.reason)
        for value in [None, 7, "x", [], {"state": "observed"}]:
            with self.subTest(reading=repr(value)):
                self.assertIs(self.snapshot(value).availability, SourceAvailability.UNAVAILABLE)
        # A time from after the observation is damaged, not early.
        self.assertIs(
            self.snapshot(self.reading(turn_since=5_000.0)).availability, SourceAvailability.UNAVAILABLE
        )


def _journal_snapshot(turn: str, since: float, seq: int, previous: str, now: float) -> VitalitySnapshot:
    return VitalitySnapshot.from_supervisor_journal(
        {"state": "observed", "run_id": "run-a", "turn": turn, "turn_since": since, "progress_seq": seq},
        run_id="run-a",
        previous_cursor=previous,
        observed_at=now,
    )


def _pid(now: float, *, stopped: bool = False) -> VitalitySnapshot:
    return VitalitySnapshot.from_pid_heartbeat(
        {"state": "live-match", "stopped": stopped}, run_id="run-a", observed_at=now
    )


def _provider(cursor: str, previous: str, now: float) -> VitalitySnapshot:
    return VitalitySnapshot.from_provider_cursor(
        {"state": "observed", "admission": "accepted", "head_run_id": "run-a", "cursor": cursor},
        run_id="run-a",
        previous_cursor=previous,
        observed_at=now,
    )


def _child(cpu_ms: int, previous: str, now: float) -> VitalitySnapshot:
    return VitalitySnapshot.from_child_activity(
        {
            "state": "observed",
            "uptime_ticks": 100,
            "total_cpu_ms": cpu_ms,
            "total_io": 0,
            "descendant_count": 1,
            "descendants": [{"pid": 4242, "start": 50, "cpu_ms": cpu_ms, "io": 0, "command": "mcp-helper"}],
        },
        run_id="run-a",
        previous_cursor=previous,
        observed_at=now,
    )


class _Timeline:
    """Tick a run through the production builders and reducer, carrying each source's cursor."""

    def __init__(self, start: float) -> None:
        self.now = start
        self.episode: VitalityEpisode | None = None
        self.progress_seq = 1
        self.turn = "active"
        self.turn_since = start
        self.child_cpu = 0
        self.child_moves = False
        self.provider = 0
        self.provider_moves = False
        self.adapter = "claude"

    def cursor(self, source: SnapshotSource) -> str:
        return (self.episode.evidence_cursors if self.episode else {}).get(source.value, "")

    def tick(self, *, owed: float, stopped: bool = False, retained: bool = False) -> VitalityEpisode:
        if self.child_moves:
            self.child_cpu += 5_000
        if self.provider_moves:
            self.provider += 1
        snapshots = [
            _pid(self.now, stopped=stopped),
            _provider(f"p{self.provider}", self.cursor(SnapshotSource.PROVIDER_CURSOR), self.now),
            _child(self.child_cpu, self.cursor(SnapshotSource.EXECUTION_CHILD), self.now),
            _journal_snapshot(
                self.turn,
                self.turn_since,
                self.progress_seq,
                self.cursor(SnapshotSource.SUPERVISOR_JOURNAL),
                self.now,
            ),
        ]
        self.episode = reduce_vitality(
            self.episode, snapshots, self.now, retained=retained, answer_owed_since=owed, adapter=self.adapter
        )
        return self.episode


class IdleTurnStallRuleTests(unittest.TestCase):
    """`_idle_turn_stall`, the one rule for a finished, silent head that owes its answer."""

    def test_the_numbers_are_named_and_confirmation_is_within_ten_minutes(self) -> None:
        self.assertEqual(IDLE_TURN_SUSPECT_DEFAULT, 300.0)
        self.assertEqual(IDLE_TURN_CONFIRM_DEFAULT, 600.0)
        self.assertEqual(SUSPECT_IDLE, IDLE_TURN_SUSPECT_DEFAULT)
        self.assertEqual(CONFIRM_IDLE, IDLE_TURN_CONFIRM_DEFAULT)
        self.assertLessEqual(CONFIRM_IDLE, 600.0)

    def idle_head(self, *, child_moves: bool) -> tuple[_Timeline, float]:
        timeline = _Timeline(10_000.0)
        for _ in range(3):
            timeline.progress_seq += 5
            timeline.provider_moves = True
            timeline.tick(owed=9_000.0)
            timeline.now += 60.0
        timeline.provider_moves = False
        timeline.child_moves = child_moves
        ended = timeline.now - 30.0
        timeline.turn, timeline.turn_since = "idle", ended
        # The first tick after the turn closed sees its last progress record, as 1727's did.
        timeline.progress_seq += 1
        timeline.tick(owed=9_000.0)
        timeline.now += 60.0
        return timeline, ended

    def test_an_idle_head_that_owes_its_answer_climbs_from_the_turn_end(self) -> None:
        for child_moves in (False, True):
            with self.subTest(child_moves=child_moves):
                timeline, ended = self.idle_head(child_moves=child_moves)
                seen: list[tuple[float, VitalityVerdict]] = []
                while timeline.now < ended + CONFIRM_IDLE + 120.0:
                    seen.append((timeline.now - ended, timeline.tick(owed=9_000.0).verdict))
                    timeline.now += 60.0
                for idle, verdict in seen:
                    if idle >= CONFIRM_IDLE:
                        expected = VitalityVerdict.CONFIRMED_STALL
                    elif idle >= SUSPECT_IDLE:
                        expected = VitalityVerdict.SUSPECTED_STALL
                    else:
                        expected = VitalityVerdict.HEALTHY_QUIET
                    self.assertIs(verdict, expected, f"{idle:.0f}s idle")
                episode = timeline.episode
                assert episode is not None
                self.assertEqual(episode.confirmed_since, ended + CONFIRM_IDLE)
                self.assertEqual(episode.suspected_since, ended + SUSPECT_IDLE)
                self.assertEqual(episode.idle_turn_since, ended)
                self.assertIn("idle-turn", " ".join(episode.basis))
                if child_moves:
                    self.assertIn("child-not-holding-idle-turn", episode.basis)

    def test_child_activity_still_holds_an_open_turn_up_to_its_ceiling(self) -> None:
        # secretary-1665's long foreground command: the turn is open, the journal and the provider
        # are quiet, the test child burns CPU.
        timeline, _ = self.idle_head(child_moves=True)
        timeline.turn, timeline.turn_since = "active", timeline.now - 30.0
        verdicts = []
        start = timeline.now
        while timeline.now < start + CHILD_ACTIVITY_CEILING_DEFAULT - 300.0:
            verdicts.append(timeline.tick(owed=9_000.0).verdict)
            timeline.now += 60.0
        self.assertEqual(set(verdicts[1:]), {VitalityVerdict.HEALTHY_ACTIVE})

    def test_an_open_turn_writing_progress_is_healthy_active(self) -> None:
        timeline = _Timeline(10_000.0)
        verdicts = []
        for _ in range(40):
            timeline.progress_seq += 3
            verdicts.append(timeline.tick(owed=9_000.0).verdict)
            timeline.now += 60.0
        self.assertEqual(set(verdicts[1:]), {VitalityVerdict.HEALTHY_ACTIVE})
        assert timeline.episode is not None
        self.assertEqual(timeline.episode.last_progress_source, SnapshotSource.SUPERVISOR_JOURNAL.value)

    def test_an_open_quiet_turn_keeps_the_existing_ladder(self) -> None:
        timeline = _Timeline(10_000.0)
        timeline.tick(owed=9_000.0)
        timeline.now += 60.0
        timeline.progress_seq += 1
        timeline.tick(owed=9_000.0)  # first cursor comparison: the journal moved
        last_progress = timeline.now
        seen = []
        while timeline.now < last_progress + 1_200.0:
            timeline.now += 60.0
            seen.append((timeline.now - last_progress, timeline.tick(owed=9_000.0).verdict))
        suspect = DEFAULT_VITALITY_THRESHOLDS.suspect_after
        confirm = suspect + DEFAULT_VITALITY_THRESHOLDS.confirm_after
        for quiet, verdict in seen:
            expected = (
                VitalityVerdict.CONFIRMED_STALL
                if quiet >= confirm
                else VitalityVerdict.SUSPECTED_STALL
                if quiet >= suspect
                else VitalityVerdict.HEALTHY_QUIET
            )
            self.assertIs(verdict, expected, f"{quiet:.0f}s quiet")

    def test_a_head_that_owes_nothing_is_not_judged_by_the_rule(self) -> None:
        # The gate phase: the report is accepted, a finished worker at its prompt is expected.
        timeline, ended = self.idle_head(child_moves=True)
        timeline.now = ended + CONFIRM_IDLE + 60.0
        episode = timeline.tick(owed=0.0)
        self.assertEqual(episode.idle_turn_since, 0.0)
        self.assertNotIn("idle-turn", " ".join(episode.basis))
        self.assertIs(episode.verdict, VitalityVerdict.HEALTHY_ACTIVE)

    def test_a_retained_head_stays_retained_whatever_its_journal_says(self) -> None:
        timeline, _ended = self.idle_head(child_moves=False)
        for _ in range(20):
            timeline.now += 60.0
            self.assertIs(
                timeline.tick(owed=9_000.0, stopped=True, retained=True).verdict, VitalityVerdict.RETAINED
            )

    def test_the_idle_clock_starts_no_earlier_than_the_question(self) -> None:
        # A continued worker: its turn ended long before the continuation was delivered.
        timeline, ended = self.idle_head(child_moves=False)
        asked = ended + 3_600.0
        timeline.now = asked + SUSPECT_IDLE - 60.0
        episode = timeline.tick(owed=asked)
        self.assertIs(episode.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(episode.idle_turn_since, asked)
        timeline.now = asked + SUSPECT_IDLE
        self.assertIs(timeline.tick(owed=asked).verdict, VitalityVerdict.SUSPECTED_STALL)

    def test_a_nudge_restarts_the_idle_clock(self) -> None:
        timeline, ended = self.idle_head(child_moves=False)
        timeline.now = ended + SUSPECT_IDLE + 30.0
        self.assertIs(timeline.tick(owed=9_000.0).verdict, VitalityVerdict.SUSPECTED_STALL)
        assert timeline.episode is not None
        from dataclasses import replace

        timeline.episode = replace(timeline.episode, quiet_since=timeline.now)
        timeline.now += 60.0
        self.assertIs(timeline.tick(owed=9_000.0).verdict, VitalityVerdict.HEALTHY_QUIET)

    def test_a_turn_ending_never_launders_a_quiet_that_earned_suspicion(self) -> None:
        timeline = _Timeline(10_000.0)
        timeline.tick(owed=9_000.0)
        timeline.now += 60.0
        timeline.tick(owed=9_000.0)
        timeline.now += DEFAULT_VITALITY_THRESHOLDS.suspect_after + 60.0
        self.assertIs(timeline.tick(owed=9_000.0).verdict, VitalityVerdict.SUSPECTED_STALL)
        timeline.turn, timeline.turn_since = "idle", timeline.now
        timeline.now += 60.0
        self.assertIs(timeline.tick(owed=9_000.0).verdict, VitalityVerdict.SUSPECTED_STALL)

    def test_no_idle_verdict_before_the_first_turn(self) -> None:
        journal = _JournalDir(self, "run-a")
        journal.write(_clean_journal()[:2])
        now = 1_790_000_000.0 + 3 * CONFIRM_IDLE
        snapshots = [
            _pid(now),
            *snapshots_from_status({"supervisor_journal": journal.read()}, run_id="run-a", observed_at=now),
        ]
        self.assertIs(snapshots[1].availability, SourceAvailability.UNAVAILABLE)
        episode = reduce_vitality(None, snapshots, now, answer_owed_since=1_790_000_000.0, adapter="claude")
        self.assertIs(episode.verdict, VitalityVerdict.HEALTHY_QUIET)
        self.assertEqual(episode.idle_turn_since, 0.0)

    def test_the_outlook_names_the_idle_turn_deadlines(self) -> None:
        timeline, ended = self.idle_head(child_moves=False)
        timeline.now = ended + 60.0
        episode = timeline.tick(owed=9_000.0)
        outlook = recovery_outlook(episode, timeline.now)
        self.assertEqual(outlook["next_deadline"]["verdict"], VitalityVerdict.SUSPECTED_STALL.value)
        self.assertEqual(outlook["next_deadline"]["at"], ended + SUSPECT_IDLE)
        timeline.now = ended + SUSPECT_IDLE + 10.0
        episode = timeline.tick(owed=9_000.0)
        self.assertEqual(recovery_outlook(episode, timeline.now)["next_deadline"]["at"], ended + CONFIRM_IDLE)

    def test_the_episode_field_survives_serialisation_and_old_payloads_load(self) -> None:
        timeline, ended = self.idle_head(child_moves=False)
        timeline.now = ended + 60.0
        episode = timeline.tick(owed=9_000.0)
        self.assertEqual(VitalityEpisode.from_json(episode.to_json()), episode)
        old = episode.to_json()
        del old["idle_turn_since"]
        self.assertEqual(VitalityEpisode.from_json(old).idle_turn_since, 0.0)


# Every journal field the source reads, and every hostile value the card names.
_HOSTILE_VALUES: list[Any] = ["missing", None, True, "12", 1e300, -1, 10**400, math.nan, math.inf, [], {}]
_READ_FIELDS = ("kind", "seq", "run_id", "turn", "at", "schema_version")


def _hostile_line(record: dict[str, Any], field: str, value: Any) -> bytes:
    damaged = copy.deepcopy(record)
    if value == "missing":
        damaged.pop(field, None)
    else:
        damaged[field] = value
    # `allow_nan` writes NaN/Infinity as bare words, which `json.loads` reads back as floats.
    return (json.dumps(damaged, sort_keys=True, separators=(",", ":"), allow_nan=True) + "\n").encode()


class HostileJournalValueTests(unittest.TestCase):
    """The card's hostile-value table, end to end: file → source → snapshot → reducer.

    Each case damages one field of one record of an otherwise clean journal (or the file itself),
    and the head -- a `claude` run, so the idle-turn rule is live -- is judged an hour after its
    turn ended with an answer owed: the moment a believed idle reading would confirm a stall.
    Every field damaged is one the source reads, so the only accepted outcome is the unavailable
    snapshot (secretary-1739 round 2): never the clean journal's verdict, never an exception.
    """

    RUN = "run-a"

    def judge(self, reading: dict[str, Any]) -> tuple[VitalitySnapshot, VitalityEpisode]:
        now = 1_790_000_120.0 + 3_600.0
        journal = snapshots_from_status({"supervisor_journal": reading}, run_id=self.RUN, observed_at=now)[0]
        # Episode started now: the pid alone has aged nothing, so a stall can come from the journal only.
        episode = reduce_vitality(
            None, [_pid(now), journal], now, answer_owed_since=1_790_000_000.0, adapter="claude"
        )
        return journal, episode

    def assert_unavailable(self, raw: bytes) -> None:
        journal = _JournalDir(self, self.RUN)
        journal.path.write_bytes(raw)
        reading = journal.read()
        self.assertEqual(reading["state"], "unavailable", reading)
        snapshot, episode = self.judge(reading)
        self.assertIs(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertIs(snapshot.turn, TurnState.UNKNOWN)
        self.assertTrue(snapshot.reason)
        self.assertNotIn(
            episode.verdict,
            (VitalityVerdict.SUSPECTED_STALL, VitalityVerdict.CONFIRMED_STALL, VitalityVerdict.DEAD),
        )

    def test_the_clean_journal_is_what_a_believed_value_would_convict(self) -> None:
        clean_dir = _JournalDir(self, self.RUN)
        clean_dir.write(_clean_journal(self.RUN))
        clean, clean_episode = self.judge(clean_dir.read())
        self.assertIs(clean.turn, TurnState.IDLE)
        self.assertIs(clean_episode.verdict, VitalityVerdict.CONFIRMED_STALL)

    def test_the_reviewers_final_seq_rows(self) -> None:
        # The damaged final `turn.finished` of review 4: `read_tail` coerces both to an int, and
        # before round 2 both were read as an ordered idle window and confirmed a stall.
        records = _clean_journal(self.RUN)
        for name, value in (('seq "12"', "12"), ("seq 1e300", 1e300)):
            with self.subTest(name):
                raw = b"".join(_line(record) for record in records[:-1]) + _hostile_line(
                    records[-1], "seq", value
                )
                self.assert_unavailable(raw)

    def test_every_hostile_value_in_every_read_field(self) -> None:
        records = _clean_journal(self.RUN)
        for index, record in enumerate(records):
            for field in _READ_FIELDS:
                for value in _HOSTILE_VALUES:
                    damaged = _hostile_line(record, field, value)
                    if damaged == _line(record):
                        continue  # removing a field the record never had damages nothing
                    with self.subTest(record=record["kind"], field=field, value=repr(value)[:16]):
                        raw = b"".join(
                            damaged if position == index else _line(other)
                            for position, other in enumerate(records)
                        )
                        self.assert_unavailable(raw)

    def test_damaged_files_and_foreign_records(self) -> None:
        records = _clean_journal(self.RUN)
        clean_raw = b"".join(_line(record) for record in records)
        foreign = _record(7, "turn.started", 1_790_000_130.0, "run-b", turn=2)
        cases = {
            "invalid utf-8 line": clean_raw + b"\xc3\x28\xa0\xa1\n",
            "blank line": clean_raw + b"\n",
            "torn last line": clean_raw + b'{"kind":"turn.started","seq":7',
            "out-of-order seq": b"".join(_line(record) for record in [*records[:4], records[5], records[4]]),
            "repeated seq": clean_raw + _line({**records[-1], "kind": "input.accepted"}),
            "records of another run": clean_raw + _line(foreign),
            "another run only": b"".join(_line({**record, "run_id": "run-b"}) for record in records),
            "binary noise": bytes(range(256)) * 4,
            "empty": b"",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.assert_unavailable(raw)


def _worker_record(
    run_id: str, runtime: str = LOCAL_PTY_RUNTIME, adapter: str = "claude"
) -> DispatcherRecord:
    run = HeadRun(
        run_id=run_id,
        spec=HeadSpec(profile_id=f"{adapter}-local-pty", adapter=adapter, runtime=runtime),
        workspace="/tmp/ummanu-9739",
        task_ref=TaskRef.card(REF),
        role="worker",
        pid_file="/tmp/ummanu-9739/worker.pid",
    ).to_json()
    return DispatcherRecord(
        worker=f"{REF}-worker",
        workspace="/tmp/ummanu-9739",
        handle="",
        head="claude-opus-high-local-pty",
        review_head="claude-opus-high-local-pty",
        attempt_id="attempt-9739",
        comment_baseline=0,
        review_baseline=0,
        state="in_progress",
        claimed_at=0.0,
        worker_head_run=run,
    )


class _Host:
    """The real host's three reads for a supervised head: provider cursor, children, journal."""

    mode = "real"

    def __init__(self, run: dict, journal: _JournalDir | None, *, max_bytes: int = 16 * 1024) -> None:
        self.run = run
        self.journal = journal
        self.max_bytes = max_bytes
        self.cursor = "0:0"
        self.child_cpu = 0

    def provider_progress(self, _task, _record, _kind) -> dict[str, str]:
        run_id, fingerprint = head_run_binding(self.run)
        return {
            "state": "observed",
            "admission": "accepted",
            "source": "claude-session",
            "source_fingerprint": "e" * 32,
            "cursor": self.cursor,
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }

    def head_children(self, _pid: int) -> dict[str, Any]:
        return {
            "state": "observed",
            "uptime_ticks": 100,
            "total_cpu_ms": self.child_cpu,
            "total_io": 0,
            "descendant_count": 1,
            "descendants": [
                {"pid": 4242, "start": 50, "cpu_ms": self.child_cpu, "io": 0, "command": "helper"}
            ],
        }


class _JournalHost(_Host):
    def supervisor_journal(self, _task, record, _kind) -> dict[str, Any]:
        assert self.journal is not None
        return head_run_turn_reading(
            self.journal.root, record.worker_head_run["run_id"], max_bytes=self.max_bytes
        )


def _status(host: _Host, record: DispatcherRecord, *, stopped: bool = False) -> dict[str, Any]:
    heartbeat = {
        "known": True,
        "alive": True,
        "match": True,
        "state": "live-match",
        "stopped": stopped,
        "pid": 4000,
    }
    with mock.patch.object(dispatcher_review, "_head_run_process_status", return_value=heartbeat):
        return dispatcher_review.command_terminal_status(host, {"ref": REF}, record, kind="worker")


class TerminalStatusCarriesTheJournalTests(unittest.TestCase):
    def test_a_supervised_run_carries_its_journal_reading(self) -> None:
        record = _worker_record("run-a")
        journal = _JournalDir(self, "run-a")
        journal.write(_clean_journal())
        status = _status(_JournalHost(record.worker_head_run, journal), record)
        self.assertEqual(status["supervisor_journal"]["turn"], "idle")
        snapshot = snapshots_from_status(status, run_id="run-a", observed_at=time.time())
        self.assertIn(SnapshotSource.SUPERVISOR_JOURNAL, {item.source for item in snapshot})

    def test_a_reading_of_another_run_or_a_failed_probe_is_unavailable(self) -> None:
        record = _worker_record("run-a")

        class Foreign(_Host):
            def supervisor_journal(self, *_args) -> dict[str, Any]:
                return {
                    "state": "observed",
                    "run_id": "run-b",
                    "turn": "idle",
                    "turn_since": 1.0,
                    "progress_seq": 1,
                }

        class Raising(_Host):
            def supervisor_journal(self, *_args) -> dict[str, Any]:
                raise RuntimeError("boom")

        for host in (Foreign(record.worker_head_run, None), Raising(record.worker_head_run, None)):
            with self.subTest(host=type(host).__name__):
                status = _status(host, record)
                self.assertEqual(status["supervisor_journal"]["state"], "unavailable")

    def test_a_host_without_the_probe_and_a_legacy_run_carry_no_journal(self) -> None:
        record = _worker_record("run-a")
        self.assertNotIn("supervisor_journal", _status(_Host(record.worker_head_run, None), record))
        legacy = _worker_record("run-a", ORCA_LEGACY_RUNTIME)
        journal = _JournalDir(self, "run-a")
        journal.write(_clean_journal())
        self.assertNotIn("supervisor_journal", _status(_JournalHost(legacy.worker_head_run, journal), legacy))


class Ummanu1727ReplayTests(unittest.TestCase):
    """The incident, tick by tick, through the production status function, builders and reducer.

    Ticks run every 65 s (the dispatcher's cadence that night) from the first tick after the
    continuation, 23:35:37Z. The journal each tick reads is the fixture up to that instant, read
    through the real bounded tail with a 16 KiB window, so most readings are partial windows. The
    provider transcript moved until 23:43:07.612Z and never again before the nudge. The head's
    children are made to advance on every tick: the most child activity the incident could have
    had, and the shape that held the episode healthy.
    """

    def setUp(self) -> None:
        # The historical gzip deliberately omits progress records. Retention now makes those
        # original sequence gaps incomplete evidence. This reducer replay needs the known turn
        # timeline, so use an explicit consecutive-sequence projection of its retained records;
        # times, kinds and all verdict assertions stay unchanged. The gzip remains original
        # historical bytes; this projection is not a pristine/full production journal.
        self.records = [dict(record, seq=index) for index, record in enumerate(_fixture_records(), 1)]
        self.record = _worker_record(RUN_1727)

    def test_original_trimmed_fixture_exposes_interior_gaps(self) -> None:
        journal = _JournalDir(self, RUN_1727)
        journal.write(_fixture_records())
        from ummanu.runtime.head.local_pty.journal import read_events

        self.assertTrue(read_events(journal.path).partial_head)
        self.assertEqual([record["seq"] for record in self.records], list(range(1, len(self.records) + 1)))

    def replay(self, *, with_journal: bool, until: float) -> list[tuple[float, VitalityEpisode]]:
        journal = _JournalDir(self, RUN_1727)
        host: _Host = (
            _JournalHost(self.record.worker_head_run, journal)
            if with_journal
            else _Host(self.record.worker_head_run, journal)
        )
        episode: VitalityEpisode | None = None
        seen: list[tuple[float, VitalityEpisode]] = []

        def tick(now: float, *, stopped: bool = False, retained: bool = False, owed: float = 0.0) -> None:
            nonlocal episode
            journal.write([record for record in self.records if record["at"] <= now])
            host.cursor = f"transcript:{min(now, TRANSCRIPT_LAST)}"
            host.child_cpu += 2_000
            cursors = episode.evidence_cursors if episode is not None else {}
            snapshots = snapshots_from_status(
                _status(host, self.record, stopped=stopped),
                run_id=RUN_1727,
                previous_cursor=cursors.get(SnapshotSource.PROVIDER_CURSOR.value, ""),
                previous_child_cursor=cursors.get(SnapshotSource.EXECUTION_CHILD.value, ""),
                previous_child_key=episode.last_child_key if episode is not None else "",
                previous_journal_cursor=cursors.get(SnapshotSource.SUPERVISOR_JOURNAL.value, ""),
                observed_at=now,
            )
            episode = reduce_vitality(
                episode, snapshots, now, retained=retained, answer_owed_since=owed, adapter="claude"
            )
            seen.append((now, episode))

        # Retained (SIGSTOP) from 23:26:10Z while the reviewer ran: the gate/review path, nothing owed.
        now = 1790292370.0
        while now < RESUMED_AT:
            tick(now, stopped=True, retained=True)
            now += TICK
        # Resumed at 23:34:40Z: the wait tick for the worker report, owed from the resume.
        now = 1790292937.0  # 23:35:37Z
        while now <= until:
            tick(now, owed=RESUMED_AT)
            now += TICK
        return seen

    def test_the_idle_head_is_confirmed_within_confirm_idle_of_its_turn_end(self) -> None:
        seen = self.replay(with_journal=True, until=NUDGE_LANDED - 1.0)
        after_resume = [(now, episode) for now, episode in seen if now > RESUMED_AT]
        # Working: every tick of the open continuation turn is healthy.
        working = [episode.verdict for now, episode in after_resume if now <= TURN_4_FINISHED]
        self.assertEqual(set(working), {VitalityVerdict.HEALTHY_ACTIVE})
        for now, episode in after_resume:
            idle = now - TURN_4_FINISHED
            if idle < SUSPECT_IDLE:
                self.assertNotIn(
                    episode.verdict, (VitalityVerdict.SUSPECTED_STALL, VitalityVerdict.CONFIRMED_STALL), idle
                )
            elif idle < CONFIRM_IDLE:
                self.assertIs(episode.verdict, VitalityVerdict.SUSPECTED_STALL, idle)
            else:
                self.assertIs(episode.verdict, VitalityVerdict.CONFIRMED_STALL, idle)
        first_confirmed = next(
            now for now, episode in after_resume if episode.verdict is VitalityVerdict.CONFIRMED_STALL
        )
        self.assertLess(first_confirmed, TURN_4_FINISHED + CONFIRM_IDLE + TICK)
        final = after_resume[-1][1]
        self.assertEqual(final.confirmed_since, TURN_4_FINISHED + CONFIRM_IDLE)
        self.assertEqual(final.suspected_since, TURN_4_FINISHED + SUSPECT_IDLE)
        self.assertEqual(final.idle_turn_since, TURN_4_FINISHED)
        self.assertIn("child-not-holding-idle-turn", final.basis)

    def test_without_the_journal_child_activity_held_the_idle_head_healthy(self) -> None:
        # What happened that night: the same ticks with no Turn source read `healthy_active`
        # nineteen minutes into the silence (00:02:20Z), held by the head's children alone.
        seen = self.replay(with_journal=False, until=TURN_4_FINISHED + 19 * 60.0)
        silent = [episode for now, episode in seen if now > TURN_4_FINISHED + TICK]
        self.assertTrue(silent)
        self.assertEqual({episode.verdict for episode in silent}, {VitalityVerdict.HEALTHY_ACTIVE})
        self.assertIn("advancing@execution_child", silent[-1].basis)

    def test_the_nudge_that_lands_ends_the_idle_turn(self) -> None:
        seen = self.replay(with_journal=True, until=NUDGE_LANDED + 3 * TICK)
        after = [episode for now, episode in seen if now > NUDGE_LANDED + 5.0]
        self.assertTrue(after)
        self.assertEqual({episode.verdict for episode in after}, {VitalityVerdict.HEALTHY_ACTIVE})


class IdleTurnAdapterPremiseTests(unittest.TestCase):
    """Review 4's scenario: a turn closed on 2 s of pty quiet while a silent child keeps working.

    The supervisor closes a turn on quiet alone, so a head whose foreground child prints nothing
    reads `Idle`. That means "at its prompt" only for the TUIs verified to animate while a tool runs
    (`IDLE_TURN_ADAPTERS`); on any other adapter the child hold keeps the head healthy, as before.
    """

    def run_scenario(self, adapter: str) -> list[tuple[float, VitalityVerdict]]:
        base = 1_790_200_000.0
        journal = _JournalDir(self, "run-a")
        journal.write(
            [
                _record(1, "run.started", base),
                _record(2, "input.accepted", base + 5, subject="worker-launch"),
                _record(3, "turn.started", base + 5, turn=1),
                _record(4, "provider.progressed", base + 20, turn=1),
                # The foreground command starts and prints nothing: the turn closes 2 s later.
                _record(5, "turn.finished", base + 22, turn=1, reason="quiet"),
            ]
        )
        record = _worker_record("run-a", adapter=adapter)
        host = _JournalHost(record.worker_head_run, journal)
        declared = wait_vitality._run_adapter(record.worker_head_run)
        self.assertEqual(declared, adapter)
        episode: VitalityEpisode | None = None
        seen: list[tuple[float, VitalityVerdict]] = []
        now = base + 30.0
        while now < base + 22 + CONFIRM_IDLE + 300.0:
            host.child_cpu += 3_000  # the silent child burns CPU on every tick
            cursors = episode.evidence_cursors if episode is not None else {}
            snapshots = snapshots_from_status(
                _status(host, record),
                run_id="run-a",
                previous_cursor=cursors.get(SnapshotSource.PROVIDER_CURSOR.value, ""),
                previous_child_cursor=cursors.get(SnapshotSource.EXECUTION_CHILD.value, ""),
                previous_child_key=episode.last_child_key if episode is not None else "",
                previous_journal_cursor=cursors.get(SnapshotSource.SUPERVISOR_JOURNAL.value, ""),
                observed_at=now,
            )
            episode = reduce_vitality(episode, snapshots, now, answer_owed_since=base + 5, adapter=declared)
            seen.append((now - (base + 22), episode.verdict))
            now += 60.0
        return seen

    def test_the_verified_adapters_are_claude_and_codex(self) -> None:
        self.assertEqual(IDLE_TURN_ADAPTERS, frozenset({"claude", "codex"}))

    def test_another_adapter_stays_healthy_under_the_child_hold(self) -> None:
        seen = self.run_scenario("hermes")
        self.assertEqual({verdict for _, verdict in seen[1:]}, {VitalityVerdict.HEALTHY_ACTIVE}, seen)

    def test_the_same_journal_on_claude_is_confirmed(self) -> None:
        for idle, verdict in self.run_scenario("claude"):
            if idle >= CONFIRM_IDLE:
                self.assertIs(verdict, VitalityVerdict.CONFIRMED_STALL, idle)
            elif idle >= SUSPECT_IDLE:
                self.assertIs(verdict, VitalityVerdict.SUSPECTED_STALL, idle)

    def test_no_adapter_keeps_the_rule_off(self) -> None:
        timeline = _Timeline(10_000.0)
        timeline.adapter = ""
        timeline.tick(owed=9_000.0)
        timeline.turn, timeline.turn_since = "idle", timeline.now
        timeline.now += CONFIRM_IDLE + 60.0
        episode = timeline.tick(owed=9_000.0)
        self.assertEqual(episode.idle_turn_since, 0.0)
        self.assertNotIn("idle-turn", " ".join(episode.basis))


class ResumedWorkerThatWorksTests(unittest.TestCase):
    """A continued worker is not read as stalled: the delivery that resumes it restarts the clock."""

    def test_a_continuation_that_opens_a_turn_and_writes_progress_is_never_suspected(self) -> None:
        journal = _JournalDir(self, "run-a")
        record = _worker_record("run-a")
        host = _JournalHost(record.worker_head_run, journal)
        base = 1_790_100_000.0
        # Round 1 ended long ago; the head was retained, then continued at `resumed`.
        records = [
            _record(1, "run.started", base),
            _record(2, "input.accepted", base + 5, subject="worker-launch"),
            _record(3, "turn.started", base + 5, turn=1),
            _record(4, "turn.finished", base + 1_800, turn=1, reason="quiet"),
        ]
        resumed = base + 1_800 + 3_600
        records += [
            _record(5, "input.accepted", resumed, subject="worker-continuation"),
            _record(6, "turn.started", resumed, turn=2),
        ]
        seq = 7
        episode: VitalityEpisode | None = None
        now = resumed + 2.0
        verdicts = []
        while now < resumed + 45 * 60.0:
            # Progress lands every four minutes: below every quiet threshold, never on every tick.
            if int((now - resumed) // 240) >= seq - 7:
                records.append(_record(seq, "provider.progressed", now - 1.0, turn=2))
                seq += 1
            journal.write(records)
            cursors = episode.evidence_cursors if episode is not None else {}
            snapshots = snapshots_from_status(
                _status(host, record),
                run_id="run-a",
                previous_cursor=cursors.get(SnapshotSource.PROVIDER_CURSOR.value, ""),
                previous_child_cursor=cursors.get(SnapshotSource.EXECUTION_CHILD.value, ""),
                previous_journal_cursor=cursors.get(SnapshotSource.SUPERVISOR_JOURNAL.value, ""),
                observed_at=now,
            )
            episode = reduce_vitality(
                episode, snapshots, now, answer_owed_since=resumed + 1.0, adapter="claude"
            )
            verdicts.append(episode.verdict)
            now += 60.0
        self.assertNotIn(VitalityVerdict.SUSPECTED_STALL, verdicts)
        self.assertNotIn(VitalityVerdict.CONFIRMED_STALL, verdicts)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
