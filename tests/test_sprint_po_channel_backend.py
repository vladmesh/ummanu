"""A sprint's PO session and allowed productions on PostgreSQL: create, refuse, replay, read back, allow.

The unit suite (`tests/test_sprint_po_channel.py`) holds the rules over fakes; this is the same create
through the real `sprints` columns of revision 0016 and the real `po_sessions` table.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.sprints import SprintFixture
from tests.po_cli_fakes import FAKE_CLAUDE, eventually, unscoped_test_launch
from ummanu.board.owner_events import OwnerEventStore
from ummanu.board.po_execution import assignment, create_assignment
from ummanu.board.sql_cards import SqlCardClient
from ummanu.cli import main
from ummanu.dispatch.po_cards import ServicePoChannel, advance_po_card, claim_po_card
from ummanu.dispatch.state import new_attempt_id
from ummanu.po.runner import PoRunner
from ummanu.po.service import PoService, listening
from ummanu.po.store import COMPLETED, PoStore
from ummanu.sprint_observer import head_choice
from ummanu.sprints import SprintReader, SprintWriter
from ummanu.tasks import TaskError, TaskReader, TaskWriter


class ObserverPoAdmissionBackendTests(SprintFixture):
    def setUp(self):
        super().setUp()
        self.ref = self._create(goal="PO card channel", reference="sprint:7")["sprint"]["ref"]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)

    def card(self, kind="decision", **fields):
        return self.tasks.create(role="observer", actor="observer", project="ummanu", task_type=kind,
            title="Choose route", sprint=self.ref, request_id="create-" + kind, **fields)["task"]

    def entry(self, card="none", **fields):
        return {"selected_step": "Route question", "selected_why": "missing route",
                "rejected_alternatives": "unowned notice", "current_task": card,
                "dod_state": "pending", "next_safe_step": "Wait for PO decision.", **fields}

    def snapshot(self):
        return self.client._query("SELECT resume_id FROM sprints WHERE ref=%s", (self.ref,)), self.client._query(
            "SELECT body FROM sprint_comments WHERE sprint_ref=%s ORDER BY comment_id", (self.ref,))

    def assert_refused(self, operation, request):
        before = self.snapshot()
        with self.assertRaises(TaskError) as refusal:
            operation()
        self.assertEqual(refusal.exception.code, "po_card_required")
        self.assertIn("decision or operation", refusal.exception.message)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.writer.audit.committed_event(request))
        self.assertTrue(self.writer.audit.events(self.ref, kind="po_channel_denied"))

    def test_request_comment_refusal_is_audited_without_consuming_corrected_retry(self):
        for index, body in enumerate(("[observer:request] Assign the route", "PO, please decide.",
                                     "Прошу ПО выбрать маршрут.", "Ждём решения ПО.")):
            request = f"request-{index}"
            operation = lambda body=body, request=request: self.writer.comment(role="observer", actor="observer", reference=self.ref,
                body=body, request_id=request)
            self.assert_refused(operation, request)
            corrected = self.writer.comment(role="observer", actor="observer", reference=self.ref,
                body="Evidence recorded on the card.", request_id=request)
            repeated = self.writer.comment(role="observer", actor="observer", reference=self.ref,
                body="Evidence recorded on the card.", request_id=request)
            self.assertEqual(corrected["event_id"], repeated["event_id"])
        for index, body in enumerate(('Evidence: "Wait for PO decision."', "Не ждём решения ПО.",
                                     "Implement PO routing next.", "PO session is recorded.")):
            self.writer.comment(role="observer", actor="observer", reference=self.ref, body=body,
                                request_id=f"note-{index}")
        self.writer.comment(role="po", actor="po", reference=self.ref, body="PO, please decide.", request_id="po-comment")

    def test_bare_wait_and_bad_card_types_states_and_sprints_are_atomic_refusals(self):
        self.assert_refused(lambda: self.writer.resume(role="observer", actor="observer", reference=self.ref,
            entry=self.entry(), delivery_id="delivery-1", through_event="event-1", request_id="bare"), "bare")
        code = self.card("code")
        decision = self.card()
        for ref in ("missing-999", code["ref"], "issue:open"):
            entry = self.entry(ref, po_request={"card": ref, "action": "choose route"})
            self.assert_refused(lambda entry=entry: self.writer.resume(role="observer", actor="observer", reference=self.ref,
                entry=entry, request_id="invalid-card"), "invalid-card")
        for state in ("blocked", "done"):
            self.client.move(self.client.key_of(decision["ref"]), state)
            self.assert_refused(lambda: self.writer.resume(role="observer", actor="observer", reference=self.ref,
                entry=self.entry(decision["ref"], po_request={"card": decision["ref"], "action": "choose route"}),
                request_id="bad-state"), "bad-state")
        self.client.move(self.client.key_of(decision["ref"]), "ready")
        self.arrange_record_active(decision["ref"], active=False)
        self.assert_refused(lambda: self.writer.resume(role="observer", actor="observer", reference=self.ref,
            entry=self.entry(decision["ref"], po_request={"card": decision["ref"], "action": "choose route"}),
            request_id="closed-card"), "closed-card")
        self.client._execute("UPDATE tasks SET archived=false WHERE task_ref=%s", (decision["ref"],))
        self.client.call("saveTaskMetadata", task_id=self.client.key_of(decision["ref"]), values={"sprint_ref": ""})
        self.assert_refused(lambda: self.writer.resume(role="observer", actor="observer", reference=self.ref,
            entry=self.entry(decision["ref"], po_request={"card": decision["ref"], "action": "choose route"}),
            request_id="foreign-card"), "foreign-card")

    def test_corrected_typed_wait_acknowledges_exact_pair_once_and_survives_export(self):
        card = self.card()
        old = self.entry(card["ref"], next_safe_step="Inspect evidence and implement the future PO route.")
        self.writer.resume(role="observer", actor="observer", reference=self.ref, entry=old, request_id="historical")
        self.assertNotIn("po_request", self.sprint(self.ref)["resume"])
        self.assert_refused(lambda: self.writer.resume(role="observer", actor="observer", reference=self.ref,
            entry=self.entry(card["ref"]), request_id="wait", delivery_id="delivery-1", through_event="event-1"), "wait")
        entry = self.entry(card["ref"], po_request={"card": card["ref"], "action": "Choose route"})
        accepted = self.writer.resume(role="observer", actor="observer", reference=self.ref, entry=entry,
            request_id="wait", delivery_id="delivery-1", through_event="event-1")
        repeated = self.writer.resume(role="observer", actor="observer", reference=self.ref, entry=entry,
            request_id="wait", delivery_id="delivery-1", through_event="event-1")
        self.assertEqual(accepted["event_id"], repeated["event_id"])
        event = self.writer.audit.committed_event("wait")
        self.assertEqual((event["payload"]["delivery_id"], event["payload"]["through_event"]), ("delivery-1", "event-1"))
        self.assertEqual(self.client._query("SELECT po_request FROM sprint_resumes WHERE resume_id="
            "(SELECT resume_id FROM sprints WHERE ref=%s)", (self.ref,))[0][0], entry["po_request"])
        from ummanu.data import normalize_sprint_entity
        from ummanu.restore import _restore_sprint_metadata
        normalized = normalize_sprint_entity(self.sprint(self.ref))
        self.assertEqual(normalized["resume"]["po_request"], entry["po_request"])
        restored = _restore_sprint_metadata(normalized)
        self.assertEqual(json.loads(restored["sprint_resume"])["po_request"], entry["po_request"])

    def test_done_race_and_comment_failure_roll_back_resume_and_delivery(self):
        card = self.card()
        entry = self.entry(card["ref"], po_request={"card": card["ref"], "action": "choose route"})
        validate = self.writer._validate_po_step
        def completed_before_validation(reference, resume):
            self.client.move(self.client.key_of(card["ref"]), "done")
            validate(reference, resume)
        with mock.patch.object(self.writer, "_validate_po_step", side_effect=completed_before_validation):
            self.assert_refused(lambda: self.writer.resume(role="observer", actor="observer", reference=self.ref,
                entry=entry, request_id="race", delivery_id="d", through_event="e"), "race")
        self.assertEqual(TaskReader(self.client).show(card["ref"])["state"], "ready", "card mutation rolled back too")
        before = self.snapshot()
        with self.named_failure("record_comment", error=RuntimeError("comment failure")), self.assertRaises(TaskError):
            self.writer.resume(role="observer", actor="observer", reference=self.ref, entry=entry,
                request_id="rollback", delivery_id="d", through_event="e")
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.writer.audit.committed_event("rollback"))
        self.writer.resume(role="observer", actor="observer", reference=self.ref, entry=entry,
            request_id="rollback", delivery_id="d", through_event="e")

    def test_concurrent_committed_completion_is_seen_at_the_locked_admission_boundary(self):
        card = self.card()
        entry = self.entry(card["ref"], po_request={"card": card["ref"], "action": "choose route"})
        # One client's transactions serialize across threads. A separate client to
        # the same store lets this probe reach the real PostgreSQL row lock.
        resumer = SqlCardClient(self.client.credentials, self.client.instance_dir)
        self.addCleanup(resumer.close)
        writer = SprintWriter(resumer, data_dir=self.tmp.name, instance=self.instance)
        reached_lock = threading.Event()
        finished = threading.Event()
        query = resumer._query
        resume_pids = []
        outcomes = []
        def queries(sql, params=()):
            if sql.startswith("SELECT task_ref FROM tasks") and "FOR UPDATE" in sql:
                resume_pids.append(resumer.connection.info.backend_pid)
                reached_lock.set()
            return query(sql, params)
        def write():
            try:
                writer.resume(role="observer", actor="observer", reference=self.ref, entry=entry,
                    request_id="concurrent", delivery_id="d", through_event="e")
            except TaskError as exc:
                outcomes.append(exc.code)
            except Exception as exc:
                outcomes.append(exc)
            finally:
                finished.set()
        before = self.snapshot()
        thread = threading.Thread(target=write)
        with mock.patch.object(resumer, "_query", side_effect=queries):
            try:
                with self.client.transaction():
                    self.client._execute("UPDATE tasks SET state='done' WHERE task_ref=%s", (card["ref"],))
                    completion_pid = self.client.connection.info.backend_pid
                    thread.start()
                    self.assertTrue(reached_lock.wait(10), "resume never reached the card lock")
                    eventually(lambda: self.client._query(
                        "SELECT %s = ANY(pg_blocking_pids(%s))", (completion_pid, resume_pids[0]))[0][0],
                        "PostgreSQL did not block resume behind completion", timeout=10)
                    self.assertFalse(finished.is_set(), "resume settled before completion committed")
                    self.assertEqual(self.snapshot(), before)
                    self.assertIsNone(self.writer.audit.committed_event("concurrent"))
            finally:
                # Release the transaction before joining, including on a failed
                # assertion, so no writer leaks into fixture/database teardown.
                if thread.ident is not None:
                    thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(outcomes, ["po_card_required"])
        self.assertEqual(TaskReader(resumer).show(card["ref"])["state"], "done")
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.writer.audit.committed_event("concurrent"))
        self.assertTrue(self.writer.audit.events(self.ref, kind="po_channel_denied"))
        operation = self.card("operation", touches_production="none")
        corrected = self.entry(operation["ref"], po_request={"card": operation["ref"], "action": "choose route"})
        accepted = writer.resume(role="observer", actor="observer", reference=self.ref, entry=corrected,
            request_id="concurrent", delivery_id="d", through_event="e")
        repeated = writer.resume(role="observer", actor="observer", reference=self.ref, entry=corrected,
            request_id="concurrent", delivery_id="d", through_event="e")
        self.assertEqual(accepted["event_id"], repeated["event_id"])
        event = writer.audit.committed_event("concurrent")
        self.assertEqual((event["payload"]["delivery_id"], event["payload"]["through_event"]), ("d", "e"))
        self.assertEqual(self.sprint(self.ref)["resume"]["po_request"], corrected["po_request"])
        self.assertEqual(len(self.snapshot()[1]), len(before[1]) + 1)
        self.assertEqual(TaskReader(resumer).show(card["ref"])["state"], "done")


class StandalonePoExecutionBackendTests(SprintFixture):
    def setUp(self):
        super().setUp()
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)
        self.reader = TaskReader(self.client)
        self.request = "dispatcher-e2e-cap-ummanu-12-3"
        self.fields = dict(role="dispatcher", actor="dispatcher", project="ummanu", task_type="decision",
            title="PO budget disposition", description="GATE: choose a safe disposition without a grant",
            po_execution=create_assignment(self.request, "e2e_budget", ["ummanu-12"]), request_id=self.request)

    def test_assignment_create_dedup_and_route_persistence_are_atomic(self):
        before = self.client.card_count()
        with mock.patch.object(self.tasks, "_create_backend", side_effect=RuntimeError("creation failure")), self.assertRaises(RuntimeError):
            self.tasks.create(**self.fields)
        self.assertEqual(self.client.card_count(), before)
        self.assertIsNone(self.tasks.audit.committed_event(self.request))
        first = self.tasks.create(**self.fields)
        repeated = self.tasks.create(**self.fields)
        self.assertEqual(first["task"]["ref"], repeated["task"]["ref"])
        self.assertEqual(self.client.card_count(), before + 1)
        ref = first["task"]["ref"]
        route = assignment(self.reader.show(ref))
        route.initial = {"replaces": "", "via": "create_session", "session": "assigned-session"}
        route.executor = "assigned-session"
        self.tasks.record_po_execution(role="dispatcher", actor="dispatcher", reference=ref, state=route.text())
        self.assertEqual(TaskReader(self.client).show(ref)["po_execution"]["executor"], "assigned-session")
        stale = assignment(self.reader.show(ref))
        route.successors["assigned-session"] = {"replaces": "assigned-session", "via": "create_session", "session": "successor"}
        route.executor = "successor"
        self.tasks.record_po_execution(role="dispatcher", actor="dispatcher", reference=ref, state=route.text())
        stale.initial = {}
        self.tasks.record_po_execution(role="dispatcher", actor="dispatcher", reference=ref, state=stale.text())
        current = assignment(self.reader.show(ref))
        self.assertEqual((current.initial["session"], current.executor), ("assigned-session", "successor"))
        self.assertEqual(current.successors["assigned-session"]["session"], "successor")
        self.assertNotIn("origin", self.reader.show(ref))
        route.sources = ("ummanu-999",)
        with self.assertRaises(TaskError):
            self.tasks.record_po_execution(role="dispatcher", actor="dispatcher", reference=ref, state=route.text())
        self.assertEqual(self.reader.show(ref)["po_execution"]["sources"], ["ummanu-12"])
        with self.client.transaction():
            self.assertEqual(self.client._query("SELECT count(*) FROM origin_returns WHERE task_ref=%s", (ref,)), [(0,)])
        self.assertEqual(OwnerEventStore(self.client.credentials).events(), [])

    def test_generic_outside_sprint_creation_and_unrelated_operation_are_still_refused(self):
        before = self.client.card_count()
        for fields in ({"po_execution": None}, {"role": "po", "actor": "po"},
                       {"request_id": "generic-request"}, {"task_type": "operation"}):
            with self.subTest(fields=fields), self.assertRaises(TaskError):
                self.tasks.create(**{**self.fields, **fields})
        self.assertEqual(self.client.card_count(), before)

    def test_unowned_question_native_service_submission_restart_and_completion(self):
        data = Path(self.tmp.name)
        (data / "po").mkdir()
        executable = data / "fake-claude"
        executable.write_text(FAKE_CLAUDE)
        executable.chmod(0o700)
        store = PoStore(self.client.credentials)
        runner = PoRunner(store, data, executables={"claude": str(executable)},
            turn_launcher=unscoped_test_launch, env={**os.environ, "FAKE_LOG": str(data / "cli.log")})
        service = PoService(runner, data_dir=data, instance=self.instance)
        service.start()
        thread = threading.Thread(target=service.run, kwargs={"tick": 0.05, "say": lambda _: None})
        thread.start()
        def stop():
            (data / "cli.log.gate").touch()
            service.stop()
            thread.join(10)
            for live in list(runner._live.values()):
                if live.process.poll() is None:
                    live.process.kill()
                live.thread.join(5)
        self.addCleanup(stop)
        self.enterContext(listening(service))
        channel = ServicePoChannel(data, self.instance)
        channel._store = store
        task = self.tasks.create(**self.fields)["task"]
        runtime = SimpleNamespace(owner="dispatcher", reader=self.reader, writer=self.tasks,
            audit=self.tasks.audit, po=channel, sprints=self.sprint_reader(), save_records=lambda *_: None)
        records, payload, attempt = {}, {}, new_attempt_id()
        submitted = claim_po_card(runtime, task, records, payload, attempt)
        self.assertEqual(submitted["action"], "po-card-submitted", submitted)
        session = submitted["po_session"]
        self.assertEqual(self.reader.show(task["ref"])["po_execution"]["executor"], session)
        eventually(lambda: bool(store.turns(session)), "assigned turn was never started")
        records.clear()
        recovered = advance_po_card(runtime, self.reader.show(task["ref"]), records, payload, attempt)
        self.assertIn(recovered["action"], {"po-card-queued", "po-card-turn-running"})
        self.assertEqual(len(store.sessions()), 1)
        self.tasks.complete(role="po", actor="po", reference=task["ref"], kind="decision",
            body="## Decision\nNo further paid runs.\n\n## How to verify\nRead the preserved three runs.",
            po_session=session, request_id="complete-assignment")
        self.assertEqual(advance_po_card(runtime, self.reader.show(task["ref"]), records, payload, attempt)["action"], "po-card-closed")
        (data / "cli.log.gate").touch()
        eventually(lambda: store.turns(session)[0].state == COMPLETED, "assigned turn did not finish")
        self.assertEqual(len(store.turns(session)), 1)
        self.assertFalse(any(event.event_class == "needs_owner" for event in OwnerEventStore(self.client.credentials).events()))
        self.assertNotIn("origin", self.reader.show(task["ref"]))
        self.assertEqual(self.client._query("SELECT count(*) FROM origin_returns WHERE task_ref=%s", (task["ref"],)), [(0,)])


class SprintPoChannelBackendTests(SprintFixture):
    def decision(self, identifier="grant-1", kind="e2e_grant", value=2, scope="sprint"):
        return {"id": identifier, "scope": scope, "kind": kind, "value": value,
                "quotation": "  Two more e2e runs.\nOnly this sprint.  "}

    def test_quoted_create_append_and_sql_readback_apply_grants_once_and_supersede(self) -> None:
        production = self.decision("prod-1", "production", True, "ummanu")
        grant = self.decision()
        created = self._create(goal="quoted", reference="sprint:7", standing_decisions=[production, grant], request_id="quoted-create")
        entries = created["sprint"]["owner_decisions"]
        self.assertEqual([entry["id"] for entry in entries], ["prod-1", "grant-1"])
        self.assertEqual(entries[1]["quotation"], grant["quotation"])
        self.assertEqual(entries[1]["recorded_by"]["event_id"], created["event_id"])
        self.assertEqual(created["sprint"]["e2e"]["budget"], 5)
        self.assertEqual(created["sprint"]["allowed_productions"], ["ummanu"])
        self.assertEqual(self._create(goal="quoted", reference="sprint:7", standing_decisions=[production, grant], request_id="quoted-create")["event_id"], created["event_id"])
        self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[grant], request_id="same-grant")
        deny = self.decision("deny-prod", "production", False, "ummanu")
        stop = self.decision("stop-1", "e2e_refusal", "no_more_e2e")
        result = self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[deny, stop], request_id="stop")
        self.assertEqual(result["sprint"]["allowed_productions"], [])
        self.assertEqual(result["sprint"]["e2e"]["budget"], 5)
        self.assertEqual(self.client.call("getSprintE2eBudget", sprint_ref="sprint:7")["refusal"]["id"], "stop-1")
        charge = self.client.call("chargeSprintE2e", sprint_ref="sprint:7", task_ref="ummanu-1", dispatch_id="refused", at="2026-10-04T00:00:00Z")
        self.assertFalse(charge["charged"])
        self.assertEqual(charge["used"], 0)
        self.assertEqual(charge["charges"], [])
        with self.assertRaises(TaskError):
            self.writer.allow_production(role="po", actor="po", reference="sprint:7", project="ummanu", reason="own judgement", request_id="bypass")
        resumed = self.decision("grant-2", value=1)
        result = self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[resumed], request_id="resume")
        again = self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[resumed], request_id="resume")
        self.assertEqual(again["event_id"], result["event_id"])
        self.assertIsNone(self.client.call("getSprintE2eBudget", sprint_ref="sprint:7")["refusal"])
        self.assertEqual(self.sprint("sprint:7")["e2e"]["budget"], 6)
        self.assertEqual(self.client._query("SELECT owner_decisions FROM sprints WHERE ref='sprint:7'")[0][0], result["sprint"]["owner_decisions"])

    def test_invalid_quoted_batches_and_roles_write_no_partial_state(self) -> None:
        self._create(goal="quoted", reference="sprint:7")
        before = self.client._query("SELECT allowed_productions,e2e_budget,owner_decisions FROM sprints")
        invalid = [dict(self.decision(), quotation=""), dict(self.decision(), value=-1),
                   self.decision("prod", "production", True, "unknown-project"), dict(self.decision(), value=True)]
        for entry in invalid:
            with self.subTest(entry=entry), self.assertRaises(TaskError):
                self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[self.decision("valid"), entry], request_id="bad-batch")
            self.assertEqual(self.client._query("SELECT allowed_productions,e2e_budget,owner_decisions FROM sprints"), before)
        for role, actor in [("observer", "observer"), ("dispatcher", "dispatcher"), ("po", "observer")]:
            with self.subTest(role=role), self.assertRaises(TaskError):
                self.writer.record_owner_decisions(role=role, actor=actor, reference="sprint:7", entries=[self.decision()], request_id="bad-role")
        self.assertEqual(self.client._query("SELECT allowed_productions,e2e_budget,owner_decisions FROM sprints"), before)
        self.assertEqual(self.writer.audit.events("sprint:7", kind="owner_decisions_recorded"), [])

    def test_invalid_create_decisions_leave_no_sprint_or_audit_record(self) -> None:
        for entry in [dict(self.decision(), quotation=""), dict(self.decision(), value=True),
                      self.decision("prod", "production", True, "unknown-project")]:
            with self.subTest(entry=entry), self.assertRaises(TaskError):
                self._create(goal="invalid", reference="sprint:7", standing_decisions=[entry], request_id="invalid-create")
            self.assert_nothing_was_written()

    def test_reused_ids_or_requests_cannot_change_content_or_spend_a_grant_again(self) -> None:
        self._create(goal="quoted", reference="sprint:7")
        self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[self.decision()], request_id="grant")
        self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[self.decision()], request_id="another-request")
        for request in ("grant", "new-request"):
            with self.subTest(request=request), self.assertRaises(TaskError):
                self.writer.record_owner_decisions(role="po", actor="po", reference="sprint:7", entries=[self.decision(value=3)], request_id=request)
        self.assertEqual(self.sprint("sprint:7")["e2e"]["budget"], 5)
        self.assertEqual(len(self.sprint("sprint:7")["owner_decisions"]), 1)

    def add_session(self, session_id: str, state: str = "open") -> None:
        with self.client.transaction():
            self.client._execute(
                "INSERT INTO po_sessions (session_id, cli, model, cwd, created_at, state, closed_at, closed_by) "
                "VALUES (%s, 'claude', 'opus', '/po', now(), %s, "
                "CASE WHEN %s = 'closed' THEN now() END, CASE WHEN %s = 'closed' THEN 'owner' END)",
                (session_id, state, state, state),
            )

    def assert_nothing_was_written(self) -> None:
        self.assertEqual(self._events(), [])
        self.assertEqual(self.sprint_record_count(), 0)

    def test_both_fields_are_stored_and_read_back_by_show_and_status(self) -> None:
        self.add_session("s-open")
        created = self._create(
            goal="with both",
            reference="sprint:both",
            po_session="s-open",
            allowed_productions=["ummanu", "other"],
        )
        self.assertEqual(created["sprint"]["po_session"], "s-open")
        shown = self.sprint("sprint:both")
        self.assertEqual(
            (shown["po_session"], shown["allowed_productions"]), ("s-open", ["ummanu", "other"])
        )
        status = SprintReader(self.client, data_dir=self.tmp.name).status("sprint:both")  # type: ignore[arg-type]
        self.assertEqual(
            (status["po_session"], status["allowed_productions"]), ("s-open", ["ummanu", "other"])
        )

    def test_a_sprint_created_with_neither_reads_null_and_empty(self) -> None:
        self._create(goal="with neither", reference="sprint:neither")
        shown = self.sprint("sprint:neither")
        self.assertEqual((shown["po_session"], shown["allowed_productions"]), (None, []))
        self.assertEqual(
            self.client._query(
                "SELECT po_session, allowed_productions FROM sprints WHERE ref = 'sprint:neither'"
            ),
            [(None, [])],
        )

    def test_an_unknown_or_closed_session_and_an_unknown_production_write_nothing(self) -> None:
        self.add_session("s-closed", "closed")
        for options in (
            {"po_session": "s-nowhere"},
            {"po_session": "s-closed"},
            {"allowed_productions": ["not-registered"]},
        ):
            with self.subTest(options=options):
                with self.assertRaises(TaskError) as raised:
                    self._create(goal="refused", reference="sprint:refused", **options)
                self.assertEqual(raised.exception.code, "validation")
                self.assert_nothing_was_written()

    def test_a_repeat_replays_and_the_same_id_with_another_session_is_refused(self) -> None:
        self.add_session("s-1")
        self.add_session("s-2")
        first = self._create(goal="once", reference="sprint:once", request_id="create-once", po_session="s-1")
        again = self._create(goal="once", reference="sprint:once", request_id="create-once", po_session="s-1")
        self.assertEqual(again["event_id"], first["event_id"])
        with self.assertRaises(TaskError) as raised:
            self._create(goal="once", reference="sprint:once", request_id="create-once", po_session="s-2")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.sprint("sprint:once")["po_session"], "s-1")

    def test_the_resolver_s_record_replaces_the_session_once(self) -> None:
        self._create(goal="resolved", reference="sprint:resolved")
        for _ in range(2):
            self.writer.set_po_session(
                role="po",
                actor="po-service",
                reference="sprint:resolved",
                session_id="s-new",
                request_id="rec-1",
            )
        self.assertEqual(self.sprint("sprint:resolved")["po_session"], "s-new")
        recorded = [event for event in self._events() if event.get("kind") == "po_session_set"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["actor"], {"role": "po", "id": "po-service"})

    def allow(self, project: str, request_id: str, **fields: str) -> dict:
        call = {"role": "po", "actor": "po", "reference": "sprint:allow", "project": project,
                "reason": "ummanu is the development server", "request_id": request_id, **fields}
        return self.writer.allow_production(**call)  # type: ignore[arg-type]

    def allowances(self) -> list[dict]:
        return [event for event in self._events() if event.get("kind") == "production_allowed"]

    def test_allow_production_extends_the_column_once_with_its_audit_event(self) -> None:
        """`sprint allow-production` (secretary-1769): the column, the event, the replays, the no-op."""
        self._create(goal="allow", reference="sprint:allow", allowed_productions=["other"])

        answer = self.allow("ummanu", "allow-1")

        self.assertEqual(answer["action"], "production_allowed")
        self.assertEqual(answer["sprint"]["allowed_productions"], ["other", "ummanu"])
        self.assertEqual(
            self.client._query("SELECT allowed_productions FROM sprints WHERE ref = 'sprint:allow'"),
            [(["other", "ummanu"],)],
        )
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], ["other", "ummanu"])
        status = SprintReader(self.client, data_dir=self.tmp.name).status("sprint:allow")  # type: ignore[arg-type]
        self.assertEqual(status["allowed_productions"], ["other", "ummanu"])
        [event] = self.allowances()
        self.assertEqual(
            (event["ref"], event["actor"], event["payload"], event["request_id"]),
            ("sprint:allow", {"role": "po", "id": "po"},
             {"project": "ummanu", "reason": "ummanu is the development server"}, "allow-1"),
        )
        # The same request id is the same write; a project already allowed writes nothing new.
        self.assertEqual(self.allow("ummanu", "allow-1")["event_id"], answer["event_id"])
        self.assertEqual(self.allow("ummanu", "allow-2")["action"], "already_allowed")
        self.assertEqual(len(self.allowances()), 1)
        with self.assertRaises(TaskError) as raised:
            self.allow("other", "allow-1")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], ["other", "ummanu"])

    def test_allow_production_refusals_write_nothing(self) -> None:
        self._create(goal="allow", reference="sprint:allow")
        for fields, code in (
            ({"role": "observer", "actor": "observer"}, "role_forbidden"),
            ({"role": "po", "actor": "observer"}, "role_masquerade"),
            ({"project": "not-registered"}, "validation"),
        ):
            with self.subTest(fields=fields), self.assertRaises(TaskError) as raised:
                self.allow(str(fields.pop("project", "ummanu")), "refused-1", **fields)
            self.assertEqual(raised.exception.code, code)
        for status in ("closed", "stopped"):
            reference = f"sprint:allow-{status}"
            self.writer.restore_create(
                reference=reference,
                goal="seeded",
                observer=head_choice("codex-observer"),
                status=status,
                request_id=f"fixture-{reference}",
            )
            with self.subTest(status=status), self.assertRaises(TaskError) as raised:
                self.allow("ummanu", f"refused-{status}", reference=reference)
            self.assertEqual((raised.exception.code, raised.exception.exit_code), ("closed", 3))
            self.assertEqual(self.sprint(reference)["allowed_productions"], [])
        self.assertEqual(self.allowances(), [])
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], [])

    def test_the_command_takes_the_actor_from_board_actor(self) -> None:
        self._create(goal="allow", reference="sprint:allow")
        out = io.StringIO()
        with (
            self.board_injected(),
            mock.patch.dict(os.environ, {"BOARD_ACTOR": "po"}),
            contextlib.redirect_stdout(out),
        ):
            code = main(
                ["sprint", "allow-production", "--ref", "sprint:allow", "--role", "po", "--project", "ummanu",
                 "--reason", "the development server", "--request-id", "cli-allow-1",
                 "--instance", str(self.instance), "--data-dir", self.tmp.name]
            )
        self.assertEqual(code, 0, out.getvalue())
        [event] = self.allowances()
        self.assertEqual(event["actor"], {"role": "po", "id": "po"})
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], ["ummanu"])


if __name__ == "__main__":
    unittest.main()
