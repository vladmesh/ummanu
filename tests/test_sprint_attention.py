"""Real SQL card producers, sprint read layer, rendered chip/bell and settlement."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest import mock

import psycopg

from tests.observer_identity import as_observer
from tests.web_fakes import Recording, system_snapshot
from tests.webproto_sprint_fixtures import SprintProtocolFixture
from ummanu.board.owner_events import OwnerEventStore, OwnerEventsUnavailable, ReadRefused, record
from ummanu.board.owner_handover import waiting_owner
from ummanu.board.sql_cards import SqlCardClient
from ummanu.tasks import TaskError, TaskReader, TaskWriter
from ummanu.web.app import WebApp
from ummanu.webproto.owner_events import OwnerEventLayer


class SprintAttentionTests(SprintProtocolFixture):
    def test_answer_transactions_settle_the_owner_before_completion_and_preserve_epochs(self):
        self.claim("decision")
        first = self.handover()
        self.assert_wait(True)
        [owner] = [e for e in self.store.events() if e.kind == "card_handed_to_owner"]
        quote = "Yes, choose option A.\n"
        def answer():
            return self.writer.record_owner_answer(role="po", actor="po", reference="ummanu-12",
                handover_event=first["event_id"], quotation=quote, request_id="answer-12")
        self.assert_event_timeout_rolls_back(answer, "answer-12", row_lock=True)
        self.assert_wait(True)
        self.assertFalse(answer()["replayed"])
        self.assert_committed_replay(answer)
        shown = self.writer.reader.show("ummanu-12")
        self.assertEqual(shown["state"], "in_progress")
        self.assertIsNone(waiting_owner(shown))
        self.assertIsNotNone(next(e for e in self.store.events() if e.id == owner.id).read_at)
        self.assert_wait(False)
        self.assertNotIn("owner", [e["kind"] for e in self.sprints.sprint_state("sprint:1")["work"]["waiting_on"]])
        second = self.writer.handover(role="po", actor="po", reference="ummanu-12", to="owner",
            reason="Which provider for the next operation?", request_id="second-handover")
        self.assert_wait(True)
        self.assertTrue(answer()["replayed"])
        self.assert_wait(True)
        with self.assertRaises(TaskError):
            self.writer.record_owner_answer(role="po", actor="po", reference="ummanu-12",
                handover_event=first["event_id"], quotation=quote, request_id="stale-answer")
        self.assert_wait(True)
        def comment():
            return self.writer.comment(role="owner", actor="owner", reference="ummanu-12",
                                       body="Use provider B.", request_id="owner-comment")
        self.assert_event_timeout_rolls_back(comment, "owner-comment", row_lock=True)
        self.assertFalse(comment()["replayed"])
        self.assert_committed_replay(comment)
        self.assertIsNone(waiting_owner(self.writer.reader.show("ummanu-12")))
        self.assert_wait(False)
        from ummanu.board.owner_handover import OWNER_ANSWER, attention_record
        recorded = attention_record(self.writer.reader.show("ummanu-12"), OWNER_ANSWER)
        self.assertEqual(recorded["handover_event"], second["event_id"])
        self.assertEqual(recorded["quotation"], "Use provider B.")

    def completed_answer_returns_to_observer(self, channel):
        from ummanu.board.owner_handover import OWNER_ANSWER, attention_record
        from ummanu.dispatch.po_cards import _po_record, advance_po_card, owner_answer_request_id
        self.claim("decision")
        first = self.handover()
        if channel == "native":
            self.writer.record_owner_answer(role="po", actor="po", reference="ummanu-12",
                handover_event=first["event_id"], quotation="Choose A.\n", request_id="answer-12")
        else:
            self.writer.comment(role="owner", actor="owner", reference="ummanu-12",
                                body="Choose A.\n", request_id="answer-12")
        card = self.writer.reader.show("ummanu-12")
        answer = attention_record(card, OWNER_ANSWER)
        self.assert_wait(False)
        before = self.store.events()
        audit = self.writer.audit.events("ummanu-12")
        po = mock.Mock()
        po.request.return_value = SimpleNamespace(session_id="answer-session", seq=7)
        po.turn.return_value = SimpleNamespace(session_id="answer-session", seq=7, state="completed")
        runtime = SimpleNamespace(reader=self.writer.reader, writer=self.writer, audit=self.writer.audit,
                                  owner="dispatcher", po=po, save_records=lambda *_: None)
        records = {"ummanu-12": _po_record(card, "followup-fixture")}
        result = advance_po_card(runtime, card, records, {}, "followup-fixture")
        self.assertEqual(result["action"], "po-card-blocked")
        self.assertIn("answer-session/7 ended completed without completing the card", result["reason"])
        po.request.assert_called_once_with(owner_answer_request_id("ummanu-12", answer["event_id"]))
        po.submit.assert_not_called()
        shown = self.writer.reader.show("ummanu-12")
        self.assertEqual(shown["state"], "blocked")
        self.assertEqual(attention_record(shown, OWNER_ANSWER), answer)
        self.assertIsNone(waiting_owner(shown))
        added = [e for e in self.store.events() if e.id not in {e.id for e in before}]
        self.assertEqual([(e.kind, e.event_class) for e in added], [("card_waits_for_person", "notice")])
        self.assertIn("observer", added[0].text.lower())
        self.assert_wait(False)
        self.assertEqual(self.writer.audit.events("ummanu-12")[:len(audit)], audit)
        for _ in range(3):
            result = advance_po_card(runtime, self.writer.reader.show("ummanu-12"), records, {}, "restart")
            self.assertEqual(result["action"], "po-card-closed")
        self.assertEqual(len(self.store.events()), len(before) + 1)
        po.submit.assert_not_called()

    def test_native_answer_completed_followup_returns_neutral_blocked_notice(self):
        self.completed_answer_returns_to_observer("native")

    def test_owner_comment_completed_followup_returns_neutral_blocked_notice(self):
        self.completed_answer_returns_to_observer("comment")

    def test_answer_and_escalation_failures_after_the_mark_roll_back_the_whole_sql_mutation(self):
        claimed = self.claim("decision")
        call = self.board.call
        def fail_after_metadata(method, **params):
            result = call(method, **params)
            if method == "saveTaskMetadata":
                raise RuntimeError("fixture interruption after durable state write")
            return result
        def escalate():
            return self.writer.escalate_po_card(actor="dispatcher", reference="ummanu-12",
                episode=claimed["event_id"], reason="PO execution failed",)
        before = self.committed_state()
        with mock.patch.object(self.board, "call", side_effect=fail_after_metadata), self.assertRaises(RuntimeError):
            escalate()
        self.assertEqual(self.committed_state(), before)
        self.assertFalse(escalate()["replayed"])
        self.assert_committed_replay(escalate)
        self.assert_wait(True)
        first = self.handover()
        self.assert_wait(True)
        def answer():
            return self.writer.record_owner_answer(role="po", actor="po", reference="ummanu-12",
                handover_event=first["event_id"], quotation="Proceed.", request_id="atomic-answer")
        before = self.committed_state()
        with mock.patch.object(self.board, "call", side_effect=fail_after_metadata), self.assertRaises(RuntimeError):
            answer()
        self.assertEqual(self.committed_state(), before)
        self.assertFalse(answer()["replayed"])
        self.assert_committed_replay(answer)
        self.assert_wait(False)

    def test_five_budget_dependents_share_one_actual_owner_question(self):
        import json
        from ummanu.board.e2e_record import BudgetWait, E2eState
        self.claim("decision")
        for number in range(90, 95):
            self.board.add_card(number, f"ummanu-{number}", metadata={"task_type": "code", "sprint_ref": "sprint:1",
                "e2e": json.dumps(E2eState(budget_wait=BudgetWait("ummanu-12", 1, "sprint", "2026-09-29T00:00:00Z")).to_json())})
        self.assert_wait(False)
        first = self.handover()
        waits = self.sprints.sprint_state("sprint:1")["work"]["waiting_on"]
        self.assertEqual(sum(e["kind"] == "owner" for e in waits), 1)
        deps = [e for e in waits if e["kind"] == "dependency"]
        self.assertEqual(len(deps), 5)
        self.assertEqual({e["holder"] for e in deps}, {"ummanu-12"})
        self.assertEqual(len(self.attention()["event_ids"]), 1)
        self.writer.record_owner_answer(role="po", actor="po", reference="ummanu-12",
            handover_event=first["event_id"], quotation="No more paid runs.", request_id="budget-answer")
        waits = self.sprints.sprint_state("sprint:1")["work"]["waiting_on"]
        self.assertFalse(any(e["kind"] == "owner" for e in waits))
        self.assert_wait(False)

    def test_supported_successor_settles_predecessor_and_refuses_late_escalation(self):
        sprint_row = next(row for row in self.sprint_rows() if row["reference"] == "sprint:1")
        self.board.save_metadata(int(sprint_row["id"]), sprint_reservations='["ummanu"]')
        claim = self.claim("decision")
        self.handover()
        self.assert_wait(True)
        def successor():
            with as_observer("sprint:1"):
                return self.writer.create(role="observer", actor="observer", project="ummanu",
                    task_type="code", title="Replace the old episode", sprint="sprint:1",
                    seed_ref="pipeline/ummanu-12", supersedes="ummanu-12", request_id="successor-12")
        cards_before = self.writer.reader.list()
        self.assert_event_timeout_rolls_back(successor, "successor-12", row_lock=True)
        self.assertEqual(self.writer.reader.list(), cards_before)
        self.assert_wait(True)
        created = successor()
        self.assertFalse(created["replayed"])
        replacement = self.writer.reader.show(created["task"]["ref"])
        self.assertNotEqual(replacement["ref"], "ummanu-12")
        self.assertEqual(replacement["state"], "ready")
        self.assertEqual(replacement["workspace"]["supersedes"], "ummanu-12")
        self.assert_committed_replay(successor)
        self.assertIsNone(waiting_owner(self.writer.reader.show("ummanu-12")))
        self.assert_wait(False)
        self.assertFalse(any(e["card"] == "ummanu-12" for e in
            self.sprints.sprint_state("sprint:1")["work"]["waiting_on"]))
        with self.assertRaises(TaskError) as refused:
            self.writer.escalate_po_card(actor="dispatcher", reference="ummanu-12",
                episode=claim["event_id"], reason="late timeout")
        self.assertEqual(refused.exception.code, "attention_resolved")
        self.assert_wait(False)

    def setUp(self):
        super().setUp()
        self.add_sprint_row("sprint:1", current_task="ummanu-12")
        self.store = OwnerEventStore(self.board.credentials, client=self.board)
        self.events = OwnerEventLayer(self.instance, store=self.store)
        self.sprints = self.reads(owner_events=self.events)
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.tmp)
        self.app = WebApp(Recording(system_snapshot=system_snapshot()), Recording(), self.sprints,
                          Recording(), Recording(pause_state={}), Recording(), Recording(), Recording(),
                          owner_events=self.events)

    def page(self):
        response = self.app.handle("GET", "/")
        self.assertEqual(response.status, 200)
        return response.body.decode()

    def attention(self):
        return self.sprints.sprint_list(statuses=["open"])["sprints"]["items"][0]["attention"]

    def assert_wait(self, expected):
        page = self.page()
        card = next(article for article in re.findall(r'<article class="compact-sprint">.*?</article>', page, re.DOTALL)
                    if 'href="/sprints/sprint%3A1"' in article)
        self.assertEqual("attention required" in card, expected)
        count = re.search(r'<span class="bell-count">(\d+)</span>', page)
        self.assertIsNotNone(count)
        if expected:
            self.assertGreater(int(count[1]), 0)
            identifiers = self.attention()["event_ids"]
            listed = {event["id"] for event in self.events.owner_event_list(unread_only=True)["events"]}
            self.assertTrue(set(identifiers) <= listed)
        return page

    def claim(self, kind="code"):
        self.board.save_metadata(12, task_type=kind)
        return self.writer.claim(role="dispatcher", actor="dispatcher", reference="ummanu-12",
                                 worker="fixture", request_id="claim-12")

    def move(self, target, request="move-12"):
        return self.writer.move(role="dispatcher", actor="dispatcher", reference="ummanu-12",
                                target=target, reason="fixture decision", request_id=request)

    def handover(self, request="handover-12"):
        return self.writer.handover(role="po", actor="po", reference="ummanu-12", to="owner",
                                    reason="Choose the fixture option", request_id=request)

    def complete(self, request="complete-12"):
        return self.writer.complete(role="po", actor="po", reference="ummanu-12", kind="decision",
                                    body="## Decision\nChoose option A.\n\n## How to verify\nRead the fixture.\n",
                                    request_id=request)

    def committed_state(self):
        return (self.writer.reader.show("ummanu-12"), self.store.events(),
                self.writer.audit.events("ummanu-12"))

    def assert_event_timeout_rolls_back(self, action, request, *, row_lock=False):
        before = self.committed_state()
        with psycopg.connect(self.board.credentials.conninfo()) as blocker:
            if row_lock:
                blocker.execute("SELECT id FROM owner_events WHERE read_at IS NULL FOR UPDATE").fetchall()
            else:
                blocker.execute("LOCK TABLE owner_events IN SHARE MODE")
            with self.assertRaises(TaskError) as refusal, self.board.transaction():
                self.board.connection.execute("SET LOCAL statement_timeout = '200ms'")
                action()
            self.assertEqual(refusal.exception.code, "backend_error")
            self.assertIn("required owner wait", str(refusal.exception))
            self.assertIn("statement timeout", str(refusal.exception))
            self.assertIn("rolled back", str(refusal.exception))
            self.assertIn("retry the same request ID", str(refusal.exception))
            self.assertIsInstance(refusal.exception.__cause__, OwnerEventsUnavailable)
        self.assertEqual(self.committed_state(), before)
        self.assertIsNone(self.writer.audit.pending_event(request))
        self.assertIsNone(self.writer.audit.committed_event(request))
        self.assertIsNone(self.writer._typed_event(request))
        self.assertEqual(self.writer.reconcile(), (0, 0))

    def assert_committed_replay(self, action):
        before = self.committed_state()
        self.assertTrue(action()["replayed"])
        self.assertEqual(self.committed_state(), before)

    def test_handover_abort_and_post_commit_interruption_keep_a_durable_wait(self):
        self.claim("decision")
        before = self.committed_state()
        [po] = self.store.events(unread_only=True)
        # A separate client and event connection see committed data while the writer's
        # transaction has already inserted the replacement, settled PO and set the mark.
        observer = SqlCardClient(self.board.credentials, self.instance)
        self.addCleanup(observer.close)
        events = OwnerEventLayer(self.instance, store=OwnerEventStore(self.board.credentials))
        sprints = self.reads(board_client=observer, owner_events=events)
        app = WebApp(Recording(system_snapshot=system_snapshot()), Recording(), sprints,
                     Recording(), Recording(pause_state={}), Recording(), Recording(), Recording(),
                     owner_events=events)
        call = self.board.call
        observed = []
        abort_before_commit = True

        def at_uncommitted_mark(method, **params):
            if method == "createComment":
                self.assertIsNotNone(waiting_owner(self.writer.reader.show("ummanu-12")))
                self.assertIsNone(waiting_owner(TaskReader(observer).show("ummanu-12")))
                [event] = events.owner_event_list(unread_only=True)["events"]
                self.assertEqual((event["id"], event["kind"]), (po.id, "card_waits_for_person"))
                page = app.handle("GET", "/").body.decode()
                self.assertNotIn("attention required", page)
                self.assertIn('<span class="bell-count">1</span>', page)
                observed.append(event["id"])
                if abort_before_commit:
                    raise KeyboardInterrupt("stop before the real transaction commits")
            return call(method, **params)

        with (mock.patch.object(self.board, "call", side_effect=at_uncommitted_mark),
              self.assertRaises(KeyboardInterrupt)):
            self.handover("interrupted-handover")
        self.assertEqual(observed, [po.id])
        self.assertEqual(self.committed_state(), before)
        self.assertIsNone(self.writer.audit.pending_event("interrupted-handover"))
        self.assert_wait(False)

        write = self.writer._write
        abort_before_commit = False

        def interrupt_after_commit(*args, **kwargs):
            result = write(*args, **kwargs)
            # This independent read verifies that _write really committed before interruption.
            self.assertIsNotNone(waiting_owner(TaskReader(observer).show("ummanu-12")))
            [event] = [e for e in events.owner_event_list(unread_only=True)["events"] if e["class"] == "needs_owner"]
            self.assertEqual(event["kind"], "card_handed_to_owner")
            self.assertEqual(event["dedup_key"], f"card_handed_to_owner:ummanu-12:{result['event_id']}")
            page = app.handle("GET", "/").body.decode()
            self.assertIn("attention required", page)
            self.assertIn('<span class="bell-count">2</span>', page)
            raise KeyboardInterrupt("stop after the real transaction commits")

        with (mock.patch.object(self.board, "call", side_effect=at_uncommitted_mark),
              mock.patch.object(self.writer, "_write", side_effect=interrupt_after_commit),
              self.assertRaises(KeyboardInterrupt)):
            self.handover("interrupted-handover")
        self.assertEqual(observed, [po.id, po.id])
        self.assertEqual(self.writer.reconcile(), (0, 0))
        self.assert_committed_replay(lambda: self.handover("interrupted-handover"))
        self.assert_wait(True)
        [owner] = [e for e in self.store.events(unread_only=True) if e.event_class == "needs_owner"]
        self.assertEqual(owner.kind, "card_handed_to_owner")
        self.assertIsNone(next(event for event in self.store.events() if event.id == po.id).read_at)
        with self.assertRaises(ReadRefused):
            self.store.mark_read(owner.id)
        self.assertEqual(self.store.mark_all_read(), 1)
        self.complete()
        self.assert_committed_replay(self.complete)
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_native_po_wait_insert_timeout_rolls_back_claim_and_retries_once(self):
        self.board.save_metadata(12, task_type="decision")

        def claim():
            return self.writer.claim(role="dispatcher", actor="dispatcher", reference="ummanu-12",
                                     worker="fixture", request_id="timeout-claim")

        self.assert_event_timeout_rolls_back(claim, "timeout-claim")
        self.assertEqual(self.writer.reader.show("ummanu-12")["state"], "ready")
        self.assert_wait(False)
        self.assertFalse(claim()["replayed"])
        self.assert_committed_replay(claim)
        [event] = self.store.events()
        self.assertEqual(event.dedup_key, "card_waits_for_person:ummanu-12:timeout-claim")
        self.assertEqual(self.sprints.sprint_state("sprint:1")["work"]["waiting_on"][0]["kind"], "po")
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 1)


    def test_native_blocked_notice_survives_unblock_until_read(self):
        self.claim()
        block = lambda: self.move("blocked", "timeout-block")
        self.assert_event_timeout_rolls_back(block, "timeout-block")
        self.assertEqual(self.writer.reader.show("ummanu-12")["state"], "in_progress")
        self.assert_wait(False)
        self.assertFalse(block()["replayed"])
        self.assert_committed_replay(block)
        [event] = self.store.events()
        self.assertEqual(event.dedup_key, "card_waits_for_person:ummanu-12:timeout-block")
        self.assert_wait(False)

        def unblock():
            return self.writer.move(role="po", actor="po", reference="ummanu-12", target="ready",
                                    reason="decision taken", sprint_override=True,
                                    sprint_override_reason="fixture decision", request_id="timeout-unblock")

        self.assertEqual(self.writer.reader.show("ummanu-12")["state"], "blocked")
        self.assert_wait(False)
        self.assertFalse(unblock()["replayed"])
        self.assert_committed_replay(unblock)
        self.assert_wait(False)
        [settled] = self.store.events()
        self.assertEqual(settled.id, event.id)
        self.assertIsNone(settled.read_at)
        self.assertEqual(self.store.mark_read(settled.id).id, event.id)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_native_handover_replacement_and_completion_settlement_failures_roll_back(self):
        claimed = self.claim("decision")
        self.writer.escalate_po_card(actor="dispatcher", reference="ummanu-12", episode=claimed["event_id"], reason="fixture failed execution")
        self.assert_event_timeout_rolls_back(self.handover, "handover-12")
        self.assert_wait(True)
        # A row lock permits the replacement INSERT, then cancels predecessor settlement.
        # Both savepoints and the mark/audit still roll back together.
        self.assert_event_timeout_rolls_back(self.handover, "handover-12", row_lock=True)
        self.assertIsNone(waiting_owner(self.writer.reader.show("ummanu-12")))
        [po] = [e for e in self.store.events() if e.kind == "po_card_escalated"]
        self.assertTrue(po.unread)
        self.handover()
        self.assert_committed_replay(self.handover)
        self.assert_wait(True)
        self.assert_event_timeout_rolls_back(self.complete, "complete-12")
        self.assertIsNotNone(waiting_owner(self.writer.reader.show("ummanu-12")))
        [owner] = [e for e in self.store.events(unread_only=True) if e.event_class == "needs_owner"]
        self.assertEqual(owner.kind, "card_handed_to_owner")
        with self.assertRaises(ReadRefused):
            self.store.mark_read(owner.id)
        self.assert_wait(True)
        self.complete()
        self.assert_committed_replay(self.complete)
        self.assert_wait(False)
        self.assertEqual(self.store.mark_all_read(), 1)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_between_cards_empty_waiting_on_and_an_unrelated_notice_are_neutral(self):
        self.board.move(12, "done")
        item = self.sprints.sprint_list()["sprints"]["items"][0]
        self.assertEqual(item["waiting"]["state"], "waiting")
        self.assertEqual(item["waiting_on"], [])
        self.assert_wait(False)
        record("provider_red", None, "unrelated provider notice", "notice", to=self.store)
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 1)

    def test_open_needs_owner_is_scoped_to_its_real_card_and_sprint(self):
        self.board.add_card(90, "other-90", metadata={"task_type": "code", "sprint_ref": "sprint:90"})
        record("steward_needs_human", "other-90", "other sprint decision", "other", to=self.store)
        self.assert_wait(False)
        record("steward_needs_human", "ummanu-12", "this sprint decision", "local", to=self.store)
        self.assert_wait(True)
        self.assertEqual(self.attention()["event_ids"], [self.store.events()[0].id])
        self.events.mark_read(self.attention()["event_ids"][0])
        self.assert_wait(False)

    def test_po_submission_handover_and_completion_use_real_held_events_and_settle(self):
        self.claim("decision")
        self.assert_wait(False)
        [po] = self.store.events(unread_only=True)
        self.assertEqual(po.kind, "card_waits_for_person")
        self.assertFalse(po.held)
        self.assertEqual(self.sprints.sprint_state("sprint:1")["work"]["waiting_on"][0]["kind"], "po")
        self.assertEqual(self.store.mark_read(po.id).id, po.id)
        self.writer.handover(role="po", actor="po", reference="ummanu-12", to="owner",
                             reason="Choose the fixture option", request_id="handover-12")
        self.assert_wait(True)
        [owner] = self.store.events(unread_only=True)
        self.assertEqual(owner.kind, "card_handed_to_owner")
        self.assertTrue(owner.held)
        self.assertIsNotNone(next(event for event in self.store.events() if event.id == po.id).read_at)
        with self.assertRaises(ReadRefused):
            self.store.mark_read(owner.id)
        self.assertEqual(self.store.mark_all_read(), 0)
        self.writer.complete(role="po", actor="po", reference="ummanu-12", kind="decision",
                             body="## Decision\nChoose option A.\n\n## How to verify\nRead the fixture.\n",
                             request_id="complete-12")
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_a_blocked_decision_uses_the_transition_producer_and_clears_on_unblock(self):
        self.claim()
        self.move("blocked")
        self.assert_wait(False)
        [event] = self.store.events(unread_only=True)
        self.assertEqual((event.kind, event.subject_ref), ("card_waits_for_person", "ummanu-12"))
        self.assertEqual(event.event_class, "notice")
        self.writer.move(role="po", actor="po", reference="ummanu-12", target="ready",
                         reason="decision taken", sprint_override=True, sprint_override_reason="fixture decision",
                         request_id="unblock-12")
        self.assert_wait(False)
        self.assertIsNone(self.store.events()[0].read_at)
        self.store.mark_read(event.id)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_superseded_blocked_card_unavailable_and_unknown_sources_do_not_warn(self):
        self.claim()
        self.move("blocked")
        self.assert_wait(False)
        self.board.add_card(91, "ummanu-91", metadata={"task_type": "code", "sprint_ref": "sprint:1", "supersedes": "ummanu-12"})
        self.assert_wait(False)
        with mock.patch.object(self.store, "snapshot", side_effect=OwnerEventsUnavailable("board unavailable")):
            page = self.page()
        self.assertNotIn("attention required", page)
        self.assertIn('<span class="bell-count">?</span>', page)
        from ummanu.sprints import SprintReader
        from ummanu.tasks import TaskError
        with mock.patch.object(SprintReader, "linked_cards", side_effect=TaskError("unavailable", "cannot read cards", 4)):
            self.assert_wait(False)
            self.assertEqual(self.attention()["state"], "unknown")
        self.board.move(12, "in_progress")
        (self.data_dir / "dispatcher" / "production-state.json").unlink()
        self.assertIn("observer unknown", self.assert_wait(False))

    def test_wait_cards_and_ci_waits_do_not_mint_human_events(self):
        from ummanu.board.wait_card import TARGET_TIME, WaitSpec, WaitTarget

        spec = WaitSpec(WaitTarget(TARGET_TIME, at="2026-09-29T01:00:00Z"),
                        deadline="2026-09-29T02:00:00Z", returns=("observer",),
                        created_at="2026-09-29T00:00:00Z")
        self.board.save_metadata(12, wait=spec.text())
        self.claim("wait")
        self.assert_wait(False)
        self.assertEqual(self.sprints.sprint_state("sprint:1")["work"]["waiting_on"][0]["kind"], "run")
        self.move("blocked")
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_a_known_pending_ci_run_is_neutral_through_the_real_card_read(self):
        import json

        from ummanu.board.e2e_record import E2eRun, E2eState

        self.claim()
        run = E2eRun(dispatch_id="ci-fixture", sha="a" * 40, repo="example/fixture", branch="main",
                     workflow="ci.yml", intent_at="2026-09-29T00:00:00Z", run_id=42,
                     head_sha="a" * 40, wait_ref="ummanu-wait",
                     run_url="https://github.com/example/fixture/actions/runs/42")
        self.board.save_metadata(12, e2e=json.dumps(E2eState(runs=[run]).to_json()))
        self._production({}, {"ummanu-12": {"state": "validate", "gate_state": "pending"}})
        work = self.sprints.sprint_state("sprint:1")["work"]
        self.assertEqual(work["waiting_on"][0]["kind"], "run")
        self.assertIn(run.run_url, work["waiting_on"][0]["detail"])
        self.assertNotIn('chip warn', self.assert_wait(False).split('<article class="compact-sprint">')[1].split('</article>')[0])
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_unread_list_keeps_old_unread_notices_when_the_recent_list_is_full(self):
        record("provider_red", None, "old unread notice", "old", to=self.store)
        with self.store._connection() as connection:
            connection.execute(
                "INSERT INTO owner_events (kind, class, text, created_at, read_at, dedup_key) "
                "SELECT 'provider_red', 'notice', 'read notice', now() + interval '1 hour', now(), "
                "'read-' || i FROM generate_series(1,500) AS i"
            )
        self.assertEqual(len(self.events.owner_event_list()["events"]), 500)
        unread = self.events.owner_event_list(unread_only=True)
        self.assertEqual((unread["unread"], len(unread["events"])), (1, 1))
        self.assertEqual(unread["events"][0]["dedup_key"], "old")
        self.assert_wait(False)

    def test_bulk_counts_all_steward_events_beyond_the_list_and_preserves_the_held_handover(self):
        self.claim("decision")
        self.handover()
        held = next(e for e in self.store.events() if e.kind == "card_handed_to_owner")
        with self.store._connection() as connection:
            connection.execute(
                "INSERT INTO owner_events (kind, class, subject_ref, text, dedup_key) "
                "SELECT 'steward_needs_human', 'needs_owner', 'ummanu-12', 'routing decision', "
                "'steward-' || i FROM generate_series(1,501) AS i"
            )
        snapshot = self.events.owner_event_list()
        self.assertEqual(len(snapshot["events"]), 500)
        self.assertEqual((snapshot["needs_owner_count"], snapshot["held_count"]), (502, 1))
        result = self.events.mark_all_read()
        self.assertEqual(result["marked"], snapshot["notice_count"])
        self.assertEqual((result["remaining"], result["needs_owner_count"], result["held_count"]), (502, 502, 1))
        page = self.app.handle("GET", "/owner-events", query=f"marked={result['marked']}").body.decode()
        for text in ("502 owner-attention events remain", "1 held by an unanswered handover",
                     "501 can be marked read individually", "No unread notices to mark"):
            self.assertIn(text, page)
        steward = next(e for e in self.store.events() if e.kind == "steward_needs_human")
        self.events.mark_read(steward.id)
        self.assertEqual(self.events.owner_event_list()["needs_owner_count"], 501)
        with self.assertRaises(ReadRefused):
            self.store.mark_read(held.id)

    def test_render_pins_one_statement_for_the_chip_and_bell_and_gets_do_not_write(self):
        self.claim("decision")
        self.assertEqual(self.store.mark_all_read(), 1)
        self.handover()
        snapshot = self.store.snapshot
        calls = []

        def read_once():
            calls.append(1)
            answer = snapshot()
            # A real settlement after the reading cannot split this response's chip/count.
            self.store.settle_subject("ummanu-12")
            return answer

        with mock.patch.object(self.store, "snapshot", side_effect=read_once):
            page = self.page()
        self.assertEqual(len(calls), 1)
        self.assertIn("attention required", page)
        self.assertIn('<span class="bell-count">1</span>', page)
        self.assert_wait(False)
        self.assertEqual({event.kind for event in self.store.events()},
                         {"card_waits_for_person", "card_handed_to_owner"})
        self.assertTrue(all(event.read_at is not None for event in self.store.events()))


if __name__ == "__main__":
    import unittest
    unittest.main()
