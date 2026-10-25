"""Card DoD links through the public writer, reader and CLI over an in-process transport."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes.tick_board import SPRINT, CountingStore
from tests.observer_identity import as_observer, unbound_observer
from tests.po_handover_fakes import MemoryAudit
from ummanu.board import sprint_dod
from ummanu.board.backend import record_key
from ummanu.cli import main
from ummanu.tasks import TaskError, TaskReader, TaskWriter


class TaskDodLinkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "projects").mkdir()
        (self.root / "projects/demo.yaml").write_text("id: demo\n", encoding="utf-8")
        self.store = CountingStore(self.root)
        self.store.meta[1]["project"] = "demo"
        self.store.audit = MemoryAudit()
        self.enterContext(mock.patch("ummanu.board.sql_audit.SqlTaskAudit", return_value=self.store.audit))
        self.sprint_meta = self.store.meta[record_key("sprint", SPRINT)]
        self.sprint_meta["sprint_reservations"] = '["demo"]'
        self.items = sprint_dod.new_items(["the change ships", "the owner accepts it", "dropped"])
        self.items = sprint_dod.descoped(self.items, "d3", "outside the scope",
                                       {"role": "po", "actor": "operator", "at": "2026-01-01T00:00:00Z"})
        self.sprint_meta[sprint_dod.FIELD] = sprint_dod.encode(self.items)
        self.writer = TaskWriter(self.store, data_dir=self.root)
        self.reader = TaskReader(self.store)
        self.enterContext(as_observer(SPRINT))

    def create(self, **fields):
        return self.writer.create(**{
            "role": "observer", "actor": "observer", "project": "demo", "task_type": "code",
            "title": "Deliver the change", "sprint": SPRINT, "dod_item": "d1", **fields,
        })

    def bind(self, **fields):
        return self.writer.bind_dod(**{
            "role": "observer", "actor": "observer", "reference": "demo-1",
            "sprint": SPRINT, "dod_item": "d1", **fields,
        })

    def cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch("ummanu.task_commands.card_client", return_value=self.store),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            location = ["--instance", str(self.root)]
            if argv[0] not in {"show", "list"}:
                location.extend(["--data-dir", str(self.root)])
            status = main(["task", *argv, *location])
        return status, output.getvalue(), errors.getvalue()

    def snapshot(self):
        return copy.deepcopy((self.store.rows, self.store.meta, self.store.audit.pending,
                              self.store.audit.committed))

    def test_create_and_read_every_card_type_with_one_stored_item(self):
        for kind in ("code", "research", "infra", "decision", "operation", "wait"):
            fields = {"touches_production": "none"} if kind == "operation" else {}
            if kind == "wait":
                fields["wait"] = {"until": "2099-01-01T00:00:00Z", "deadline": "2100-01-01T00:00:00Z",
                                  "returns": ["dependents"]}
            with self.subTest(kind=kind):
                card = self.create(task_type=kind, **fields)["task"]
                self.assertEqual((card["sprint"], card["dod_item"]), (SPRINT, "d1"))
                listed = self.reader.list(sprint=SPRINT)
                self.assertEqual(next(row for row in listed if row["ref"] == card["ref"])["dod_item"], "d1")
                status, body, _ = self.cli("show", "--ref", card["ref"])
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(body)["dod_item"], "d1")

    def test_invalid_creates_leave_no_card_metadata_or_request(self):
        cases = ({"dod_item": ""}, {"dod_item": "d9"}, {"dod_item": "d3"},
                 {"dod_item": "d1,d2"}, {"dod_item": ["d1", "d2"]},
                 {"dod_item": "d1", "sprint": ""})
        for fields in cases:
            with self.subTest(fields=fields):
                before = self.snapshot()
                with self.assertRaises(TaskError) as caught:
                    self.create(**fields)
                self.assertEqual(caught.exception.code, "validation")
                self.assertEqual(self.snapshot(), before)

    def test_unstructured_and_unsprinted_creates_keep_the_supported_unlinked_form(self):
        self.sprint_meta.pop(sprint_dod.FIELD)
        before = self.snapshot()
        with self.assertRaises(TaskError):
            self.create()
        self.assertEqual(self.snapshot(), before)
        card = self.create(dod_item="")["task"]
        self.assertIsNone(card["dod_item"])
        unsprinted = self.create(role="po", actor="operator", sprint="", dod_item="")["task"]
        self.assertEqual((unsprinted["sprint"], unsprinted["dod_item"]), (None, None))

    def test_create_replays_and_item_is_part_of_request_identity(self):
        first = self.create(request_id="create")
        again = self.create(request_id="create")
        self.assertTrue(again["replayed"])
        self.assertEqual(first["event_id"], again["event_id"])
        before = self.snapshot()
        with self.assertRaises(TaskError):
            self.create(request_id="create", dod_item="d2")
        self.assertEqual(self.snapshot(), before)

    def test_pending_create_repair_restores_the_link_with_the_other_metadata(self):
        with (mock.patch.object(self.store, "_rpc_saveTaskMetadata", side_effect=OSError("write lost")),
              self.assertRaises(TaskError)):
            self.create(request_id="repair-create")
        pending = self.store.audit.pending_event("repair-create")
        self.assertIsNotNone(pending)
        repaired = self.create(request_id="repair-create")
        self.assertTrue(repaired["replayed"])
        self.assertEqual(repaired["task"]["dod_item"], "d1")

    def test_pending_binding_confirms_the_saved_link_before_publishing_its_event(self):
        with (mock.patch.object(self.store.audit, "append", side_effect=OSError("audit lost")),
              self.assertRaises(TaskError)):
            self.bind(request_id="repair-bind")
        self.assertEqual(self.reader.show("demo-1")["dod_item"], "d1")
        self.assertTrue(self.bind(request_id="repair-bind")["replayed"])

    def test_binding_rejects_an_unconfirmed_metadata_write(self):
        before = self.snapshot()
        with (mock.patch.object(self.store, "_rpc_saveTaskMetadata", return_value=False),
              self.assertRaises(TaskError) as caught):
            self.bind(request_id="unsaved")
        self.assertEqual(caught.exception.code, "backend_error")
        self.assertEqual(self.snapshot(), before)

    def test_edit_keeps_a_saved_link(self):
        created = self.create()["task"]
        self.writer.edit(role="observer", actor="observer", reference=created["ref"], head="chosen-head")
        self.assertEqual(self.reader.show(created["ref"])["dod_item"], "d1")

    def test_old_cards_read_as_unlinked_without_rewriting_them(self):
        self.store.rows[1]["description"] = "This prose mentions d2 and d1."
        self.sprint_meta["sprint_current_task"] = "demo-1"
        before = self.snapshot()
        card = self.reader.show("demo-1")
        self.assertIsNone(card["dod_item"])
        self.assertIsNone(next(row for row in self.reader.list(sprint=SPRINT) if row["ref"] == "demo-1")["dod_item"])
        self.assertEqual(self.snapshot(), before)

    def test_cli_help_describes_fresh_links_and_legacy_binding(self):
        for command, wording in (("create", "fresh card of a structured"), ("bind-dod", "reassignment is refused")):
            status, body, _ = self.cli(command, "--help")
            self.assertEqual(status, 0)
            self.assertIn("--dod-item", body)
            self.assertIn(wording, " ".join(body.split()))

    def test_binding_replays_and_same_link_with_new_request_does_not_rewrite_metadata(self):
        first = self.bind(request_id="bind")
        writes = list(self.store.writes)
        again = self.bind(request_id="bind")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["event_id"], first["event_id"])
        self.bind(request_id="same-link")
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(self.reader.show("demo-1")["dod_item"], "d1")
        event = self.store.audit.committed_event("bind")
        self.assertEqual((event["ref"], event["payload"]["sprint"], event["payload"]["dod_item"]),
                         ("demo-1", SPRINT, "d1"))
        before = self.snapshot()
        for request in ("bind", "different-link"):
            with self.assertRaises(TaskError):
                self.bind(request_id=request, dod_item="d2")
        self.assertEqual(self.snapshot(), before)

    def test_binding_rejects_invalid_scope_without_mutating_the_card(self):
        for fields in ({"sprint": "sprint:2"}, {"dod_item": "d9"}, {"dod_item": "d3"},
                       {"dod_item": "d1,d2"}, {"dod_item": ["d1", "d2"]}, {"dod_item": ""}):
            with self.subTest(fields=fields), self.assertRaises(TaskError):
                self.bind(**fields)
        self.assertIsNone(self.reader.show("demo-1")["dod_item"])
        self.assertEqual(self.store.writes, [])
        self.assertFalse(any(event["kind"] == "dod_item_bound" for event in self.store.audit.events()))

    def test_binding_checks_stored_membership_and_open_structured_sprint(self):
        for change in ("foreign", "unsprinted", "stopped", "closed", "free-text"):
            with self.subTest(change=change):
                with mock.patch.dict(self.store.meta[1]), mock.patch.dict(self.sprint_meta):
                    if change in {"foreign", "unsprinted"}:
                        self.store.meta[1]["sprint_ref"] = "sprint:2" if change == "foreign" else ""
                    elif change == "free-text":
                        self.sprint_meta.pop(sprint_dod.FIELD)
                    else:
                        self.sprint_meta["sprint_status"] = change
                    before = self.snapshot()
                    with self.assertRaises(TaskError):
                        self.bind()
                    self.assertEqual(self.snapshot(), before)

    def test_binding_checks_role_observer_identity_and_po_override(self):
        with self.assertRaises(TaskError) as caught:
            self.bind(role="worker", actor="worker")
        self.assertEqual(caught.exception.code, "role_forbidden")
        with unbound_observer(), self.assertRaises(TaskError) as caught:
            self.bind()
        self.assertEqual(caught.exception.code, "observer_identity_unbound")
        with as_observer("sprint:2"), self.assertRaises(TaskError) as caught:
            self.bind()
        self.assertEqual(caught.exception.code, "observer_sprint_mismatch")
        with self.assertRaises(TaskError) as caught:
            self.bind(role="po", actor="operator")
        self.assertEqual(caught.exception.code, "sprint_write_forbidden")
        self.assertIsNone(self.reader.show("demo-1")["dod_item"])
        self.bind(role="po", actor="operator", sprint_override=True, sprint_override_reason="owner assignment")
        self.assertEqual(self.reader.show("demo-1")["dod_item"], "d1")

    def test_done_archived_bootstrap_card_can_be_bound_and_replay_survives_sprint_close(self):
        self.store.rows[5]["project_id"] = self.store.rows[1]["project_id"]
        self.store.meta[5]["project"] = "demo"
        bound = self.bind(reference="demo-5", request_id="bootstrap")
        self.assertTrue(bound["task"]["closed"])
        self.assertEqual(bound["task"]["dod_item"], "d1")
        self.sprint_meta["sprint_status"] = "closed"
        self.assertTrue(self.bind(reference="demo-5", request_id="bootstrap")["replayed"])
        with self.assertRaises(TaskError):
            self.bind(reference="demo-5", request_id="fresh")

    def test_cli_creates_binds_and_refuses_multiple_item_flags(self):
        with mock.patch("ummanu.task_commands._refuse_unrunnable_project"):
            status, body, _ = self.cli("create", "--role", "observer", "--project", "demo", "--type", "code",
                                       "--title", "Change", "--sprint", SPRINT, "--dod-item", "d2")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(body)["task"]["dod_item"], "d2")
        argv = ("bind-dod", "--ref", "demo-1", "--role", "observer", "--sprint", SPRINT,
                "--dod-item", "d1", "--request-id", "cli-bind")
        self.assertEqual(self.cli(*argv)[0], 0)
        self.assertTrue(json.loads(self.cli(*argv)[1])["replayed"])
        for prefix in (("create", "--role", "observer", "--project", "demo", "--type", "code", "--title", "X"),
                       ("bind-dod", "--ref", "demo-2", "--role", "observer")):
            before = self.snapshot()
            status, _, errors = self.cli(*prefix, "--sprint", SPRINT, "--dod-item", "d1", "--dod-item", "d2")
            self.assertEqual(status, 2)
            self.assertIn("exactly one", errors)
            self.assertEqual(self.snapshot(), before)
