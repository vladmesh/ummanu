"""Sealed, observational attempt outcome occurrence contracts."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from ummanu.board.events import AnalyticsOutcomeConflict, BoardEventCanon
from ummanu.board.fake import MemoryAudit
from ummanu.board.models import Actor, EntityKind, Event, EventKind
from ummanu.board.terminal_taxonomy import normalize_terminal_taxonomy
from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.state import OutcomeTerminalPath
from ummanu.dispatch.types import HostError
from ummanu.tasks import TaskError


def outcome(*, disposition: str = "rework", effect: str = "evt-effect") -> Event:
    return Event(
        event_id="evt-outcome",
        kind=EventKind.ATTEMPT_OUTCOME,
        entity_kind=EntityKind.CARD,
        ref="ummanu-1532",
        actor=Actor("dispatcher", "dispatcher"),
        reason="confirmed terminal lifecycle effect",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        data={
            "version": 1,
            "attempt_id": "attempt-1",
            "attempt": 1,
            "report_generation": 1,
            "sprint_ref": "sprint:1419",
            "specification_revision": None,
            "terminal_state": "in_progress" if disposition == "rework" else "blocked",
            "verdict": "red",
            "disposition": disposition,
            "blocked_reason": "infrastructure" if disposition == "blocked" else None,
            "source_event_ids": {
                "report": None,
                "verdict": None,
                "decision": None,
                "effect": effect,
                "worker_usage": None,
                "review_usage": None,
            },
            "usage_completeness": {"worker": "missing", "review": "missing"},
        },
    )


class AttemptOutcomeTests(unittest.TestCase):
    def test_stage_recovery_and_exact_replay_have_one_natural_key(self) -> None:
        canon = BoardEventCanon(MemoryAudit())
        event = outcome()
        canon.stage("outcome-1", event)
        staged = canon.attempt_outcome_occurrences()
        self.assertEqual(len(staged), 1)
        self.assertTrue(staged[0].pending)
        canon.commit("outcome-1", event)
        canon.commit("outcome-1", event)
        committed = canon.attempt_outcome_occurrences()
        self.assertEqual(len(committed), 1)
        self.assertFalse(committed[0].pending)

    def test_conflicting_natural_key_is_a_named_diagnostic(self) -> None:
        canon = BoardEventCanon(MemoryAudit())
        first = outcome()
        canon.commit("outcome-1", first)
        second = outcome(effect="evt-other")
        second = Event(
            event_id="evt-outcome-other",
            kind=second.kind,
            entity_kind=second.entity_kind,
            ref=second.ref,
            actor=second.actor,
            reason=second.reason,
            occurred_at=second.occurred_at,
            data=second.data,
        )
        canon.commit("outcome-2", second)
        with self.assertRaises(AnalyticsOutcomeConflict):
            canon.attempt_outcome_occurrences()

    def test_unknown_version_and_missingness_are_rejected_by_event_reader(self) -> None:
        event = outcome()
        record = event.to_record("outcome-1")
        # v2 adds forward lineage; v3 remains unknown at the typed boundary.
        record["data"] = dict(record["data"], version=3)
        with self.assertRaisesRegex(ValueError, "unsupported attempt outcome version"):
            Event.from_record(record)
        record = event.to_record("outcome-1")
        record["data"] = dict(record["data"])
        record["data"]["usage_completeness"] = {"worker": "zero", "review": "missing"}
        with self.assertRaisesRegex(ValueError, "completeness"):
            Event.from_record(record)


class AttemptOutcomeLifecycleTests(DispatcherRuntimeFixture, unittest.TestCase):
    def _outcomes(self):
        return self.writer.board_host.canon.attempt_outcome_occurrences(ref=CARD_REF)

    def _start_worker_round(self):
        self.start_dispatcher()
        self.tick()
        payload = self.runtime.production_state.load()
        return payload, self.runtime.production_state.records(payload)[CARD_REF]

    def test_worker_report_blocked_seals_a_worker_only_key(self) -> None:
        self._start_worker_round()
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            classification="external_fact",
            body="worker cannot proceed",
            request_id=self._worker_report_request_id("blocked", "external_fact"),
        )

        blocked = self.tick()

        self.assertEqual(blocked["to"], "blocked")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual((occurrence["verdict"], occurrence["disposition"]), ("blocked", "blocked"))
        self.assertEqual(occurrence["blocked_reason"], "other")
        self.assertEqual(occurrence["usage_completeness"]["review"], "missing")
        self.assertEqual(occurrence["version"], 2)
        self.assertIsNotNone(occurrence["specification_revision"])
        self.assertIsNotNone(occurrence["source_event_ids"]["report"])
        self.assertIsNone(occurrence["source_event_ids"]["verdict"])
        self.assertIsNone(occurrence["source_event_ids"]["decision"])

    def test_worker_wrong_task_definition_maps_forward_to_task_contract(self) -> None:
        self._start_worker_round()
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            classification="wrong_task_definition",
            body="the contract contradicts itself",
            request_id=self._worker_report_request_id("blocked", "wrong_task_definition"),
        )

        self.tick()

        occurrence = self._outcomes()[0].event.data
        self.assertEqual(occurrence["blocked_reason"], "task_contract")
        effect = next(
            event
            for event in self.writer.board_host.canon.events(ref=CARD_REF)
            if event.kind.value == "card.blocked"
        )
        self.assertEqual(
            effect.data["terminal_taxonomy"],
            {
                "version": 2,
                "disposition": "blocked",
                "blocked_reason": "task_contract",
                "source_evidence": "wrong_task_definition",
                "budget_class": "blocked",
                "provenance": "forward",
            },
        )

    def test_malformed_taxonomy_does_not_gate_the_lifecycle_effect(self) -> None:
        _payload, record = self._start_worker_round()

        attempt_accounting.terminal_effect(self.runtime,
            self.reader.show(CARD_REF),
            record,
            target="blocked",
            reason="a lifecycle effect with malformed analytics input",
            request_id="malformed-taxonomy-effect",
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="not-a-taxonomy-reason",
        )

        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(self._outcomes(), ())

    def test_terminal_effect_never_repairs_or_writes_round_context(self) -> None:
        """A failed observational handoff cannot veto the lifecycle transaction."""
        _payload, record = self._start_worker_round()
        with mock.patch.object(
            attempt_accounting,
            "persist_outcome_round_context",
            side_effect=TaskError("audit_pending", "context journal unavailable", 4),
        ) as persist:
            effect = attempt_accounting.terminal_effect(self.runtime,
                self.reader.show(CARD_REF),
                record,
                target="blocked",
                reason="terminal lifecycle effect survives a context outage",
                request_id="context-outage-terminal-effect",
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="infrastructure",
            )
        persist.assert_not_called()
        self.assertTrue(effect["event_id"])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")

    def test_reviewed_release_after_adoption_uses_the_original_durable_round(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()  # gate green -> review launch, including its durable handoff
        payload = self.runtime.production_state.load()
        original = self.runtime.production_state.records(payload)[CARD_REF]
        verdict_request = self._review_verdict_request_id("green")
        self.runtime.production_state.put_records(payload, {})
        payload["attempt_id"] = "attempt-after-restart"
        self.runtime.production_state.save(payload)

        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="green after dispatcher restart",
            request_id=verdict_request,
        )
        self.assertEqual(self.tick()["to"], "assessment")
        self._decide("release", request_id="release-after-adoption")
        self.tick()

        outcome = self._outcomes()[0].event.data
        self.assertEqual(outcome["attempt_id"], original.attempt_id)
        self.assertEqual(outcome["report_generation"], original.report_generation)
        self.assertTrue(
            all(outcome["source_event_ids"][name] for name in ("report", "verdict", "decision", "effect"))
        )

    def test_done_then_red_gate_seals_rework_before_opening_the_next_round(self) -> None:
        self.start_dispatcher()
        self.host.gate_results = [GateResult("red", "test gate rejected the candidate")]
        self._run_worker_to_validate()

        reworked = self.tick()

        self.assertEqual(reworked["action"], "gate-red-rework")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual(
            (occurrence["terminal_state"], occurrence["verdict"], occurrence["disposition"]),
            ("in_progress", "red", "rework"),
        )
        self.assertIsNotNone(occurrence["source_event_ids"]["report"])
        self.assertIsNone(occurrence["source_event_ids"]["verdict"])
        self.assertIsNone(occurrence["source_event_ids"]["decision"])

    def test_verdictless_gate_termination_keeps_the_consumed_report_required(self) -> None:
        self._start_worker_round()
        self._report_done("gate will terminate this report round")
        self.assertEqual(self.tick()["to"], "validate")
        payload = self.runtime.production_state.load()
        record = self.runtime.production_state.records(payload)[CARD_REF]
        attempt_accounting.terminal_effect(self.runtime,
            self.reader.show(CARD_REF),
            record,
            target="blocked",
            reason="gate infrastructure terminal",
            request_id="verdictless-gate-terminal",
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="gate",
        )
        outcome = self._outcomes()[0].event.data
        self.assertTrue(outcome["lineage_required"]["report"])
        self.assertTrue(outcome["lineage_required"]["specification_revision"])
        self.assertIsNotNone(outcome["source_event_ids"]["report"])
        self.assertFalse(outcome["lineage_required"]["verdict"])
        self.assertFalse(outcome["lineage_required"]["decision"])

    def test_no_observer_green_release_seals_the_reviewed_round(self) -> None:
        self.start_dispatcher()
        self.board.save_metadata(12, {"sprint_ref": ""})
        self.sprints.rows.clear()
        self.board.clear_sprints()
        self._run_worker_to_validate()
        self.tick()  # gate green -> review started
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="green",
            request_id=self._review_verdict_request_id("green"),
        )

        released = self.tick()

        self.assertEqual(released["to"], "done")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual((occurrence["verdict"], occurrence["disposition"]), ("green", "release"))
        self.assertIsNotNone(occurrence["source_event_ids"]["report"])
        self.assertIsNotNone(occurrence["source_event_ids"]["verdict"])
        self.assertIsNone(occurrence["source_event_ids"]["decision"])

    def test_infrastructure_bringup_block_seals_the_claimed_round(self) -> None:
        self.start_dispatcher()
        self.host.fail_prepare_reason = "workspace is unavailable"

        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual(
            (occurrence["disposition"], occurrence["blocked_reason"]), ("blocked", "infrastructure")
        )

    def test_fanout_refusal_commits_its_lifecycle_effect_and_outcome(self) -> None:
        self.start_dispatcher()
        with mock.patch(
            "ummanu.dispatch.worker_launch._write_launch_intent",
            return_value="codex-fanout-policy: prohibited source",
        ):
            blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        occurrence = self._outcomes()[0].event.data
        self.assertEqual(occurrence["blocked_reason"], "provider")

    def test_committed_obligations_retire_before_the_next_tick(self) -> None:
        _payload, record = self._start_worker_round()
        effect = attempt_accounting.terminal_effect(self.runtime,
            self.reader.show(CARD_REF),
            record,
            target="blocked",
            reason="terminal for recovery retirement",
            request_id="outcome-retirement-effect",
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="infrastructure",
        )
        canon = self.writer.board_host.canon
        self.assertEqual(canon.attempt_outcome_effects(), ())

        with mock.patch.object(attempt_accounting, "_finish_attempt_outcome") as finish:
            self.assertEqual(attempt_accounting.publish_pending_attempt_outcomes(self.runtime), [])
            self.assertEqual(attempt_accounting.publish_pending_attempt_outcomes(self.runtime), [])
        finish.assert_not_called()
        self.assertEqual(self._outcomes()[0].event.data["source_event_ids"]["effect"], effect["event_id"])

    def test_crash_after_stage_reuses_the_pending_occurrence(self) -> None:
        _payload, record = self._start_worker_round()
        with mock.patch.object(
            self.writer,
            "_commit_attempt_outcome",
            side_effect=TaskError("audit_pending", "append interrupted", 4),
        ):
            effect = attempt_accounting.terminal_effect(self.runtime,
                self.reader.show(CARD_REF),
                record,
                target="blocked",
                reason="terminal before append",
                request_id="outcome-stage-crash",
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="infrastructure",
            )

        self.assertTrue(self._outcomes()[0].pending)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        attempt_accounting.publish_pending_attempt_outcomes(self.runtime)
        occurrence = self._outcomes()[0]
        self.assertFalse(occurrence.pending)
        self.assertEqual(occurrence.event.data["source_event_ids"]["effect"], effect["event_id"])

    def test_operator_move_without_a_round_identity_creates_no_outcome(self) -> None:
        self.writer.move(
            role="po",
            actor="operator",
            reference=CARD_REF,
            target="blocked",
            reason="operator stop before claim",
            request_id="operator-stop-no-round",
        )

        self.assertEqual(self._outcomes(), ())

    def test_unresumable_lost_claim_commits_blocked_without_inventing_a_key(self) -> None:
        payload, record = self._start_worker_round()

        blocked = self.runtime._block_unresumable(
            self.reader.show(CARD_REF),
            {},
            payload,
            record.attempt_id,
            "advance",
            RuntimeError("head vanished"),
        )

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(self._outcomes(), ())

    def test_reviewed_red_reslice_preserves_the_verdict_and_disposition(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        self._review_red()

        self._park_and_decide("reslice")

        outcome = self._outcomes()[0].event.data
        self.assertEqual((outcome["verdict"], outcome["disposition"]), ("red", "reslice"))
        self.assertEqual(outcome["terminal_state"], "blocked")
        self.assertTrue(outcome["lineage_required"]["decision"])
        self.assertTrue(all(outcome["source_event_ids"][name] for name in ("report", "verdict", "decision", "effect")))

    def test_reviewed_green_release_has_one_sealed_outcome(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="looks good",
            request_id=self._review_verdict_request_id("green"),
        )

        self._park_and_decide("release")

        outcomes = self._outcomes()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(
            (outcomes[0].event.data["verdict"], outcomes[0].event.data["disposition"]),
            ("green", "release"),
        )
        self.assertFalse(outcomes[0].pending)
        data = outcomes[0].event.data
        self.assertTrue(all(data["source_event_ids"][name] for name in ("report", "verdict", "decision", "effect")))
        self.assertTrue(data["lineage_required"]["specification_revision"])

    def test_second_reviewed_red_round_freezes_its_own_exact_sources(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        self._review_red()
        self._park_and_decide("rework", request_id="first-round-decision")

        self.host.commit = "second-round-c0ffee"
        self._report_done("second round done")
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()
        self._review_red()
        self.assertEqual(self.tick()["to"], "assessment")
        self._decide("rework", request_id="second-round-decision")
        with mock.patch.object(
            self.writer,
            "attempt_outcome",
            side_effect=TaskError("audit_pending", "append interrupted", 4),
        ):
            self.assertEqual(self.tick()["action"], "rework-started")
        self.assertEqual(len(self._outcomes()), 1)
        self.assertEqual(attempt_accounting.publish_pending_attempt_outcomes(self.runtime), [])

        outcomes = self._outcomes()
        self.assertEqual(len(outcomes), 2)
        first, second = (occurrence.event.data for occurrence in outcomes)
        self.assertNotEqual(first["source_event_ids"]["verdict"], second["source_event_ids"]["verdict"])
        self.assertNotEqual(first["source_event_ids"]["decision"], second["source_event_ids"]["decision"])
        self.assertTrue(all(second["source_event_ids"][name] for name in ("report", "verdict", "decision", "effect")))

    def test_reviewed_release_uses_the_persisted_verdict_context_not_a_mutable_baseline(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="green",
            request_id=self._review_verdict_request_id("green"),
        )
        self.assertEqual(self.tick()["to"], "assessment")

        payload = self.runtime.production_state.load()
        record = self.runtime.production_state.records(payload)[CARD_REF]
        record.review_baseline += 99
        self.runtime.production_state.put_records(payload, {CARD_REF: record})
        self.runtime.production_state.save(payload)

        self._decide("release", request_id="release-after-baseline-loss")
        self.tick()
        outcome = self._outcomes()[0].event.data
        self.assertIsNotNone(outcome["source_event_ids"]["verdict"])
        self.assertTrue(outcome["lineage_required"]["verdict"])

    def test_missing_forward_report_commits_an_incomplete_row_with_a_named_obligation_diagnostic(self) -> None:
        self._start_worker_round()
        report_id = self._worker_report_request_id("blocked", "external_fact")
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            classification="external_fact",
            body="worker cannot proceed",
            request_id=report_id,
        )
        canon = self.writer.board_host.canon
        original = canon.committed
        with mock.patch.object(
            canon,
            "committed",
            side_effect=lambda request_id: None if request_id == report_id else original(request_id),
        ):
            self.tick()

        outcome = self._outcomes()[0].event.data
        self.assertIsNone(outcome["source_event_ids"]["report"])
        self.assertTrue(outcome["lineage_required"]["report"])
        effect = next(event for event in canon.events(ref=CARD_REF) if event.kind.value == "card.blocked")
        self.assertEqual(effect.data["attempt_outcome_owed"]["lineage_diagnostic"], "attempt_outcome_lineage_missing_report")

    def test_adopted_record_keeps_the_report_required_on_a_verdictless_gate_terminal(self) -> None:
        """A lost state file must not turn a round that reported into a no-report path."""
        self.start_dispatcher()
        self._run_worker_to_validate()
        payload = self.runtime.production_state.load()
        original = self.runtime.production_state.records(payload)[CARD_REF]
        self.runtime.production_state.put_records(payload, {})
        payload["attempt_id"] = "attempt-after-state-loss"
        self.runtime.production_state.save(payload)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")

        with mock.patch.object(
            self.host, "gate_check", side_effect=HostError("gate infrastructure is unavailable")
        ):
            blocked = self.tick()

        self.assertEqual((blocked["status"], blocked["step"]), ("blocked", "gate"))
        outcome = self._outcomes()[0].event.data
        self.assertEqual(outcome["attempt_id"], original.attempt_id)
        self.assertTrue(outcome["lineage_required"]["report"])
        self.assertTrue(outcome["lineage_required"]["specification_revision"])
        self.assertIsNotNone(outcome["source_event_ids"]["report"])
        self.assertFalse(outcome["lineage_required"]["verdict"])
        self.assertFalse(outcome["lineage_required"]["decision"])

    def test_adoption_before_any_report_stays_a_no_report_path(self) -> None:
        """The same rule must not invent a report requirement for a round that never reported."""
        self._start_worker_round()
        payload = self.runtime.production_state.load()
        self.runtime.production_state.put_records(payload, {})
        payload["attempt_id"] = "attempt-after-state-loss"
        self.runtime.production_state.save(payload)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")

        adopted = self.runtime._adopt(self.reader.show(CARD_REF), "attempt-after-state-loss")

        self.assertIs(adopted.outcome_terminal_path, OutcomeTerminalPath.NO_ACCEPTED_REPORT)

    def test_missing_report_handoff_stays_required_for_reviewer_and_post_gate_terminals(self) -> None:
        """One persisted path classification covers every later terminal caller."""
        self._start_worker_round()
        self._report_done("the report handoff will be unavailable")
        with mock.patch.object(attempt_accounting, "capture_outcome_source"):
            self.assertEqual(self.tick()["to"], "validate")

        payload = self.runtime.production_state.load()
        record = self.runtime.production_state.records(payload)[CARD_REF]
        self.assertIs(record.outcome_terminal_path, OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT)
        for terminal in ("review-launch", "review-wait", "post-gate"):
            with self.subTest(terminal=terminal):
                obligation = attempt_accounting._attempt_outcome_obligation(self.runtime,
                    self.reader.show(CARD_REF),
                    record,
                    terminal_state="blocked",
                    taxonomy=normalize_terminal_taxonomy(disposition="blocked", blocked_reason="provider"),
                    terminal_path=record.outcome_terminal_path,
                )
                assert obligation is not None
                self.assertTrue(obligation["lineage_required"]["specification_revision"])
                self.assertTrue(obligation["lineage_required"]["report"])
                self.assertIsNone(obligation["source_event_ids"]["report"])
                self.assertEqual(obligation["lineage_diagnostic"], "attempt_outcome_lineage_missing_report")

        attempt_accounting.terminal_effect(self.runtime,
            self.reader.show(CARD_REF),
            record,
            target="blocked",
            reason="reviewer launch failed after an unavailable report handoff",
            request_id="post-report-lineage-terminal",
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="provider",
        )
        outcome = self._outcomes()[0].event.data
        self.assertTrue(outcome["lineage_required"]["report"])
        self.assertIsNone(outcome["source_event_ids"]["report"])

    def test_divergent_specification_boundary_is_an_incomplete_outcome_not_a_permanent_debt(self) -> None:
        self._start_worker_round()
        self.board.update(12, description="edited outside audited task operations")
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            classification="external_fact",
            body="worker cannot proceed",
            request_id=self._worker_report_request_id("blocked", "external_fact"),
        )

        self.tick()

        outcome = self._outcomes()[0].event.data
        self.assertIsNone(outcome["specification_revision"])
        self.assertIsNotNone(outcome["source_event_ids"]["report"])
        self.assertTrue(outcome["lineage_required"]["specification_revision"])

    def test_pre_v2_report_is_an_honestly_absent_upgrade_window_source(self) -> None:
        self._start_worker_round()
        report_id = self._worker_report_request_id("blocked", "external_fact")
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            classification="external_fact",
            body="worker cannot proceed",
            request_id=report_id,
        )
        canon = self.writer.board_host.canon
        original = canon.committed
        current = original(report_id)
        assert current is not None
        legacy = Event(
            event_id=current.event_id,
            kind=current.kind,
            entity_kind=current.entity_kind,
            ref=current.ref,
            actor=current.actor,
            reason=current.reason,
            occurred_at=current.occurred_at,
            data={key: value for key, value in current.data.items() if key != "specification_revision"},
        )
        with (
            mock.patch.object(
                canon,
                "committed",
                side_effect=lambda request_id: legacy if request_id == report_id else original(request_id),
            ),
            mock.patch.object(
                self.writer,
                "attempt_outcome",
                side_effect=TaskError("audit_pending", "append interrupted", 4),
            ),
        ):
            self.tick()
        self.assertEqual(self._outcomes(), ())
        self.assertEqual(attempt_accounting.publish_pending_attempt_outcomes(self.runtime), [])

        outcome = self._outcomes()[0].event.data
        self.assertIsNone(outcome["source_event_ids"]["report"])
        self.assertTrue(outcome["lineage_required"]["report"])

    def test_append_failure_after_a_terminal_effect_is_recovered_without_lifecycle_work(self) -> None:
        """The outcome journal is weaker than the transition it observes."""
        self.start_dispatcher()
        self.tick()  # Claim and start the first durable worker round.
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        record = records[CARD_REF]
        task = self.reader.show(CARD_REF)

        with mock.patch.object(
            self.writer,
            "attempt_outcome",
            side_effect=TaskError("audit_pending", "append refused", 4),
        ):
            effect = attempt_accounting.terminal_effect(self.runtime,
                task,
                record,
                target="blocked",
                reason="test terminal effect",
                request_id="attempt-outcome-terminal-effect",
                terminal_state="blocked",
                disposition="blocked",
                verdict="blocked",
                blocked_reason="infrastructure",
            )

        self.assertTrue(effect["event_id"])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        # This is what the normal terminal handler does after the confirmed
        # effect. Its success is independent of analytics publication.
        records.pop(CARD_REF)
        self.runtime.production_state.put_records(payload, records)
        self.runtime.production_state.save(payload)

        recovered = attempt_accounting.publish_pending_attempt_outcomes(self.runtime)

        self.assertEqual(recovered, [])
        occurrences = self.writer.board_host.canon.attempt_outcome_occurrences(ref=CARD_REF)
        self.assertEqual(len(occurrences), 1)
        self.assertFalse(occurrences[0].pending)
        self.assertEqual(occurrences[0].event.data["source_event_ids"]["effect"], effect["event_id"])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(self.runtime.production_state.records(payload), {})
