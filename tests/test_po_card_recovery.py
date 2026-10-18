"""Dispatcher recovery refusals preserve PO-card and owner-answer delivery obligations."""

import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.po_card_fakes import REF, Forbidden, OneCardBoard, SprintView, card
from tests.po_fake_store import FakePoStore
from ummanu.board.owner_handover import OWNER_ANSWER, attention_record
from ummanu.dispatch.claim import claim_ready_task
from ummanu.dispatch.po_cards import advance_po_card, owner_answer_request_id
from ummanu.dispatch.state import DispatcherRecord
from ummanu.po.client import RecoveryRefused, ServiceRefused
from ummanu.po.store import SPRINT_SESSION, sprint_session_fingerprint


class _PoChannel:
    def __init__(self):
        self.store = FakePoStore()
        self.deliveries = []
        self.resolutions = []

    def sprint_session(self, *, sprint_ref, request_id):
        session, created = self.store.claim_session(
            session_id="po-session", cli="claude", model="model", effort="high",
            cwd="/fixture/po", cli_session_id="native", request_id=request_id,
            operation=SPRINT_SESSION, fingerprint=sprint_session_fingerprint(sprint_ref),
        )
        if created:
            self.resolutions.append(request_id)
        return {"session_id": session.session_id, "created": created}

    def submit(self, *, session_id, text, request_id, source, card):
        turn, created = self.store.claim_turn(
            session_id, text, lambda seq: Path(f"/fixture/turn-{seq}.stdout"),
            request_id=request_id, card=card,
        )
        if created:
            self.deliveries.append(request_id)
        return {"session_id": session_id, "seq": turn.seq, "repeated": not created}

    def request(self, request_id):
        return self.store.request(request_id)

    def turn(self, session_id, seq):
        return self.store.turn(session_id, seq)

    def queued(self, request_id):
        return None

    def refused(self, request_id):
        return None


