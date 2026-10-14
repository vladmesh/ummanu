from __future__ import annotations

import copy
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from ummanu.board.audit_contract import require_claim
from ummanu.dispatch import gate_attestation, gate_lifecycle
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import (
    AcceptedGreenGate,
    GateReceipt,
    mint_gate_receipt,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.tasks import TaskError


class AttestationEffectTests(unittest.TestCase):
    """Storage-free caller tests with strict audit ownership, never a request-exists fake."""

    def setUp(self) -> None:
        self.ref = "secretary-1883"
        self.record = DispatcherRecord(
            worker="worker", workspace="/fixture",
            handle="worker", head="worker", review_head="reviewer",
            attempt_id="attempt-20260930T211155Z-9b578122f7db", comment_baseline=3,
            review_baseline=4, state="reviewing", claimed_at=1,
        )
        self.events = {}
        self.comments = []
        self.saves = []
        self.now = datetime(2026, 9, 30, 21, 35, 32, tzinfo=UTC)
        self.runtime = SimpleNamespace(
            owner="ummanu-production",
            audit=SimpleNamespace(committed_event=self.events.get, pending_event=lambda _: None),
            reader=SimpleNamespace(show=lambda _: {"comments": self.comments}),
            writer=SimpleNamespace(comment=self.comment),
            host=SimpleNamespace(head_commit=lambda _: self.sha, mode="real"),
            save_records=lambda payload, records: self.saves.append(self.record.to_json()),
        )
        self.sha = "a" * 40
        self.mode = "github"
        self.enterContext(mock.patch.object(gate_lifecycle, "_validation_ci", side_effect=lambda *_: self.mode))

    def receipt(self) -> dict[str, object]:
        with mock.patch("ummanu.dispatch.gate_receipt.datetime") as clock:
            clock.now.return_value = self.now
            receipt = mint_gate_receipt(
                validated_sha=self.sha, base_sha="b" * 40, gate_mode=self.mode,
                required_checks=[{"name": "test / unit", "conclusion": "SUCCESS", "url": "https://ci.invalid/1"}],
                check_set_identity="complete check rollup",
            )
        assert receipt is not None
        return receipt

    def comment(self, *, role, actor, reference, body, request_id):
        identity = {"marker": role, "body_sha256": gate_attestation.digest(body)}
        if request_id in self.events:
            require_claim(self.events[request_id], kind="commented", reference=reference, identity=identity)
            return {"replayed": True}
        # Prove write-ahead persistence at the actual strict writer boundary.
        if "gate-attestation-stuck" not in request_id:
            self.assertTrue(any(effect["body"] == body and effect["request_id"] == request_id
                                for effect in self.saves[-1]["gate_attestation_effects"].values()))
        event = {"request_id": request_id, "event_id": f"evt_{len(self.events)}",
                 "kind": "commented", "ref": reference, "actor": {"role": role, "id": actor},
                 "outcome": "success", "payload": identity}
        self.events[request_id] = event
        self.comments.append({"body": "[dispatcher]\n" + body, "marker": role})
        return {"replayed": False}

    def accept(self, receipt=None, *, stage="assessment", reconciliation=None):
        return gate_lifecycle.accept_green_gate(
            self.runtime, {"ref": self.ref}, self.record, {self.ref: self.record}, {},
            self.record.attempt_id, GateResult("green", "CI", attestation=receipt or self.receipt()),
            stage=stage, e2e_reconciliation=reconciliation,
        )

    def reload(self):
        self.record = DispatcherRecord.from_json(json.loads(json.dumps(self.saves[-1])))

    def context(self, *, stage="assessment", reconciliation=None):
        return gate_attestation.delivery_context(
            self.record, ref=self.ref, owner=self.runtime.owner,
            attempt_id=self.record.attempt_id, stage=stage, e2e_reconciliation=reconciliation,
        )

    def test_original_bytes_survive_later_observation_and_restart_at_both_call_sites(self):
        for stage in ("assessment", "release"):
            with self.subTest(stage=stage):
                self.assertIsNone(self.accept(stage=stage))
                before = copy.deepcopy((self.comments, self.events))
                self.now += timedelta(seconds=3)
                self.reload()
                self.assertIsNone(self.accept(stage=stage))
                self.assertEqual((self.comments, self.events), before)
                self.assertEqual(self.record.gate_attestation["completed_at"], self.now.isoformat())
        self.assertEqual(len(self.comments), 2)

    def test_actual_1883_legacy_bytes_and_audit_are_adopted_with_overwritten_timestamp(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/gate_attestation_1883.json").read_text())
        event = fixture["event"]
        # These bytes were committed by the dispatcher owner of their time, under the product's
        # earlier name; the replay runs as that owner.
        self.runtime.owner = event["actor"]["id"]
        self.events[event["request_id"]] = copy.deepcopy(event)
        self.comments.append({"marker": "dispatcher", "body": "[dispatcher]\n" + fixture["body"]})
        self.sha = fixture["receipt"]["validated_sha"]
        self.record.review_commit = self.sha
        self.record.gate_attestation = fixture["receipt"]
        self.assertIsNone(self.accept(fixture["receipt"]))
        effect = next(iter(self.record.gate_attestation_effects.values()))
        self.assertEqual(effect["request_id"], event["request_id"])
        self.assertEqual(effect["body"], fixture["body"])
        self.assertEqual(effect["receipt"]["completed_at"], "2026-09-30T21:35:32+00:00")
        self.reload()
        self.assertIsNone(self.accept(fixture["receipt"]))
        self.assertEqual(self.events[event["request_id"]], event)
        self.assertEqual(len(self.comments), 1)

    def test_each_admitted_semantic_or_delivery_change_has_distinct_repeatable_effect(self):
        first = self.receipt()
        context = self.context()
        original = GateReceipt.accept(first, current_sha=self.sha)
        assert original is not None
        original_id = gate_attestation.semantic_identity(original, context)
        changes = [
            {"validated_sha": "c" * 40}, {"base_sha": "c" * 40}, {"gate_mode": "local"},
            {"command_or_check_set_digest": "d" * 64},
            *({"required_checks": [{**first["required_checks"][0], key: value}]}
              for key, value in (("name", "different"), ("conclusion", "NEUTRAL"), ("url", "https://ci.invalid/2"))),
            {"required_checks": first["required_checks"] + [{"name": "extra", "conclusion": "SUCCESS", "url": ""}]},
        ]
        for change in changes:
            with self.subTest(change=change):
                payload = {**first, **change}
                receipt = GateReceipt.accept(payload, current_sha=payload["validated_sha"])
                assert receipt is not None
                identity = gate_attestation.semantic_identity(receipt, context)
                self.assertNotEqual(identity, original_id)
                self.sha, self.mode = payload["validated_sha"], payload["gate_mode"]
                self.assertIsNone(self.accept(payload))
                before = copy.deepcopy((self.comments, self.events))
                self.reload()
                self.assertIsNone(self.accept({**payload, "completed_at": "2026-09-30T23:00:00+00:00"}))
                self.assertEqual((self.comments, self.events), before)
        for key, value in (("stage", "release"), ("review_baseline", 5), ("comment_baseline", 4),
                           ("report_generation", 2), ("review_commit", "d" * 40),
                           ("review_reconciliation", {"base_sha": "c" * 40}),
                           ("e2e_reconciliation", {"base_sha": "c" * 40})):
            with self.subTest(context=key):
                self.assertNotEqual(gate_attestation.semantic_identity(original, {**context, key: value}), original_id)

    def test_conflicts_have_safe_durable_diagnostics_and_escalate_once_at_three(self):
        receipt = GateReceipt.accept(self.receipt(), current_sha=self.sha)
        assert receipt is not None
        request_id = gate_attestation.effect_request(self.context(), gate_attestation.semantic_identity(receipt, self.context()))
        self.events[request_id] = {"kind": "moved", "ref": "another-card", "event_id": "foreign-event",
                                   "request_id": request_id, "actor": {"role": "owner", "id": "owner"},
                                   "payload": {"body": "secret transcript"}, "outcome": "success"}
        for count in range(1, 6):
            outcome = self.accept()
            self.assertEqual(outcome["attestation_failure"]["count"], min(count, 3))
            self.assertEqual(outcome["attestation_failure"]["request_id"], request_id)
            self.assertEqual(outcome["attestation_failure"]["committed"],
                             {"kind": "moved", "ref": "another-card", "event_id": "foreign-event"})
            self.assertNotIn("secret transcript", json.dumps(outcome))
            self.assertEqual(len(self.comments), int(count >= 3))
            self.reload()
        # A newly admitted check run has its own effect, without changing the conflicting audit.
        changed = self.receipt()
        changed["required_checks"] = [{"name": "test / unit", "conclusion": "SUCCESS", "url": "https://ci.invalid/2"}]
        self.assertIsNone(self.accept(changed))
        self.assertEqual(self.record.gate_attestation_failure, {})
        self.reload()
        self.assertEqual(self.record.gate_attestation_failure, {})

    def test_legacy_receipt_drift_selects_a_distinct_full_semantic_effect(self):
        receipt = GateReceipt.accept(self.receipt(), current_sha=self.sha)
        assert receipt is not None
        context = self.context()
        request_id = gate_attestation.legacy_request(receipt, context)
        body = gate_attestation.render_body(receipt, context)
        event = {"request_id": request_id, "event_id": "original-event", "kind": "commented",
                 "ref": self.ref, "actor": {"role": "dispatcher", "id": self.runtime.owner},
                 "outcome": "success", "payload": {"marker": "dispatcher", "body_sha256": gate_attestation.digest(body)}}
        self.events[request_id] = copy.deepcopy(event)
        self.comments.append({"marker": "dispatcher", "body": body})
        changed = {**receipt.as_dict(), "base_sha": "c" * 40,
                   "command_or_check_set_digest": receipt.command_or_check_set_digest[:12] + "f" * 52}
        self.assertIsNone(self.accept(changed))
        self.assertEqual(self.events[request_id], event)
        effect = next(iter(self.record.gate_attestation_effects.values()))
        self.assertNotEqual(effect["request_id"], request_id)
        before = copy.deepcopy((self.comments, self.events))
        self.reload()
        self.assertIsNone(self.accept({**changed, "completed_at": "2026-10-01T00:00:00+00:00"}))
        self.assertEqual((self.comments, self.events), before)

    def test_legacy_missing_ambiguous_unreadable_or_foreign_evidence_fails_closed(self):
        receipt = GateReceipt.accept(self.receipt(), current_sha=self.sha)
        assert receipt is not None
        request_id = gate_attestation.legacy_request(receipt, self.context())
        body = gate_attestation.render_body(receipt, self.context())
        valid = {"kind": "commented", "ref": self.ref, "request_id": request_id,
                 "event_id": "original-event", "outcome": "success",
                 "actor": {"role": "dispatcher", "id": self.runtime.owner},
                 "payload": {"marker": "dispatcher", "body_sha256": gate_attestation.digest(body)}}
        cases = [({}, []), ({}, [body, body]), ({"kind": "moved"}, [body]),
                 ({"ref": "foreign"}, [body]), ({"actor": {"role": "dispatcher", "id": "foreign"}}, [body]),
                 ({"payload": {"marker": "worker", "body_sha256": gate_attestation.digest(body)}}, [body]),
                 ({"payload": {"marker": "dispatcher", "body_sha256": "f" * 64}}, [body]),
                 ({}, [body.replace("Assessment delivery", "unrelated delivery")])]
        for change, bodies in cases:
            with self.subTest(change=change, count=len(bodies)):
                self.record.gate_attestation_failure = {}
                event = {**valid, **change}
                if bodies and bodies[0] != body:
                    event["payload"] = {"marker": "dispatcher", "body_sha256": gate_attestation.digest(bodies[0])}
                self.events[request_id] = event
                self.comments[:] = [{"marker": "dispatcher", "body": item} for item in bodies]
                outcome = self.accept()
                self.assertEqual(outcome["action"], "gate-attestation-refused")
                self.assertFalse(self.record.gate_attestation_effects)
        with mock.patch.object(self.runtime.reader, "show", side_effect=OSError("sensitive backend detail")):
            outcome = self.accept()
            self.assertIn("readability", outcome["reason"])
            self.assertNotIn("sensitive", json.dumps(outcome))

    def test_unreadable_audit_or_staged_legacy_effect_never_authorizes_delivery(self):
        with mock.patch.object(self.runtime.audit, "committed_event", side_effect=OSError("sensitive audit detail")):
            outcome = self.accept()
        self.assertIn("audit readability", outcome["reason"])
        self.assertNotIn("sensitive", json.dumps(outcome))
        self.assertFalse(self.comments)
        self.record.gate_attestation_failure = {}
        with mock.patch.object(self.runtime.audit, "pending_event", return_value={"kind": "commented", "ref": self.ref}):
            outcome = self.accept()
        self.assertIn("staged legacy request", outcome["reason"])
        self.assertFalse(self.comments)

    def test_legacy_release_reconciliation_is_verified_and_new_facts_select_new_effect(self):
        receipt = GateReceipt.accept(self.receipt(), current_sha=self.sha)
        assert receipt is not None
        facts = {"reviewed_sha": "a" * 40, "head_sha": "a" * 40,
                 "base_sha": "b" * 40, "reviewed_paths": 3}
        self.record.review_reconciliation = facts
        context = self.context(stage="release", reconciliation=facts)
        body = gate_attestation.render_body(receipt, context)
        request_id = gate_attestation.legacy_request(receipt, context)
        event = {"request_id": request_id, "event_id": "release-original", "kind": "commented",
                 "ref": self.ref, "actor": {"role": "dispatcher", "id": self.runtime.owner},
                 "outcome": "success", "payload": {"marker": "dispatcher", "body_sha256": gate_attestation.digest(body)}}
        self.events[request_id] = event
        self.comments.extend([{"marker": "review:green", "body": "review context"}] * self.record.review_baseline)
        self.comments.append({"marker": "dispatcher", "body": body})
        self.assertIsNone(self.accept(receipt.as_dict(), stage="release", reconciliation=facts))
        effect = next(iter(self.record.gate_attestation_effects.values()))
        self.assertEqual(effect["request_id"], request_id)
        changed = {**facts, "base_sha": "c" * 40}
        self.record.review_reconciliation = changed
        self.assertIsNone(self.accept(receipt.as_dict(), stage="release", reconciliation=changed))
        self.assertEqual(self.events[request_id], event)
        self.assertEqual(len(self.record.gate_attestation_effects), 2)

    def test_writer_claim_race_preserves_effect_and_recovery_clears_episode(self):
        strict = self.runtime.writer.comment
        with mock.patch.object(self.runtime.writer, "comment", side_effect=TaskError("validation", "sensitive body", 2)):
            outcome = self.accept()
        self.assertEqual(outcome["action"], "gate-attestation-refused")
        self.assertNotIn("sensitive", json.dumps(outcome))
        self.reload()
        self.now += timedelta(seconds=3)
        self.runtime.writer.comment = strict
        self.assertIsNone(self.accept())
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.record.gate_attestation_failure, {})

    def test_frozen_effect_never_bypasses_fresh_full_admission(self):
        self.assertIsNone(self.accept())
        original = copy.deepcopy((self.comments, self.events))
        receipt = self.receipt()
        changes = [{"validated_sha": "c" * 40}, {"base_sha": "bad"},
                   {"required_checks": []}, {"command_or_check_set_digest": "bad"},
                   *({"required_checks": [{"name": "test / unit", "conclusion": result, "url": ""}]}
                     for result in ("FAILURE", "PENDING", "RED"))]
        for change in changes:
            with self.subTest(change=change), mock.patch.object(
                gate_lifecycle.release_lifecycle, "block_merge_path", return_value={"blocked": True},
            ) as block:
                self.assertEqual(self.accept({**receipt, **change}), {"blocked": True})
                block.assert_called_once()
                self.assertEqual((self.comments, self.events), original)

    def test_reconciliation_changes_freeze_distinct_release_bodies(self):
        receipt = self.receipt()
        facts = {"reviewed_sha": "a" * 40, "head_sha": "a" * 40,
                 "base_sha": "b" * 40, "reviewed_paths": 3}
        for source in ("review", "e2e"):
            for key, value in (("reviewed_sha", "c" * 40), ("head_sha", "c" * 40),
                               ("base_sha", "c" * 40), ("reviewed_paths", 4)):
                with self.subTest(source=source, key=key):
                    self.record.review_reconciliation = facts if source == "review" else None
                    e2e = facts if source == "e2e" else None
                    self.assertIsNone(self.accept(receipt, stage="release", reconciliation=e2e))
                    context = self.context(stage="release", reconciliation=e2e)
                    before_id = gate_attestation.semantic_identity(GateReceipt.accept(receipt, current_sha=self.sha), context)
                    changed = {**facts, key: value}
                    self.record.review_reconciliation = changed if source == "review" else None
                    e2e = changed if source == "e2e" else None
                    changed_context = self.context(stage="release", reconciliation=e2e)
                    self.assertNotEqual(before_id, gate_attestation.semantic_identity(
                        GateReceipt.accept(receipt, current_sha=self.sha), changed_context))
                    self.assertIsNone(self.accept(receipt, stage="release", reconciliation=e2e))
                    original = copy.deepcopy((self.comments, self.events))
                    self.reload()
                    # The real release admission recalculates these transient facts each tick.
                    self.record.review_reconciliation = changed if source == "review" else None
                    self.assertIsNone(self.accept({**receipt, "completed_at": "2026-10-01T00:00:00+00:00"},
                                                  stage="release", reconciliation=e2e))
                    self.assertEqual((self.comments, self.events), original)


class AttestationLifecycleTests(DispatcherRuntimeFixture, unittest.TestCase):
    """CI-only SQL-backed GREEN review, strict writer and durable park regressions."""

    def prepare_green_review(self):
        self.start_dispatcher()
        self.catalog._adapter = {"validation": {"ci": "github"}}
        self.host.commit = "a" * 40
        self.now = datetime(2026, 9, 30, 21, 35, 32, tzinfo=UTC)
        self.enterContext(mock.patch.object(self.host, "gate_check", side_effect=self.fresh_gate))
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self.writer.verdict(role="reviewer", actor="reviewer", reference=CARD_REF,
                            kind="green", body="independent GREEN review",
                            request_id=self._review_verdict_request_id("green"))

    def fresh_gate(self, task, record):
        with mock.patch("ummanu.dispatch.gate_receipt.datetime") as clock:
            clock.now.return_value = self.now
            receipt = mint_gate_receipt(
                validated_sha=self.host.commit, base_sha="b" * 40, gate_mode="github",
                required_checks=[{"name": "test / unit", "conclusion": "SUCCESS", "url": "https://ci.invalid/1"}],
                check_set_identity="complete rollup",
            )
        return GateResult("green", "executed checks", attestation=receipt)

    def attestation_events(self):
        return [event for event in self.writer.audit.events(CARD_REF)
                if event.get("kind") == "commented"
                and "-gate-attestation-assessment-" in event.get("request_id", "")]

    def assert_parked_once(self, original):
        self.assertEqual(self.reader.show(CARD_REF)["state"], "assessment")
        self.assertEqual(self.attestation_events(), original)
        self.assertEqual(len(original), 1)
        comments = [item for item in self.reader.show(CARD_REF)["comments"]
                    if "Mechanical gate attestation" in item["body"]]
        self.assertEqual(len(comments), 1)
        from ummanu.board.audit_contract import card_transition_of

        transitions = [event for event in self.writer.audit.events(CARD_REF)
                       if (card_transition_of(event) or ("", ""))[1] == "assessment"]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(self.host.completed, [])
        self.assertEqual(self.host.resumed_workers, [])
        self.assertEqual(self.host.reviews, [CARD_REF])

    def test_stop_refusal_then_later_observation_parks_with_original_committed_effect(self):
        self.prepare_green_review()
        self.host.fail_stop_review_reason = "scope unit was not loaded; settlement unconfirmed"
        for _ in range(3):
            self.assertEqual(self.tick()["action"], "review-stop-unconfirmed")
            self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")
            self.assertEqual(self._pilot_record()["worker_continuation"]["stage"], "retained")
            self.assertTrue(self._pilot_record()["worker_continuation"]["session_held"])
            self.now += timedelta(seconds=3)
        original = copy.deepcopy(self.attestation_events())
        self.host.fail_stop_review_reason = ""
        self.assertEqual(self.tick()["to"], "assessment")
        self.assert_parked_once(original)

    def test_crash_after_comment_commit_before_park_intent_replays_original_bytes(self):
        self.prepare_green_review()
        strict = self.writer.comment

        def crash(**kwargs):
            result = strict(**kwargs)
            if "-gate-attestation-assessment-" in kwargs.get("request_id", ""):
                raise OSError("crash after comment commit")
            return result

        with mock.patch.object(self.writer, "comment", side_effect=crash), self.assertRaises(OSError):
            self.tick()
        original = copy.deepcopy(self.attestation_events())
        self.assertEqual(self._pilot_record()["worker_continuation"]["stage"], "retained")
        self.now += timedelta(seconds=3)
        self.assertEqual(self.tick()["to"], "assessment")
        self.assert_parked_once(original)

    def test_crash_after_park_intent_reuses_attestation_and_move_identity(self):
        self.prepare_green_review()
        strict = self.writer.move

        def crash(**kwargs):
            if kwargs.get("target") == "assessment":
                raise OSError("crash after park intent")
            return strict(**kwargs)

        with mock.patch.object(self.writer, "move", side_effect=crash), self.assertRaises(OSError):
            self.tick()
        original = copy.deepcopy(self.attestation_events())
        self.assertEqual(self._pilot_record()["worker_continuation"]["stage"], "assessment_pending")
        self.now += timedelta(seconds=3)
        self.assertEqual(self.tick()["to"], "assessment")
        self.assert_parked_once(original)

    def test_crash_after_move_before_save_reuses_attestation_and_move_identity(self):
        self.prepare_green_review()
        strict = self.runtime.production_state.save

        def crash(payload):
            if payload.get("records", {}).get(CARD_REF, {}).get("state") == "assessment":
                raise OSError("crash after Assessment move")
            return strict(payload)

        with mock.patch.object(self.runtime.production_state, "save", side_effect=crash), self.assertRaises(OSError):
            self.tick()
        original = copy.deepcopy(self.attestation_events())
        self.assertEqual(self._pilot_record()["worker_continuation"]["stage"], "assessment_pending")
        self.now += timedelta(seconds=3)
        self.assertEqual(self.tick()["to"], "assessment")
        self.assert_parked_once(original)

    def test_release_commit_crash_retries_frozen_audit_before_merge(self):
        self.prepare_green_review()
        self.assertEqual(self.tick()["to"], "assessment")
        self._decide("release", request_id="attestation-release")
        strict = self.writer.comment

        def crash(**kwargs):
            result = strict(**kwargs)
            if "-gate-attestation-release-" in kwargs.get("request_id", ""):
                raise OSError("crash after release attestation")
            return result

        with mock.patch.object(self.writer, "comment", side_effect=crash), self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.host.completed, [])
        self.now += timedelta(seconds=3)
        self.assertEqual(self.tick()["to"], "done")
        self.assertEqual(self.host.completed, [CARD_REF])
        events = [event for event in self.writer.audit.events(CARD_REF)
                  if event.get("kind") == "commented" and "-gate-attestation-release-" in event.get("request_id", "")]
        self.assertEqual(len(events), 1)

    def test_strict_payload_conflict_stalls_at_three_without_routing_or_comment_spam(self):
        self.prepare_green_review()
        record = DispatcherRecord.from_json(self._pilot_record())
        receipt = GateReceipt.accept(self.fresh_gate({}, record).attestation, current_sha=self.host.commit)
        assert receipt is not None
        context = gate_attestation.delivery_context(
            record, ref=CARD_REF, owner=self.runtime.owner, attempt_id=record.attempt_id,
            stage="assessment", e2e_reconciliation=None,
        )
        request_id = gate_attestation.effect_request(context, gate_attestation.semantic_identity(receipt, context))
        foreign = self.writer.comment(role="dispatcher", actor=self.runtime.owner, reference=CARD_REF,
                                      body="unrelated operation under a conflicting request", request_id=request_id)
        for count in range(1, 6):
            outcome = self.tick()
            self.assertEqual(outcome["attestation_failure"]["count"], min(count, 3))
            self.assertEqual(outcome["attestation_failure"]["committed"]["event_id"], foreign["event_id"])
            self.assertIn(request_id, outcome["reason"])
            self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")
            self.assertEqual(self._pilot_record()["worker_continuation"]["stage"], "retained")
            self.assertEqual(self.host.completed, [])
            self.assertEqual(self.host.resumed_workers, [])
            self.now += timedelta(seconds=3)
        notes = [event for event in self.writer.audit.events(CARD_REF)
                 if "-gate-attestation-stuck-" in event.get("request_id", "")]
        self.assertEqual(len(notes), 1)
        self.assertEqual(self.host.reviews, [CARD_REF])
        self.assertEqual(self.host.stopped_reviews, [])


