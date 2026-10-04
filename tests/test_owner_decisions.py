"""Standing decision validation and admission outcomes without a board or containers."""

from __future__ import annotations

import json
import copy
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock

from ummanu.board import e2e_record, owner_decisions, owner_events
from ummanu.board.production_rights import rights_note
from ummanu.board.sprint_write import SprintCreateIntent
from ummanu.data import normalize_sprint_entity
from ummanu.dispatch import e2e_after_merge, e2e_stage, observer
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import mint_gate_receipt
from ummanu.dispatch.state import DispatcherRecord
from ummanu.sprints import SprintWriter
from ummanu.tasks import TaskError


def answer(identifier="stop-1", kind="e2e_refusal", value="no_more_e2e", scope="sprint"):
    return {"id": identifier, "kind": kind, "scope": scope, "value": value,
            "quotation": "  No more e2e in this sprint.\nKeep the paid run.  "}


def recorded(entry=None):
    return owner_decisions.attributed([entry or answer()], {
        "actor": {"role": "po", "id": "po-session"}, "event_id": "evt_1",
        "request_id": "r-1", "occurred_at": "2026-10-04T00:00:00Z",
    })[0]


class DecisionTests(unittest.TestCase):
    def test_append_uses_audited_atomic_projection_and_binds_retries(self):
        writer = object.__new__(SprintWriter)
        writer.client = mock.Mock(_depth=1)
        writer.transactions = mock.Mock()
        writer.instance = None
        writer.thresholds = {"signal": 12, "hard": 30}
        sprint = {"id": "sprint_postgres_7", "ref": "sprint:7", "status": "open", "allowed_productions": [],
                  "e2e": {"budget": 3, "used": 1, "charges": [{"dispatch_id": "paid"}]}, "owner_decisions": []}
        writer.reader = SimpleNamespace(show=lambda _: copy.deepcopy(sprint))
        pending, committed = {}, {}
        writer.audit = mock.Mock()
        writer.audit.pending_event.side_effect = pending.get
        writer.audit.committed_event.side_effect = committed.get
        writer.audit.stage.side_effect = lambda request, event: pending.update({request: event})
        def append(request, event):
            committed[request] = event
            pending.pop(request, None)
            return event["event_id"]
        writer.audit.append.side_effect = append
        def save(method, **kwargs):
            self.assertEqual(method, "saveTaskMetadata")
            values = kwargs["values"]
            sprint["owner_decisions"] = json.loads(values[owner_decisions.FIELD])
            sprint["allowed_productions"] = json.loads(values["sprint_allowed_productions"])
            sprint["e2e"]["budget"] += int(values.get("sprint_e2e_budget_add", 0))
        writer.client.call.side_effect = save
        with tempfile.TemporaryDirectory() as directory, mock.patch("ummanu.sprints.update_active_sprint_projects"):
            writer.data_dir = Path(directory)
            entry = answer("grant-1", "e2e_grant", 2)
            kwargs = {"role": "po", "actor": "po-session", "reference": "sprint:7", "entries": [entry]}
            first = writer.record_owner_decisions(**kwargs, request_id="grant")
            self.assertEqual(sprint["e2e"]["budget"], 5)
            self.assertEqual(sprint["owner_decisions"][0]["recorded_by"]["event_id"], first["event_id"])
            self.assertEqual(writer.record_owner_decisions(**kwargs, request_id="grant")["event_id"], first["event_id"])
            writer.record_owner_decisions(**kwargs, request_id="second-request")
            self.assertEqual(sprint["e2e"]["budget"], 5)
            self.assertEqual(len(sprint["owner_decisions"]), 1)
            for request, entries in [("grant", [answer("other", "e2e_grant", 3)]),
                                     ("second-request", [answer("other", "e2e_grant", 3)]),
                                     ("new-request", [answer("grant-1", "e2e_grant", 3)]),
                                     ("invalid", [entry, dict(answer(), quotation="")])]:
                with self.subTest(request=request), self.assertRaises(TaskError):
                    writer.record_owner_decisions(**{**kwargs, "entries": entries}, request_id=request)
            writer.client.call.assert_called_once()
            self.assertEqual(sprint["e2e"]["charges"], [{"dispatch_id": "paid"}])

    def test_validation_preserves_verbatim_quotation_and_explicit_bounded_consent(self):
        entries = [answer(), answer("prod", "production", True, "ummanu"),
                   answer("grant", "e2e_grant", 2),
                   answer("advance", "advance_consent", {"action": "restart", "max_uses": 1}, "ummanu")]
        self.assertEqual(owner_decisions.parse_decisions(entries), entries)
        self.assertEqual(owner_decisions.stored_decisions(json.dumps([recorded()])), [recorded()])

    def test_malformed_or_unbounded_values_and_quotation_are_refused(self):
        invalid = [None, {}, [answer(), answer()], [dict(answer(), quotation=" \n")],
                   [dict(answer(), quotation=False)], [dict(answer(), extra="guess")],
                   [answer("grant", "e2e_grant", True)], [answer("grant", "e2e_grant", 0)],
                   [answer("grant", "e2e_grant", 2**31)], [answer("grant", "e2e_grant", 1.5)],
                   [answer("stop", value=False)], [answer("prod", "production", "yes", "ummanu")],
                   [answer("consent", "advance_consent", "whatever is needed")],
                   [answer("consent", "advance_consent", {"action": "restart", "max_uses": 0})]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                owner_decisions.parse_decisions(value)

    def test_precedence_follows_order_and_budget_permission_is_explicit(self):
        stop, grant = answer(), answer("grant", "e2e_grant", 2)
        self.assertEqual(owner_decisions.e2e_refusal([grant, stop]), stop)
        self.assertIsNone(owner_decisions.e2e_refusal([stop, grant]))
        self.assertEqual(owner_decisions.e2e_refusal([stop, answer("advance", "advance_consent", {"action": "e2e", "max_uses": 8})]), stop)
        prod = answer("prod", "production", True, "ummanu")
        deny = answer("deny", "production", False, "ummanu")
        self.assertEqual(owner_decisions.productions(["baseline"], [prod, deny]), ["baseline"])
        self.assertEqual(owner_decisions.productions([], [deny, prod]), ["ummanu"])
        note = rights_note("ummanu", "sprint:7", [], request_id="r", owner_decisions=[prod, deny])
        self.assertIn("sprint:7/deny refuses", note)
        self.assertIn(deny["quotation"], note)
        self.assertIn("without asking the owner again", note)

    def test_roles_cannot_manufacture_entries_or_masquerade(self):
        writer = object.__new__(SprintWriter)
        writer._record_owner_decisions_atomic = mock.Mock()
        for role, actor in [("observer", "observer"), ("dispatcher", "dispatcher"),
                            ("steward", "steward"), ("po", "observer")]:
            with self.subTest(role=role, actor=actor), self.assertRaises(TaskError):
                writer.record_owner_decisions(role=role, actor=actor, reference="sprint:7", entries=[answer()])
        writer._record_owner_decisions_atomic.assert_not_called()

    def test_normalized_export_and_create_intent_keep_decisions_and_real_budget_history(self):
        entries = [recorded()]
        sprint = {"ref": "sprint:7", "status": "open", "owner_decisions": entries,
                  "e2e": {"budget": 7, "used": 1, "charges": [{"card": "ummanu-1", "dispatch_id": "d", "at": "2026-10-04T00:00:00Z"}]}}
        normalized = normalize_sprint_entity(sprint)
        self.assertEqual(normalized["owner_decisions"], entries)
        self.assertEqual(normalized["e2e"], sprint["e2e"])
        intent = SprintCreateIntent.from_document({"role": "po", "owner_decisions": [answer()]})
        self.assertEqual(SprintCreateIntent.from_document(intent.to_document()).owner_decisions, (answer(),))
        self.assertNotIn("owner_decisions", normalize_sprint_entity({"ref": "sprint:1"}))

    def test_observer_sees_ids_and_grounds_before_resume(self):
        context = "\n".join(observer._observer_sprint_context({"owner_decisions": [recorded()], "comments": [], "resume": {"next_safe_step": "spend more"}}))
        self.assertLess(context.index("stop-1"), context.index("Saved observer resume"))
        self.assertIn("No more e2e in this sprint.", context)
        self.assertIn("Do not ask the owner again", context)

    def test_native_page_exposes_ids_quotes_and_attribution_with_html_escaping(self):
        from ummanu.web.pages import _sprint_tabs
        from ummanu.webproto.sprint_reads import _sprint_value
        entry = recorded(dict(answer(), quotation="<script>quoted</script>"))
        value = _sprint_value({"owner_decisions": [entry]})
        rendered = _sprint_tabs("sprint:7", value, {})
        self.assertIn("stop-1", rendered)
        self.assertIn("evt_1", rendered)
        self.assertIn("&lt;script&gt;quoted&lt;/script&gt;", rendered)
        self.assertNotIn("<script>quoted</script>", rendered)


class AdmissionTests(unittest.TestCase):
    def runtime(self):
        self.state = e2e_record.E2eState()
        self.task = {"ref": "ummanu-1", "sprint": "sprint:7", "type": "code", "project": "ummanu", "review": "skipped"}
        runtime = SimpleNamespace(reader=mock.Mock(), writer=mock.Mock(), host=mock.Mock(),
                                  owner="dispatcher", audit=mock.Mock(), save_records=mock.Mock())
        runtime.reader.sprint_e2e_budget.return_value = {"budget": 3, "used": 0, "refusal": recorded()}
        runtime.reader.show.return_value = self.task
        def persist(**kwargs):
            self.state = e2e_record.E2eState.from_json(json.loads(kwargs["state"]))
            self.task["extensions"] = {"extra": {"e2e": kwargs["state"]}}
        runtime.writer.record_e2e_state.side_effect = persist
        return runtime

    def test_premerge_refusal_blocks_before_dispatch_or_decision_even_with_room(self):
        runtime = self.runtime()
        record = DispatcherRecord(worker="w", workspace="/tmp/w", handle="", head="", review_head="",
                                  attempt_id="attempt-1", comment_baseline=0, review_baseline=0, state="validate", claimed_at=0)
        receipt = mint_gate_receipt(validated_sha="a"*40, base_sha="b"*40, gate_mode="github",
                                    required_checks=[{"name": "test", "conclusion": "SUCCESS", "url": ""}], check_set_identity='{"required":["test"]}')
        for used in (0, 3):
            with self.subTest(used=used):
                runtime.reader.sprint_e2e_budget.return_value["used"] = used
                with mock.patch.object(e2e_stage, "applies", return_value=True), mock.patch.object(e2e_stage, "declared_e2e", return_value=SimpleNamespace(after_merge=False)), mock.patch.object(e2e_stage, "_dispatch") as dispatch, mock.patch.object(e2e_stage, "_decision_card") as card, mock.patch.object(e2e_stage.attempt_accounting, "terminal_effect") as terminal:
                    result = e2e_stage.run_stage(runtime, self.task, record, {}, {}, "attempt-1", step="review", gate=lambda: (None, GateResult("green", "green", attestation=receipt)))
                self.assertEqual(result["status"], "blocked")
                self.assertIn("sprint:7/stop-1", terminal.call_args.kwargs["reason"])
                self.assertIn(recorded()["quotation"], terminal.call_args.kwargs["reason"])
                dispatch.assert_not_called()
                card.assert_not_called()
                self.assertIsNone(owner_events.person_wait({**self.task, "state": "blocked"}))
                with mock.patch.object(owner_events, "_sink", return_value=None), mock.patch.object(owner_events, "record_required_wait") as bell:
                    owner_events.record_person_wait({**self.task, "state": "blocked"}, "r", to=None)
                bell.assert_not_called()

    def test_pending_premerge_wait_uses_refusal_before_reading_old_decision(self):
        runtime = self.runtime()
        state = e2e_record.E2eState(budget_wait=e2e_record.BudgetWait("decision-1", 3, "sprint", "mark"))
        with mock.patch.object(e2e_stage, "_standing_decline", return_value={"status": "blocked"}) as decline:
            result = e2e_stage._budget_recheck(runtime, self.task, SimpleNamespace(), {}, {}, "a", state, step="review")
        self.assertEqual(result["status"], "blocked")
        runtime.reader.show.assert_not_called()
        decline.assert_called_once()

    def test_paid_premerge_run_settles_before_refusal_and_is_not_cancelled(self):
        runtime = self.runtime()
        run = e2e_record.E2eRun(dispatch_id="paid", sha="a"*40, repo="org/repo", branch="worker",
                                workflow="e2e.yml", intent_at="2026-10-04T00:00:00Z", deadline="2h")
        self.task["extensions"] = {"extra": {"e2e": e2e_record.E2eState(runs=[run]).text()}}
        gate = mock.Mock(side_effect=AssertionError("paid run must settle first"))
        with mock.patch.object(e2e_stage, "applies", return_value=True), mock.patch.object(e2e_stage, "declared_e2e", return_value=SimpleNamespace(after_merge=False)), mock.patch.object(e2e_stage, "_settle_run", return_value={"action": "e2e-waiting"}), mock.patch.object(e2e_stage, "_standing_decline") as decline:
            self.assertEqual(e2e_stage.run_stage(runtime, self.task, SimpleNamespace(), {}, {}, "a", step="review", gate=gate)["action"], "e2e-waiting")
        decline.assert_not_called()
        runtime.reader.sprint_e2e_budget.assert_not_called()
        runtime.host.stop.assert_not_called()

    def test_aftermerge_refuses_pending_and_later_cards_before_budget_card(self):
        runtime = self.runtime()
        for used in (0, 3):
            with self.subTest(used=used):
                runtime.reader.sprint_e2e_budget.return_value["used"] = used
                for index in (1, 2):
                    queue = {"pending": [{"ref": "ummanu-1"}], "budget_waits": ([{"cards": ["ummanu-1"], "decision": "old"}] if index == 1 else [])}
                    with mock.patch.object(e2e_after_merge, "_remark") as mark:
                        result = e2e_after_merge._standing_declines(runtime, {}, {}, "ummanu", queue)
                    self.assertEqual(result["action"], "e2e-after-merge-declined")
                    self.assertEqual(queue, {"pending": [], "budget_waits": []})
                    self.assertEqual(mark.call_args.kwargs["decision"], "sprint:7/stop-1")
                    self.assertIn(recorded()["quotation"], runtime.writer.comment.call_args.kwargs["body"])
                runtime.writer.create.assert_not_called()

    def test_uncovered_spent_budget_still_creates_po_decision(self):
        runtime = self.runtime()
        runtime.reader.sprint_e2e_budget.return_value["refusal"] = None
        runtime.audit.committed_event.return_value = None
        runtime.writer.create.return_value = {"task": {"ref": "decision-1"}}
        state = e2e_record.E2eState()
        result = e2e_stage._budget_spent(runtime, self.task, SimpleNamespace(), {}, {}, "a", state,
                                       "a"*40, e2e_stage._Spent(3, 3, ()), step="review")
        self.assertEqual(result["action"], "e2e-budget-waiting")
        self.assertEqual(runtime.writer.create.call_args.kwargs["task_type"], "decision")
        self.assertEqual(runtime.writer.create.call_args.kwargs["sprint"], "sprint:7")