class DispatcherRecoveryTests(unittest.TestCase):
    def arrange(self):
        self.board = OneCardBoard(card())
        self.channel = _PoChannel()
        self.saved = []
        self.records = {}
        self.payload = {}
        self.attempt = "delivery-attempt"
        self.runtime = SimpleNamespace(
            owner="dispatcher", reader=self.board, writer=self.board, audit=self.board,
            sprints=SprintView([]), po=self.channel,
            host=Forbidden("host"), catalog=Forbidden("catalog"), head_health=Forbidden("head health"),
            save_records=lambda payload, records: self.saved.append(
                {ref: record.to_json() for ref, record in records.items()}),
        )

    def claim(self):
        return claim_ready_task(self.runtime, self.board.show(REF), self.records, self.payload, self.attempt)

    def tick(self):
        return advance_po_card(self.runtime, self.board.show(REF), self.records, self.payload, "later-tick")

    def reload_record(self):
        self.records = {REF: DispatcherRecord.from_json(copy.deepcopy(self.saved[-1][REF]))}

    def answer(self, *, legacy_session=False):
        self.assertEqual(self.claim()["action"], "po-card-submitted")
        initial = self.channel.request(self.records[REF].po_submission.submit_request_id)
        self.channel.store.complete_turn(initial.session_id, initial.seq, "Approval needed")
        now = datetime.now(UTC).isoformat()
        answer = {"event_id": "owner-answer", "handover_event": "owner-question", "at": now,
                  "quotation": "Approved.", "mark": {"since": now, "reason": "Approval needed", "by": "po"},
                  "po_session": "" if legacy_session else self.records[REF].po_submission.session_id}
        self.board.card.setdefault("extensions", {}).setdefault("extra", {})[OWNER_ANSWER] = json.dumps(answer)
        if legacy_session:
            self.records.clear()
        return answer

    def assert_held(self, outcome, unanswered):
        self.assertEqual((outcome["status"], outcome["action"]), ("degraded", "po-service-unanswered"))
        self.assertEqual(outcome["attempt_id"], self.attempt)
        self.assertEqual(self.board.card["state"], "in_progress")
        self.assertEqual(self.records[REF].attempt_id, self.attempt)
        self.assertEqual(self.records[REF].po_submission.unanswered, unanswered)
        self.assertEqual(self.saved[-1][REF]["attempt_id"], self.attempt)

    def test_retryable_initial_submission_retains_attempt_and_delivers_once(self):
        for code in ("recovery_hold", "recovery_busy"):
            with self.subTest(code=code):
                self.arrange()
                with mock.patch.object(self.channel, "submit", side_effect=RecoveryRefused(code, "recovery waits")) as send:
                    self.assert_held(self.claim(), 1)
                    frozen = copy.deepcopy(self.records[REF].po_submission.to_json())
                    request_id = frozen["submit_request_id"]
                    self.reload_record()
                    self.assert_held(self.tick(), 2)
                    self.assertFalse(self.records[REF].po_submission.submitted)
                    self.assertIsNone(self.channel.request(request_id))
                    self.assertEqual(self.channel.deliveries, [])
                    self.assertEqual(send.call_args_list[0], send.call_args_list[1])
                    self.assertEqual(send.call_args.kwargs["request_id"], request_id)
                    self.assertEqual(self.records[REF].po_submission.text, frozen["text"])
                    self.assertEqual(self.records[REF].po_submission.card, frozen["card"])
                accepted = self.tick()
                self.assertEqual((accepted["action"], accepted["po_request_id"]), ("po-card-submitted", request_id))
                self.assertEqual(self.channel.deliveries, [request_id])
                self.assertEqual(self.records[REF].po_submission.unanswered, 0)
                self.assertEqual(self.tick()["action"], "po-card-turn-running")
                self.reload_record()
                self.assertEqual(self.tick()["action"], "po-card-turn-running")
                self.assertEqual(self.channel.deliveries, [request_id])
                self.assertEqual(len(self.channel.resolutions), 1)

    def test_retryable_initial_resolution_preserves_request_and_opens_one_session(self):
        for code in ("recovery_hold", "recovery_busy"):
            with self.subTest(code=code):
                self.arrange()
                with mock.patch.object(self.channel, "sprint_session", side_effect=RecoveryRefused(code, "recovery waits")) as resolve:
                    self.assert_held(self.claim(), 1)
                    request_id = self.records[REF].po_submission.session_request_id
                    self.reload_record()
                    self.assert_held(self.tick(), 2)
                    self.assertEqual([call.kwargs["request_id"] for call in resolve.call_args_list], [request_id] * 2)
                    self.assertEqual(self.channel.resolutions, [])
                    self.assertEqual(self.channel.deliveries, [])
                self.assertEqual(self.tick()["action"], "po-card-submitted")
                submit_id = self.records[REF].po_submission.submit_request_id
                self.assertEqual(self.channel.resolutions, [request_id])
                self.assertEqual(self.channel.deliveries, [submit_id])
                self.assertEqual(self.tick()["action"], "po-card-turn-running")
                self.assertEqual(self.channel.deliveries, [submit_id])

    def test_retryable_owner_submission_preserves_answer_and_delivers_once(self):
        for code in ("recovery_hold", "recovery_busy"):
            with self.subTest(code=code):
                self.arrange()
                answer = self.answer()
                initial_id = self.records[REF].po_submission.submit_request_id
                request_id = owner_answer_request_id(REF, answer["event_id"])
                with mock.patch.object(self.channel, "submit", side_effect=RecoveryRefused(code, "recovery waits")) as send:
                    self.assert_held(self.tick(), 1)
                    text = self.records[REF].po_submission.owner_text
                    self.reload_record()
                    self.assert_held(self.tick(), 2)
                    submission = self.records[REF].po_submission
                    self.assertEqual((submission.owner_request_id, submission.owner_event_id, submission.owner_text),
                                     (request_id, answer["event_id"], text))
                    self.assertFalse(submission.owner_submitted)
                    self.assertEqual(attention_record(self.board.card, OWNER_ANSWER), answer)
                    self.assertEqual(self.channel.deliveries, [initial_id])
                    self.assertIsNone(self.channel.request(request_id))
                    self.assertEqual(send.call_args_list[0], send.call_args_list[1])
                    self.assertEqual(send.call_args.kwargs["request_id"], request_id)
                self.assertEqual(self.tick()["action"], "po-owner-answer-submitted")
                self.assertTrue(self.records[REF].po_submission.owner_submitted)
                self.assertEqual(self.channel.deliveries, [initial_id, request_id])
                self.assertEqual(self.tick()["action"], "po-card-owner-answered")
                self.reload_record()
                self.assertEqual(self.tick()["action"], "po-card-owner-answered")
                self.assertEqual(self.channel.deliveries, [initial_id, request_id])
                self.assertEqual(attention_record(self.board.card, OWNER_ANSWER), answer)

    def test_retryable_owner_resolution_preserves_answer_and_delivers_once(self):
        for code in ("recovery_hold", "recovery_busy"):
            with self.subTest(code=code):
                self.arrange()
                answer = self.answer(legacy_session=True)
                initial_id = self.channel.deliveries[0]
                request_id = owner_answer_request_id(REF, answer["event_id"])
                read = self.channel.request
                with mock.patch.object(self.channel, "request", side_effect=lambda identity, initial_id=initial_id, read=read: None if identity == initial_id else read(identity)):
                    session_request = self.channel.resolutions[0]
                    with mock.patch.object(self.channel, "sprint_session", side_effect=RecoveryRefused(code, "recovery waits")) as resolve:
                        self.assert_held(self.tick(), 1)
                        self.assertEqual(self.records[REF].po_submission.session_request_id, session_request)
                        self.reload_record()
                        self.assert_held(self.tick(), 2)
                        submission = self.records[REF].po_submission
                        self.assertFalse(submission.owner_submitted)
                        self.assertEqual(submission.owner_request_id, "")
                        self.assertEqual(attention_record(self.board.card, OWNER_ANSWER), answer)
                        self.assertEqual(self.channel.deliveries, [initial_id])
                        self.assertEqual([call.kwargs["request_id"] for call in resolve.call_args_list], [session_request] * 2)
                    self.assertEqual(self.tick()["action"], "po-owner-answer-submitted")
                    self.assertEqual(self.records[REF].po_submission.owner_request_id, request_id)
                    self.assertEqual(self.tick()["action"], "po-card-owner-answered")
                    self.reload_record()
                    self.assertEqual(self.tick()["action"], "po-card-owner-answered")
                self.assertEqual(self.channel.deliveries, [initial_id, request_id])
                self.assertEqual(self.channel.resolutions, [session_request])
                self.assertEqual(attention_record(self.board.card, OWNER_ANSWER), answer)

    def test_invalid_recovery_and_terminal_refusals_block_each_delivery_stage(self):
        for stage in ("initial resolve", "initial submit", "owner resolve", "owner submit"):
            for refusal in (RecoveryRefused("recovery_invalid", "damaged authority"), ServiceRefused("validation", "invalid input")):
                with self.subTest(stage=stage, code=refusal.code):
                    self.arrange()
                    answer = self.answer(legacy_session=stage == "owner resolve") if stage.startswith("owner") else None
                    deliveries = list(self.channel.deliveries)
                    initial_id = deliveries[0] if answer else ""
                    read = self.channel.request
                    method = "sprint_session" if stage.endswith("resolve") else "submit"
                    with (
                        mock.patch.object(self.channel, "request", side_effect=lambda identity, stage=stage, initial_id=initial_id, read=read: None if stage == "owner resolve" and identity == initial_id else read(identity)),
                        mock.patch.object(self.channel, method, side_effect=refusal),
                    ):
                        outcome = self.tick() if answer else self.claim()
                    self.assertEqual((outcome["status"], outcome["action"]), ("blocked", "po-card-blocked"))
                    self.assertEqual(self.board.card["state"], "blocked")
                    self.assertEqual(self.channel.deliveries, deliveries)
                    self.assertEqual(attention_record(self.board.card, OWNER_ANSWER), answer)
                    self.assertEqual(self.tick()["action"], "po-card-closed")
                    self.assertEqual(self.channel.deliveries, deliveries)