class GateReceiptPolicyTests(unittest.TestCase):
    def receipt(self, *, mode: str = "local") -> dict[str, object]:
        receipt = mint_gate_receipt(
            validated_sha="a" * 40,
            base_sha="b" * 40,
            gate_mode=mode,
            required_checks=[
                {
                    "name": "unit",
                    "conclusion": "SUCCESS",
                    "url": "https://ci.invalid/1",
                }
            ],
            check_set_identity="python3 -m unittest",
        )
        assert receipt is not None
        return receipt

    def test_exact_sha_passing_receipt_is_typed_and_renderable(self) -> None:
        accepted = AcceptedGreenGate.accept(
            self.receipt(), current_sha="a" * 40, gate_mode="local", noop=False
        )

        self.assertTrue(accepted.policy_valid)
        self.assertIsInstance(accepted.receipt, GateReceipt)
        assert accepted.receipt is not None
        self.assertIn("unit: SUCCESS", accepted.receipt.render())

    def test_mismatch_or_nonpassing_receipt_fails_closed_for_executed_gate(self) -> None:
        failing = self.receipt()
        failing["required_checks"] = [{"name": "unit", "conclusion": "FAILURE", "url": ""}]

        for payload, current_sha, gate_mode in (
            (self.receipt(), "c" * 40, "local"),
            (self.receipt(mode="github"), "a" * 40, "local"),
            (failing, "a" * 40, "local"),
        ):
            with self.subTest(current_sha=current_sha, gate_mode=gate_mode):
                accepted = AcceptedGreenGate.accept(
                    payload, current_sha=current_sha, gate_mode=gate_mode, noop=False
                )
                self.assertFalse(accepted.policy_valid)
                self.assertIsNone(accepted.receipt)

    def test_none_and_noop_accept_only_receiptless_results(self) -> None:
        candidate = self.receipt()

        for gate_mode, noop in (("none", False), ("local", True)):
            with self.subTest(gate_mode=gate_mode, noop=noop):
                receiptless = AcceptedGreenGate.accept(
                    None, current_sha="a" * 40, gate_mode=gate_mode, noop=noop
                )
                forged = AcceptedGreenGate.accept(
                    candidate, current_sha="a" * 40, gate_mode=gate_mode, noop=noop
                )
                self.assertTrue(receiptless.policy_valid)
                self.assertIsNone(receiptless.receipt)
                self.assertFalse(forged.policy_valid)

    def test_unknown_mode_is_invalid_even_when_receiptless_or_noop(self) -> None:
        for noop in (False, True):
            accepted = AcceptedGreenGate.accept(None, current_sha="a" * 40, gate_mode="alternate", noop=noop)
            self.assertFalse(accepted.policy_valid)

    def test_only_full_sha1_or_sha256_object_ids_are_receipts(self) -> None:
        for length in (7, 12, 39, 41, 63, 65):
            with self.subTest(length=length):
                self.assertIsNone(
                    mint_gate_receipt(
                        validated_sha="a" * length,
                        base_sha="b" * 40,
                        gate_mode="local",
                        required_checks=[{"name": "unit", "conclusion": "SUCCESS", "url": ""}],
                        check_set_identity="unit",
                    )
                )
        abbreviated_base = self.receipt()
        abbreviated_base["base_sha"] = "b" * 12
        self.assertIsNone(GateReceipt.accept(abbreviated_base, current_sha="a" * 40))
        sha256 = mint_gate_receipt(
            validated_sha="a" * 64,
            base_sha="b" * 64,
            gate_mode="local",
            required_checks=[{"name": "unit", "conclusion": "SUCCESS", "url": ""}],
            check_set_identity="unit",
        )
        self.assertIsNotNone(GateReceipt.accept(sha256, current_sha="a" * 64))


if __name__ == "__main__":
    unittest.main()
