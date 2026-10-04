"""A sprint's PO session and allowed productions on PostgreSQL: create, refuse, replay, read back, allow.

The unit suite (`tests/test_sprint_po_channel.py`) holds the rules over fakes; this is the same create
through the real `sprints` columns of revision 0016 and the real `po_sessions` table.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.fakes.sprints import SprintFixture
from ummanu.cli import main
from ummanu.sprint_observer import head_choice
from ummanu.sprints import SprintReader
from ummanu.tasks import TaskError


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
