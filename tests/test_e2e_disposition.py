"""Durable after-merge disposition rules/transactions over an in-memory board.

The actual PostgreSQL completion and dispatcher boundary is exercised in
test_e2e_after_merge, in CI. No SQL/container backend is started by this suite.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.board import e2e_disposition, e2e_record, po_execution
from ummanu.board.completion_evidence import po_completion_fields, render_po_completion_record
from ummanu.dispatch import e2e_after_merge
from ummanu.tasks import TaskError, TaskWriter
from ummanu.webproto.sprint_reads import card_waits


class DispositionTests(unittest.TestCase):
    def setUp(self):
        self.carrier, self.source, self.operation = "ummanu-2", "ummanu-1", "ummanu-3"
        self.run = e2e_record.E2eRun(dispatch_id="ummanu-2-e2e-am-1-abc", sha="b" * 40,
            repo="vladmesh/ummanu", branch="pipeline-e2e/old", workflow="e2e.yml",
            intent_at="2026-10-05T00:00:00Z", placement="after_merge", dispatch="refused",
            closing="closed", resolution="blocked", acted=True, disposition=self.operation,
            covered=[{"ref": self.source, "merge_sha": "a" * 40},
                     {"ref": self.carrier, "merge_sha": "b" * 40}])
        self.cards = {}
        for covered in self.run.covered:
            state = e2e_record.E2eState(after_merge=e2e_record.AfterMergeMark(
                merge_sha=covered["merge_sha"], state="blocked", dispatch_id=self.run.dispatch_id,
                carrier=self.carrier, decision=self.operation, charged=[self.run.dispatch_id]),
                after_merge_runs=[copy.deepcopy(self.run)] if covered["ref"] == self.carrier else [])
            self.cards[covered["ref"]] = {"id": int(covered["ref"].split("-")[-1]),
                "ref": covered["ref"], "project": "ummanu", "type": "code", "state": "done",
                "extensions": {"extra": {"e2e": state.text()}}}
        assignment = po_execution.PoExecution(po_execution.DISPOSITION_PREFIX + self.run.dispatch_id,
            "e2e_disposition", (self.source, self.carrier),
            initial={"via": "create_session", "replaces": "", "session": "po-session-1"}, executor="po-session-1")
        self.cards[self.operation] = {"id": 3, "ref": self.operation, "project": "ummanu",
            "type": "operation", "state": "in_progress", "comments": [],
            "extensions": {"extra": {"po_execution": assignment.text()}}}
        self.events = []
        self.hotfix_created = None
        self.created = {"ref": self.operation, "actor": {"role": "dispatcher"}, "kind": "created", "outcome": "success"}
        self.saves = []
        self.fail_save = ""

        def save(method, **fields):
            self.assertEqual(method, "saveTaskMetadata")
            ref = next(ref for ref, card in self.cards.items() if card["id"] == fields["task_id"])
            if ref == self.fail_save:
                raise TaskError("backend_error", "save interrupted", 1)
            values = dict(fields["values"])
            if "blocked_by" in values:
                self.cards[ref]["blocked_by"] = values.pop("blocked_by")
            self.cards[ref].setdefault("extensions", {}).setdefault("extra", {}).update(values)
            self.saves.append(ref)

        self.writer = TaskWriter.__new__(TaskWriter)
        self.writer.data_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.writer.client = SimpleNamespace(_query=mock.Mock(return_value=[]), call=save)
        self.writer._role = lambda role, allowed, actor: self.assertEqual(role, "dispatcher")
        self.writer.reader = SimpleNamespace(show=self.show, list=lambda: [self.show(ref) for ref in self.cards])
        self.writer.reader.restore_snapshot = lambda: {card["ref"]: card for card in self.writer.reader.list()}
        self.writer.audit = SimpleNamespace(committed_event=lambda request: self.hotfix_created
            if request.startswith(e2e_record.AFTER_MERGE_HOTFIX_REQUEST_PREFIX) else self.created,
                                            events=lambda ref, **kw: copy.deepcopy(self.events) if ref == self.operation else [])
        self.writer.comment = mock.Mock()

        @contextmanager
        def transaction():
            before = copy.deepcopy(self.cards)
            try:
                yield
            except BaseException:
                self.cards = before
                raise
        self.writer._mutation = transaction
        self.runtime = SimpleNamespace(writer=self.writer, reader=self.writer.reader, audit=self.writer.audit,
            owner="dispatcher", save_records=mock.Mock())

    def show(self, ref):
        if ref not in self.cards:
            raise TaskError("not_found", ref, 2)
        task = copy.deepcopy(self.cards[ref])
        view = e2e_record.e2e_view(task)
        if view:
            task["e2e"] = view
        return task

    def outcome(self, action="retry", **fields):
        result = {**e2e_disposition.completion_identity(self.operation, self.carrier, self.run),
                  "action": action, "evidence": "GitHub investigation: previous POST was refused; no workflow started"}
        if action == "retry":
            result["prior_effect"] = "not_started"
        return {**result, **fields}

    def complete(self, outcome=None, *, session="po-session-1", body=None):
        body = body or "## What was done\nInvestigated prior effect.\n\n## How to verify\nRead GitHub dispatch receipt."
        if outcome is not None:
            body += "\n\n## E2E disposition\n" + json.dumps(outcome)
        fields, refusal = po_completion_fields("operation", body)
        self.assertFalse(refusal)
        record = render_po_completion_record("operation", fields)
        self.cards[self.operation].update(state="done", comments=[{"marker": "po", "body": "[po]\n" + record}])
        self.events = [{"ref": self.operation, "record_type": "board.protocol_event", "transition": {"source": "in_progress", "target": "done"},
            "reason": record, "actor": {"role": "po"}, "data": {"po_session": session},
            "request_id": "completed-1", "event_id": "completion-event"}]

    def reconcile(self):
        return self.writer.reconcile_after_merge_disposition(role="dispatcher", actor="dispatcher",
            carrier=self.carrier, dispatch_id=self.run.dispatch_id)

    def mark(self, ref=None):
        return e2e_record.e2e_state(self.show(ref or self.source)).after_merge

    def follow_up(self, state="ready", **fields):
        self.cards["ummanu-4"] = {"id": 4, "ref": "ummanu-4", "project": "ummanu", "type": "code",
            "state": state, "extensions": {"extra": {"po_origin": '{"session":"real-po","request":"turn-1"}'}}, **fields}

    def test_explicit_retry_is_durable_pending_and_survives_deleted_queue(self):
        self.complete(self.outcome())
        result = self.reconcile()
        self.assertEqual((result["status"], self.mark().state, self.mark().holder), ("settled", "pending", ""))
        self.assertEqual(self.mark().charged, [self.run.dispatch_id])
        self.assertFalse(self.reconcile()["changed"])
        payload = {}
        e2e_after_merge._recover_pending(self.runtime, payload, {}, self.writer.reader.list())
        queue = e2e_after_merge.queues(payload)["ummanu"]
        self.assertEqual({entry["ref"] for entry in queue["pending"]}, {self.source, self.carrier})
        self.assertTrue(all(entry["repo"] == self.run.repo for entry in queue["pending"]))
        e2e_after_merge._recover_pending(self.runtime, payload, {}, self.writer.reader.list())
        self.assertEqual(len(queue["pending"]), 2)
        self.assertEqual(card_waits(self.show(self.source))[0]["kind"], "run")

    def test_recover_pending_reads_only_cards_listed_with_a_pending_or_waiting_mark(self):
        self.complete(self.outcome())
        self.reconcile()
        marks = {"ummanu-5": e2e_record.AfterMergeMark(merge_sha="c" * 40, state="covered"),
                 "ummanu-6": e2e_record.AfterMergeMark(merge_sha="d" * 40, state="budget_wait",
                                                      decision="ummanu-7", holder="ummanu-7")}
        for ref, mark in marks.items():
            self.cards[ref] = {"id": int(ref.split("-")[-1]), "ref": ref, "project": "ummanu", "type": "code",
                "state": "done", "extensions": {"extra": {"e2e": e2e_record.E2eState(after_merge=mark).text()}}}
        self.cards["ummanu-8"] = {"id": 8, "ref": "ummanu-8", "project": "ummanu", "type": "code", "state": "ready"}
        snapshot = self.writer.reader.list()
        payload = {}
        e2e_after_merge._queue(payload, "ummanu")["budget_waits"].append(
            {"decision": "ummanu-7", "generation": 1, "scope": "sprint", "scope_ref": "sprint:1", "cards": []})
        shown = []
        show = self.writer.reader.show
        self.writer.reader.show = lambda ref: (shown.append(ref), show(ref))[1]
        with mock.patch.object(self.writer, "_card_superseded", wraps=self.writer._card_superseded) as superseded:
            self.assertEqual(e2e_after_merge._recover_pending(self.runtime, payload, {}, snapshot), [])
        listed = {self.source, self.carrier, "ummanu-6"}
        self.assertEqual({call.args[0] for call in superseded.call_args_list}, listed)
        self.assertEqual(superseded.call_count, 3)
        # Each pending card is read once, plus its carrier's run for the queued repo.
        self.assertEqual(sorted(shown), sorted([*listed, self.carrier, self.carrier]))
        queue = e2e_after_merge.queues(payload)["ummanu"]
        self.assertEqual({entry["ref"] for entry in queue["pending"]}, {self.source, self.carrier, "ummanu-6"})
        self.assertEqual(queue["budget_waits"][0]["cards"], ["ummanu-6"])

    def test_a_retry_settled_in_this_pass_is_recovered_in_this_pass(self):
        # The snapshot is read while both marks are still blocked; the retry returns them to pending.
        self.complete(self.outcome())
        payload = {}
        with mock.patch.object(e2e_after_merge, "_advance", return_value=[]):
            e2e_after_merge.reconcile_after_merge(self.runtime, payload, {})
        self.assertEqual(self.mark().state, "pending")
        queue = e2e_after_merge.queues(payload)["ummanu"]
        self.assertEqual({entry["ref"] for entry in queue["pending"]}, {self.source, self.carrier})

    def test_decline_settles_live_wait_retaining_evidence_and_charge(self):
        self.complete(self.outcome("decline"))
        self.reconcile()
        self.assertEqual(self.mark().state, "declined")
        self.assertEqual(self.mark().decision, self.operation)
        self.assertEqual(card_waits(self.show(self.source)), [])
        receipt = e2e_record.e2e_state(self.show(self.carrier)).after_merge_runs[0].disposition_result
        self.assertEqual(receipt["operation"], self.operation)

    def test_concrete_follow_up_is_live_holder_then_done_settles(self):
        self.follow_up()
        self.complete(self.outcome("follow_up", holder="ummanu-4"))
        self.reconcile()
        self.assertEqual(self.mark().holder, "ummanu-4")
        self.assertEqual(card_waits(self.show(self.source))[0]["holder"], "ummanu-4")
        self.cards["ummanu-4"]["state"] = "done"
        self.reconcile()
        self.assertEqual(self.mark().state, "declined")
        self.assertEqual(card_waits(self.show(self.source)), [])
        self.assertFalse(self.reconcile()["changed"])

    def test_bare_done_and_unresolved_or_mismatched_outcomes_are_neutral(self):
        for outcome in (None, self.outcome(run="another-run"), self.outcome(prior_effect="uncertain"),
                        self.outcome(budget=99), self.outcome(covered=[])):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.complete(outcome)
                result = self.reconcile()
                self.assertEqual(result["status"], "neutral")
                self.assertTrue(result["reason"])
                self.assertEqual((self.mark().state, self.mark().holder), ("blocked", ""))
                self.assertEqual(card_waits(self.show(self.source)), [])

    def test_native_po_authority_is_required_even_with_well_formed_record(self):
        for session, role in (("foreign-session", "po"), ("", "po"), ("po-session-1", "observer")):
            with self.subTest(session=session, role=role):
                self.complete(self.outcome(), session=session)
                self.events[0]["actor"]["role"] = role
                self.assertEqual(self.reconcile()["status"], "neutral")
        self.complete(self.outcome())
        self.events = []
        self.assertEqual(self.reconcile()["status"], "neutral")

    def test_later_bare_done_cannot_reuse_an_earlier_native_completion(self):
        self.complete(self.outcome())
        self.events.append({"record_type": "board.protocol_event", "ref": self.operation,
            "transition": {"source": "ready", "target": "done"}, "actor": {"role": "po"},
            "reason": "just Done", "data": {}, "request_id": "later-bare-done"})
        self.assertEqual(self.reconcile()["status"], "neutral")

    def test_recorded_sprint_session_rollover_does_not_invalidate_prior_completion(self):
        self.cards[self.operation]["sprint"] = "sprint:7"
        self.complete(self.outcome())
        completion_events = self.events
        self.writer.audit.events = lambda ref, **kw: completion_events if ref == self.operation else [
            {"kind": "po_session_set", "actor": {"role": "po"}, "payload": {"po_session": "po-session-1"}}]
        with mock.patch("ummanu.sprints.SprintReader.show", return_value={"status": "open", "po_session": "po-session-2"}):
            self.assertEqual(self.reconcile()["status"], "settled")

    def test_superseded_follow_up_becomes_neutral_without_dead_holder(self):
        self.follow_up()
        self.complete(self.outcome("follow_up", holder="ummanu-4"))
        self.reconcile()
        self.writer._card_superseded = lambda ref: ref == "ummanu-4"
        self.assertEqual(self.reconcile()["status"], "neutral")
        self.assertEqual(self.mark().holder, "")

    def test_missing_operation_or_unowned_follow_up_has_no_closed_holder(self):
        self.complete(self.outcome("follow_up", holder="ummanu-4"))
        self.assertEqual(self.reconcile()["status"], "neutral")
        self.follow_up(extensions={"extra": {}})
        self.assertEqual(self.reconcile()["status"], "neutral")
        del self.cards[self.operation]
        self.assertEqual(self.reconcile()["status"], "neutral")
        self.assertEqual(self.mark().holder, "")

    def test_missing_source_is_neutral_on_surviving_carrier_and_cannot_retry(self):
        self.complete(self.outcome())
        del self.cards[self.source]
        effect = self.reconcile()
        self.assertEqual(effect["status"], "neutral")
        self.assertIn("Covered source", effect["reason"])
        self.assertEqual(self.mark(self.carrier).holder, "")
        self.assertEqual(self.mark(self.carrier).state, "blocked")

    def test_transition_save_failure_rolls_back_and_recovery_applies_once(self):
        self.complete(self.outcome())
        self.fail_save = self.source
        with self.assertRaisesRegex(TaskError, "save interrupted"):
            self.reconcile()
        self.assertEqual(self.mark().state, "blocked")
        self.assertIsNone(e2e_record.e2e_state(self.show(self.carrier)).after_merge_runs[0].disposition_result)
        self.fail_save = ""
        self.assertTrue(self.reconcile()["changed"])
        self.assertFalse(self.reconcile()["changed"])

    def test_newer_merge_run_or_holder_is_preserved(self):
        for changes in ({"merge_sha": "new"}, {"dispatch_id": "new-run"}, {"decision": "ummanu-5"},
                        {"holder": "ummanu-5"}, {"state": "green"}):
            with self.subTest(changes=changes):
                self.setUp()
                state = e2e_record.e2e_state(self.show(self.source))
                for key, value in changes.items():
                    setattr(state.after_merge, key, value)
                self.cards[self.source]["extensions"]["extra"]["e2e"] = state.text()
                before = self.mark()
                self.complete(self.outcome())
                self.reconcile()
                self.assertEqual(self.mark(), before)
                self.assertEqual(self.mark(self.carrier).state, "pending")

    def test_replay_does_not_reset_later_budget_decline(self):
        self.complete(self.outcome())
        self.reconcile()
        state = e2e_record.e2e_state(self.show(self.source))
        state.after_merge.state, state.after_merge.note = "declined", "adapter removed"
        self.cards[self.source]["extensions"]["extra"]["e2e"] = state.text()
        self.reconcile()
        self.assertEqual(self.mark().state, "declined")

    def proposal(self):
        states = {}
        for covered in self.run.covered:
            state = e2e_record.e2e_state(self.show(covered["ref"]))
            state.after_merge.state = "covered"
            state.after_merge.dispatch_id = "next-run"
            state.after_merge.charged.append("next-run")
            if covered["ref"] == self.carrier:
                state.after_merge_runs.append(e2e_record.E2eRun(dispatch_id="next-run", sha=self.run.sha,
                    repo=self.run.repo, branch="pipeline-e2e/next-run", workflow=self.run.workflow,
                    intent_at=self.run.intent_at, placement="after_merge", charged_to="cards",
                    covered=copy.deepcopy(self.run.covered)))
            states[covered["ref"]] = state.text()
        return states

    def test_explicit_retry_still_admits_batch_all_or_none_against_actual_cap(self):
        self.complete(self.outcome())
        self.reconcile()
        capped = e2e_record.e2e_state(self.show(self.source))
        capped.after_merge.charged.extend(["older-1", "older-2"])
        self.cards[self.source]["extensions"]["extra"]["e2e"] = capped.text()
        before = copy.deepcopy(self.cards)
        result = self.writer.record_after_merge_intent(role="dispatcher", actor="dispatcher",
            states=self.proposal(), sprint="", carrier=self.carrier, dispatch_id="next-run")
        self.assertEqual(result, {"charged": False, "spent": [self.source]})
        self.assertEqual(self.cards, before)
        self.assertEqual(self.mark(self.carrier).charged, [self.run.dispatch_id])

    def test_retry_pending_does_not_override_newer_mark_at_atomic_admission(self):
        self.complete(self.outcome())
        self.reconcile()
        offered = self.proposal()
        current = e2e_record.e2e_state(self.show(self.source))
        current.after_merge.merge_sha = "new-merge"
        self.cards[self.source]["extensions"]["extra"]["e2e"] = current.text()
        before = copy.deepcopy(self.cards)
        result = self.writer.record_after_merge_intent(role="dispatcher", actor="dispatcher",
            states=offered, sprint="", carrier=self.carrier, dispatch_id="next-run")
        self.assertEqual(result, {"charged": False, "stale": True})
        self.assertEqual(self.cards, before)

    def test_recovered_timestamps_choose_newest_descendant_as_run_and_budget_carrier(self):
        self.complete(self.outcome())
        self.reconcile()
        self.runtime.host = SimpleNamespace()
        queue = {"pending": [
            {"ref": self.carrier, "merge_sha": "b" * 40, "repo": self.run.repo, "merged_at": 1},
            {"ref": self.source, "merge_sha": "a" * 40, "repo": self.run.repo, "merged_at": 2}],
            "run": None, "budget_waits": [], "cleanup": []}
        declaration = SimpleNamespace(after_merge=True, workflow="e2e.yml", deadline="2h")
        with mock.patch.object(e2e_after_merge, "declared_e2e", return_value=declaration), \
                mock.patch.object(e2e_after_merge, "is_ancestor", side_effect=lambda host, repo, base, head: base <= head), \
                mock.patch.object(e2e_after_merge, "_push_and_dispatch"), \
                mock.patch.object(e2e_after_merge, "_settle", return_value=(None, False)):
            result = e2e_after_merge._start(self.runtime, {}, {}, "ummanu", queue)
        self.assertEqual(result["pilot_ref"], self.carrier)
        latest = e2e_record.e2e_state(self.show(self.carrier)).after_merge_runs[-1]
        self.assertEqual(latest.sha, "b" * 40)
        self.assertEqual([item["ref"] for item in latest.covered], [self.source, self.carrier])
        self.assertEqual(latest.charged_to, "cards")
        for ref in (self.source, self.carrier):
            self.assertEqual(len(self.mark(ref).charged), 2)

    def test_bounded_record_preserves_normalized_metadata_without_money_or_origin(self):
        from ummanu.data import normalize_board_card
        self.complete(self.outcome())
        self.reconcile()
        raw = self.cards[self.carrier]["extensions"]["extra"]["e2e"]
        row = {"reference": self.carrier, "metadata": {"e2e": raw}}
        normalized = normalize_board_card(row, row)
        restored = e2e_record.E2eState.from_json(json.loads(normalized["metadata"]["e2e"]))
        self.assertEqual(restored.after_merge_runs[0].disposition_result["action"], "retry")
        self.assertEqual(restored.after_merge.holder, "")
        self.assertEqual(restored.dispatched, 1)
        self.assertNotIn("po_origin", normalized["metadata"])

    def test_stale_red_mark_and_carrier_run_save_preserve_newer_merge(self):
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge.merge_sha, state.after_merge.dispatch_id = "new", "new-run"
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        self.writer.record_after_merge_mark(role="dispatcher", actor="dispatcher", reference=self.carrier,
            carrier=self.carrier, run=self.run, changes={"state": "red", "decision": self.operation})
        self.writer.record_after_merge_run(role="dispatcher", actor="dispatcher", reference=self.carrier, run=self.run)
        self.assertEqual((self.mark(self.carrier).merge_sha, self.mark(self.carrier).dispatch_id), ("new", "new-run"))

    def test_degraded_read_is_not_completion_evidence_and_other_carrier_recovers(self):
        bad = "ummanu-0"
        listing = self.writer.reader.list()
        listing.append({**copy.deepcopy(self.cards[self.carrier]), "ref": bad})
        self.writer.reader.list = lambda: listing
        original = self.writer.reader.show
        self.writer.reader.show = lambda ref: (_ for _ in ()).throw(TaskError("backend_error", "old carrier unreadable", 1)) if ref == bad else original(ref)
        self.complete(self.outcome("decline"))
        results = e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
        self.assertTrue(any(row["status"] == "degraded" and row["pilot_ref"] == bad for row in results))
        self.assertEqual(self.mark().state, "declined")
        self.assertTrue(any(row["action"] == "e2e-after-merge-disposition-settled" for row in results))

    def test_committed_create_and_released_pending_reach_shared_consumer(self):
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge_runs[0].disposition = ""
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        for ref in (self.source, self.carrier):
            state = e2e_record.e2e_state(self.show(ref))
            state.after_merge.state, state.after_merge.decision = "pending", ""
            state.after_merge.dispatch_id = ""
            self.cards[ref]["extensions"]["extra"]["e2e"] = state.text()
        self.complete(self.outcome("decline"))
        result = e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
        self.assertEqual(self.mark().state, "declined")
        actual = e2e_record.e2e_state(self.show(self.carrier)).after_merge_runs[0]
        self.assertEqual(actual.disposition, self.operation)
        self.assertEqual(actual.disposition_result["action"], "decline")
        self.assertTrue(any(item["action"] == "e2e-after-merge-disposition-settled" for item in result))

    def test_publication_recovers_after_committed_transition_without_reapplying(self):
        self.complete(self.outcome("decline"))
        self.writer.comment.side_effect = TaskError("backend_error", "publication unavailable", 1)
        with self.assertRaisesRegex(TaskError, "publication unavailable"):
            e2e_after_merge._reconcile_disposition(self.runtime, {}, {}, self.carrier, self.run.dispatch_id)
        failed = self.writer.comment.call_args.kwargs
        self.assertEqual(self.mark().state, "declined")
        self.writer.comment.side_effect = None
        e2e_after_merge._reconcile_disposition(self.runtime, {}, {}, self.carrier, self.run.dispatch_id)
        retry = self.writer.comment.call_args_list[-2].kwargs
        self.assertEqual((retry["request_id"], retry["body"]), (failed["request_id"], failed["body"]))
        self.assertEqual(self.mark().state, "declined")

    def released_unresolved(self):
        operation = self.cards.pop(self.operation)
        self.created = None
        self.run.disposition = ""
        for ref in (self.source, self.carrier):
            state = e2e_record.e2e_state(self.show(ref))
            state.after_merge.decision, state.after_merge.holder = "", None
            state.after_merge.dispatch_id = ""
            if ref == self.carrier:
                state.after_merge_runs[0].disposition = ""
            self.cards[ref]["extensions"]["extra"]["e2e"] = state.text()
        return operation

    def test_released_history_without_owned_marks_never_creates_or_links_a_question(self):
        for stale in ("remerged", "superseded", "declined"):
            with self.subTest(stale=stale):
                self.setUp()
                self.released_unresolved()
                if stale == "superseded":
                    self.writer._card_superseded = lambda ref: ref in {self.source, self.carrier}
                else:
                    for ref in (self.source, self.carrier):
                        state = e2e_record.e2e_state(self.show(ref))
                        if stale == "remerged":
                            state.after_merge.merge_sha, state.after_merge.dispatch_id = "new", "new-run"
                        else:
                            state.after_merge.state = "declined"
                        self.cards[ref]["extensions"]["extra"]["e2e"] = state.text()
                before = copy.deepcopy(self.cards)
                with mock.patch.object(e2e_after_merge, "_create_disposition") as create:
                    for _ in range(2):
                        e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
                create.assert_not_called()
                self.assertEqual(self.cards, before)

    def test_released_mixed_marks_create_one_holder_only_for_current_obligation(self):
        operation = self.released_unresolved()
        state = e2e_record.e2e_state(self.show(self.source))
        state.after_merge.merge_sha, state.after_merge.dispatch_id = "new", "new-run"
        self.cards[self.source]["extensions"]["extra"]["e2e"] = state.text()
        before = self.show(self.source)
        def create(*args):
            self.cards[self.operation] = operation
            self.created = {"ref": self.operation, "actor": {"role": "dispatcher"}, "kind": "created", "outcome": "success"}
            return self.operation
        with mock.patch.object(e2e_after_merge, "_create_disposition", side_effect=create) as created:
            e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
            e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
        self.assertEqual(created.call_count, 1)
        self.assertEqual(self.show(self.source), before)
        self.assertEqual(self.mark(self.carrier).holder, self.operation)

    def test_remerge_at_locked_creation_admission_prevents_a_stale_question(self):
        self.released_unresolved()
        def remerge(*args):
            for ref in (self.source, self.carrier):
                state = e2e_record.e2e_state(self.show(ref))
                state.after_merge.merge_sha = "new"
                self.cards[ref]["extensions"]["extra"]["e2e"] = state.text()
            return []
        self.writer.client._query.side_effect = remerge
        with mock.patch.object(e2e_after_merge, "_create_disposition") as create:
            self.assertEqual(e2e_after_merge._disposition(self.runtime, "ummanu", self.carrier, self.run, "Resolve"), "")
        create.assert_not_called()

    def test_retained_sources_and_carrier_remain_owned_and_recover_pending(self):
        self.cards[self.source]["closed"] = self.cards[self.carrier]["closed"] = True
        with self.writer.after_merge_disposition_admission(role="dispatcher", actor="dispatcher",
                carrier=self.carrier, run=self.run) as owned:
            self.assertTrue(owned)
        self.complete(self.outcome())
        self.cards[self.operation]["closed"] = True
        self.reconcile()
        self.assertEqual(self.mark().state, "pending")
        self.assertEqual(card_waits(self.show(self.source))[0]["kind"], "run")
        payload = {}
        e2e_after_merge._recover_pending(self.runtime, payload, {}, self.writer.reader.list())
        self.assertEqual(len(e2e_after_merge.queues(payload)["ummanu"]["pending"]), 2)
        self.assertEqual(self.mark().charged, [self.run.dispatch_id])

    def test_retained_released_carrier_recovers_committed_operation_outside_active_listing(self):
        self.cards[self.carrier]["closed"] = True
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge_runs[0].disposition = ""
        state.after_merge.decision = ""
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        source = e2e_record.e2e_state(self.show(self.source))
        source.after_merge.decision = ""
        self.cards[self.source]["extensions"]["extra"]["e2e"] = source.text()
        self.writer.reader.restore_snapshot = lambda: {ref: self.show(ref) for ref in self.cards}
        self.writer.reader.list = lambda: [self.show(ref) for ref in self.cards if not self.cards[ref].get("closed")]
        self.complete(self.outcome("decline"))
        e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
        self.assertEqual(self.mark().state, "declined")
        self.assertEqual(self.mark(self.carrier).state, "declined")
        self.assertEqual(card_waits(self.show(self.carrier)), [])

    def test_projection_compare_and_swap_preserves_newer_mark_and_paid_records(self):
        expected = self.mark()
        newer = copy.deepcopy(expected)
        newer.holder = "ummanu-9"
        state = e2e_record.e2e_state(self.show(self.source))
        state.after_merge = newer
        self.cards[self.source]["extensions"]["extra"]["e2e"] = state.text()
        proposed = e2e_record.AfterMergeMark("next", charged=[self.run.dispatch_id])
        self.assertIsNone(self.writer.record_after_merge_projection(role="dispatcher", actor="dispatcher",
            reference=self.source, expected=expected, proposed=proposed))
        self.assertEqual(self.mark(), newer)

    def test_supersession_at_atomic_mark_and_intent_cannot_charge_or_replace(self):
        before = copy.deepcopy(self.cards)
        self.writer._card_superseded = lambda ref: ref == self.source
        self.writer.record_after_merge_mark(role="dispatcher", actor="dispatcher", reference=self.source,
            carrier=self.carrier, run=self.run, changes={"state": "red"})
        self.assertEqual(self.cards, before)
        self.writer._card_superseded = lambda ref: False
        self.complete(self.outcome())
        self.reconcile()
        before = copy.deepcopy(self.cards)
        self.writer._card_superseded = lambda ref: ref == self.source
        result = self.writer.record_after_merge_intent(role="dispatcher", actor="dispatcher",
            states=self.proposal(), sprint="", carrier=self.carrier, dispatch_id="next-run")
        self.assertEqual(result, {"charged": False, "stale": True})
        self.assertEqual(self.cards, before)

    def test_released_unmarked_new_merge_requires_latest_native_green_publication(self):
        previous = self.mark()
        proposed = e2e_record.AfterMergeMark("next", charged=list(previous.charged))
        def publication(sha):
            return {"ref": self.source, "kind": "commented", "outcome": "success", "actor": {"role": "dispatcher"},
                    "payload": {"post_merge_ci": {"result": "green", "merge_sha": sha}}}
        self.writer.audit.events = lambda *args, **kw: [publication("newer")]
        before = copy.deepcopy(self.cards)
        self.assertIsNone(self.writer.record_after_merge_projection(role="dispatcher", actor="dispatcher",
            reference=self.source, expected=previous, proposed=proposed, published_merge=True))
        self.assertEqual(self.cards, before)
        self.writer.audit.events = lambda *args, **kw: []
        with self.assertRaisesRegex(TaskError, "committed post-merge CI"):
            self.writer.record_after_merge_projection(role="dispatcher", actor="dispatcher",
                reference=self.source, expected=previous, proposed=proposed, published_merge=True)
        self.writer.audit.events = lambda *args, **kw: [publication("next")]
        self.assertIsNotNone(self.writer.record_after_merge_projection(role="dispatcher", actor="dispatcher",
            reference=self.source, expected=previous, proposed=proposed, published_merge=True))
        self.assertEqual((self.mark().merge_sha, self.mark().charged), ("next", previous.charged))

    def test_missing_committed_mark_is_degraded_without_discarding_queued_obligation(self):
        state = e2e_record.e2e_state(self.show(self.source))
        state.after_merge = None
        self.cards[self.source]["extensions"]["extra"]["e2e"] = state.text()
        queue = {"pending": [{"ref": self.source, "merge_sha": "a" * 40, "marked": True}]}
        before = copy.deepcopy(queue)
        with self.assertRaisesRegex(TaskError, "readable committed mark"):
            e2e_after_merge._mark_queued(self.runtime, {}, {}, queue)
        self.assertEqual(queue, before)

    def test_stale_red_history_cannot_manufacture_a_new_hotfix(self):
        self.released_unresolved()
        for ref in (self.source, self.carrier):
            state = e2e_record.e2e_state(self.show(ref))
            state.after_merge.merge_sha = "new"
            self.cards[ref]["extensions"]["extra"]["e2e"] = state.text()
        with mock.patch.object(e2e_after_merge, "_hotfix") as create:
            e2e_after_merge._red(self.runtime, "ummanu", self.carrier,
                e2e_record.e2e_state(self.show(self.carrier)), self.run)
        create.assert_not_called()

    def released_hotfix(self):
        operation = self.released_unresolved()
        hotfix = "ummanu-4"
        self.cards[hotfix] = {"id": 4, "ref": hotfix, "project": "ummanu", "type": "code", "state": "blocked"}
        self.hotfix_created = {"ref": hotfix, "kind": "created", "outcome": "success", "actor": {"role": "dispatcher"}}
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge_runs[0].resolution, state.after_merge_runs[0].hotfix = "red", hotfix
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        self.run.resolution, self.run.hotfix = "red", hotfix
        return operation, hotfix

    def install_operation(self, operation):
        self.cards[self.operation] = operation
        self.created = {"ref": self.operation, "actor": {"role": "dispatcher"}, "kind": "created", "outcome": "success"}
        return self.operation

    def test_released_red_hotfix_owns_route_even_when_every_source_has_remerged(self):
        operation, hotfix = self.released_hotfix()
        for ref in (self.source, self.carrier):
            state = e2e_record.e2e_state(self.show(ref))
            state.after_merge.merge_sha, state.after_merge.dispatch_id = "new", "new-run"
            self.cards[ref]["extensions"]["extra"]["e2e"] = state.text()
        before = {ref: self.mark(ref) for ref in (self.source, self.carrier)}
        with mock.patch.object(e2e_after_merge, "_create_disposition", side_effect=lambda *args: self.install_operation(operation)) as create:
            for _ in range(2):
                e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
            create.assert_called_once()
            self.assertEqual(card_waits(self.show(hotfix))[0]["holder"], self.operation)
            self.complete(self.outcome("decline"))
            for _ in range(2):
                e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
        self.assertEqual({ref: self.mark(ref) for ref in before}, before)
        route = e2e_record.e2e_state(self.show(hotfix)).hotfix_route
        self.assertEqual((route.carrier, route.run, route.result["action"]), (self.carrier, self.run.dispatch_id, "decline"))
        self.assertEqual(card_waits(self.show(hotfix)), [])
        self.assertEqual(self.show(hotfix)["blocked_by"], "")

    def test_terminal_or_superseded_hotfix_with_stale_sources_has_no_question(self):
        for terminal in ("done", "superseded"):
            with self.subTest(terminal=terminal):
                self.setUp()
                _, hotfix = self.released_hotfix()
                for ref in (self.source, self.carrier):
                    state = e2e_record.e2e_state(self.show(ref))
                    state.after_merge.merge_sha = "new"
                    self.cards[ref]["extensions"]["extra"]["e2e"] = state.text()
                if terminal == "done":
                    self.cards[hotfix]["state"] = "done"
                else:
                    self.writer._card_superseded = lambda ref, hotfix=hotfix: ref == hotfix
                before = copy.deepcopy(self.cards)
                with mock.patch.object(e2e_after_merge, "_create_disposition") as create:
                    e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
                create.assert_not_called()
                self.assertEqual(self.cards, before)

    def test_hotfix_follow_up_is_visible_then_retained_done_follow_up_settles(self):
        operation, hotfix = self.released_hotfix()
        self.install_operation(operation)
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge_runs[0].disposition = self.operation
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        self.follow_up()
        # Use a separate real planned card; the actual hotfix must remain blocked.
        self.cards["ummanu-5"] = {**self.cards.pop("ummanu-4"), "id": 5, "ref": "ummanu-5"}
        self.cards[hotfix] = {"id": 4, "ref": hotfix, "project": "ummanu", "type": "code", "state": "blocked"}
        self.complete(self.outcome("follow_up", holder="ummanu-5"))
        self.reconcile()
        self.assertEqual(card_waits(self.show(hotfix))[0]["holder"], "ummanu-5")
        self.cards["ummanu-5"].update(state="done", closed=True)
        self.reconcile()
        self.assertEqual(card_waits(self.show(hotfix)), [])
        self.assertEqual(e2e_record.e2e_state(self.show(hotfix)).hotfix_route.result["status"], "settled")

    def test_terminal_hotfix_route_roundtrips_schema_and_keeps_genuine_escalation(self):
        from jsonschema import Draft202012Validator

        from ummanu.board.owner_handover import OWNER_ESCALATION
        from ummanu.data import normalize_board_card
        operation, hotfix = self.released_hotfix()
        self.install_operation(operation)
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge_runs[0].disposition = self.operation
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        self.complete(self.outcome("decline"))
        self.reconcile()
        task = self.show(hotfix)
        schema = json.loads((Path(__file__).parents[1] / "src/ummanu/schemas/web-read.schema.json").read_text())
        Draft202012Validator({"$ref": "#/$defs/task_e2e", "$defs": schema["$defs"]}).validate(task["e2e"])
        raw = task["extensions"]["extra"]["e2e"]
        row = {"reference": hotfix, "metadata": {"e2e": raw}}
        normalized = normalize_board_card(row, row)
        restored = e2e_record.E2eState.from_json(json.loads(normalized["metadata"]["e2e"]))
        self.assertEqual(restored.hotfix_route, e2e_record.e2e_state(task).hotfix_route)
        self.cards[hotfix]["extensions"]["extra"][OWNER_ESCALATION] = json.dumps({"reason": "Actual unresolved steward escalation"})
        self.assertEqual(card_waits(self.show(hotfix))[0]["kind"], "owner")

    def test_hotfix_create_evidence_and_publication_failure_are_degraded_and_atomic(self):
        operation, hotfix = self.released_hotfix()
        self.install_operation(operation)
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge_runs[0].disposition = self.operation
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        self.complete(self.outcome("decline"))
        before = copy.deepcopy(self.cards)
        self.hotfix_created["ref"] = "foreign"
        with self.assertRaisesRegex(TaskError, "matching committed dispatcher"):
            self.reconcile()
        self.assertEqual(self.cards, before)
        self.hotfix_created["ref"] = hotfix
        self.writer.comment.side_effect = TaskError("backend_error", "publication interrupted", 1)
        with self.assertRaisesRegex(TaskError, "publication interrupted"):
            self.reconcile()
        self.assertEqual(self.cards, before)
        self.writer.comment.side_effect = None
        self.reconcile()
        self.assertEqual(e2e_record.e2e_state(self.show(hotfix)).hotfix_route.result["action"], "decline")

    def test_released_red_hotfix_mixed_marks_recovers_one_current_holder(self):
        operation, _hotfix = self.released_hotfix()
        state = e2e_record.e2e_state(self.show(self.carrier))
        state.after_merge.merge_sha, state.after_merge.dispatch_id = "new", "new-run"
        self.cards[self.carrier]["extensions"]["extra"]["e2e"] = state.text()
        before = self.mark(self.carrier)
        def create(*args):
            self.cards[self.operation] = operation
            self.created = {"ref": self.operation, "actor": {"role": "dispatcher"}, "kind": "created", "outcome": "success"}
            return self.operation
        with mock.patch.object(e2e_after_merge, "_create_disposition", side_effect=create) as created:
            e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
            e2e_after_merge.reconcile_after_merge(self.runtime, {}, {})
        self.assertEqual(created.call_count, 1)
        self.assertEqual(self.mark(self.carrier), before)
        self.assertEqual(self.mark(self.source).holder, self.operation)


if __name__ == "__main__":
    unittest.main()
