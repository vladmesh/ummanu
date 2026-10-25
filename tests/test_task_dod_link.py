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

from tests.fakes.tasks import open_sprint
from tests.fakes.tick_board import SPRINT, CountingStore
from tests.observer_identity import as_observer, unbound_observer
from tests.po_handover_fakes import MemoryAudit
from ummanu.board import sprint_dod
from ummanu.board.audit_contract import PROTOCOL_EVENT_RECORD_TYPE
from ummanu.board.backend import record_key
from ummanu.board.tick_snapshot import tick_snapshot
from ummanu.cli import main
from ummanu.sprints import SprintReader, SprintWriter
from ummanu.tasks import TaskError, TaskReader, TaskWriter
from ummanu.web.page_sprint import sprint as sprint_page
from ummanu.webproto.sprint_reads import SprintReadLayer


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

    def test_manual_delivery_replays_audits_and_stays_independent_of_live_verification(self):
        writer = SprintWriter(self.store, data_dir=self.root)
        fields = {"role": "observer", "actor": "observer", "reference": SPRINT,
                  "item": "d2", "evidence": "operator verified main", "request_id": "manual"}
        first = writer.mark_dod_in_main(**fields)
        again = writer.mark_dod_in_main(**fields)
        self.assertEqual(first["event_id"], again["event_id"])
        dod = writer.reader.show(SPRINT, include_delivery=True)["dod"]
        self.assertEqual((dod["in_main"], dod["met"]), (1, 0))
        by = dod["items"][1]["delivery"]["by"]
        self.assertEqual((by["role"], by["actor"]), ("observer", "observer"))
        event = self.store.audit.committed_event("manual")
        self.assertEqual(by["at"], event["occurred_at"])
        self.assertEqual(event["payload"], {"item": "d2", "evidence": "operator verified main"})
        with self.assertRaises(TaskError):
            writer.mark_dod_in_main(**{**fields, "evidence": "another proof"})
        with self.assertRaises(TaskError):
            writer.mark_dod_in_main(**{**fields, "role": "po", "actor": "operator"})
        writer.meet_dod_item(role="observer", actor="observer", reference=SPRINT, item="d2", evidence="live")
        self.assertEqual(writer.reader.show(SPRINT, include_delivery=True)["dod"]["met"], 1)
        writer.reopen_dod_item(role="observer", actor="observer", reference=SPRINT, item="d2", reason="check again")
        self.assertEqual(writer.reader.show(SPRINT, include_delivery=True)["dod"]["in_main"], 1)
        self.create(dod_item="d2")
        self.assertEqual(writer.reader.show(SPRINT, include_delivery=True)["dod"]["in_main"], 0)
        self.assertEqual(writer.mark_dod_in_main(**fields)["event_id"], first["event_id"])
        self.assertEqual(writer.reader.show(SPRINT, include_delivery=True)["dod"]["in_main"], 0)

    def test_metadata_only_read_leaves_delivery_unknown_until_cards_and_audit_are_loaded(self):
        writer = SprintWriter(self.store, data_dir=self.root)
        writer.mark_dod_in_main(role="observer", actor="observer", reference=SPRINT,
                                item="d2", evidence="operator proof")
        metadata = writer.reader.show(SPRINT, include_cards=False)["dod"]
        loaded = writer.reader.show(SPRINT, include_delivery=True)["dod"]
        self.assertIsNone(metadata["in_main"])
        self.assertEqual(metadata["items"][1]["delivery"]["state"], "unknown")
        self.assertIn("not loaded", metadata["items"][1]["delivery"]["reason"])
        self.assertEqual((loaded["in_main"], loaded["met"]), (1, 0))
        self.create(dod_item="d2")
        self.assertIsNone(writer.reader.show(SPRINT, include_cards=False)["dod"]["in_main"])
        self.assertEqual(writer.reader.show(SPRINT, include_delivery=True)["dod"]["in_main"], 0)

    def test_manual_delivery_cli_refuses_cards_blank_evidence_and_foreign_or_terminal_sprint(self):
        writer = SprintWriter(self.store, data_dir=self.root)
        fields = {"role": "observer", "actor": "observer", "reference": SPRINT,
                  "item": "d1", "evidence": "proof", "request_id": "manual"}
        self.bind()
        for changes in ({}, {"evidence": " "}, {"role": "worker"}):
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                writer.mark_dod_in_main(**{**fields, **changes})
        self.store.rows[1]["is_active"] = 0
        with tick_snapshot(self.reader) as snapshot:
            snapshot.load()
            with self.assertRaises(TaskError):
                writer.mark_dod_in_main(**fields)
        self.assertIsNone(self.store.audit.committed_event("manual"))
        with as_observer("sprint:2"), self.assertRaises(TaskError):
            writer.mark_dod_in_main(**{**fields, "item": "d2"})
        output = io.StringIO()
        args = ["sprint", "dod", "in-main", "--ref", SPRINT, "--item", "d2", "--role", "observer",
                "--evidence", "operator proof", "--request-id", "cli-main", "--data-dir", str(self.root),
                "--instance", str(self.root)]
        with mock.patch("ummanu.sprint_commands.board_client", return_value=self.store), contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 0)
        self.assertIsNone(json.loads(output.getvalue())["sprint"]["dod"]["in_main"])
        self.assertEqual(writer.reader.show(SPRINT, include_delivery=True)["dod"]["in_main"], 1)
        self.sprint_meta["sprint_status"] = "closed"
        with self.assertRaises(TaskError) as refused:
            writer.mark_dod_in_main(**{**fields, "item": "d2", "request_id": "closed"})
        self.assertEqual(refused.exception.code, "closed")

    def test_reader_web_and_page_agree_on_archived_merge_delivery_and_source_failures(self):
        self.bind()
        self.store.rows[1]["is_active"] = 0
        self.store.audit.committed["merge"] = {
            "ref": "demo-1", "event_id": "merge", "record_type": PROTOCOL_EVENT_RECORD_TYPE,
            "actor": {"role": "dispatcher"}, "transition": {"source": "assessment", "target": "done"},
            "data": {"release_merge": {"base": "main", "merge_sha": "a" * 40, "path": "pr"}},
        }
        reader = SprintReader(self.store, data_dir=self.root)
        reads = SprintReadLayer(self.root, data_dir=self.root, board_client=self.store)
        shown = reader.show(SPRINT, include_delivery=True)["dod"]
        with tick_snapshot(self.reader) as snapshot:
            snapshot.load()
            self.assertIsNone(reader.show(SPRINT)["dod"]["in_main"])
            self.assertEqual(reader.show(SPRINT, include_delivery=True)["dod"], shown)
            self.assertEqual(reader.statuses()[0]["dod"], shown)
        document = reads.sprint_state(SPRINT)
        self.assertEqual(shown, document["sprint"]["value"]["dod"])
        output = io.StringIO()
        with (mock.patch("ummanu.sprint_commands.board_client", return_value=self.store),
              contextlib.redirect_stdout(output)):
            code = main(["sprint", "show", "--ref", SPRINT, "--instance", str(self.root),
                         "--data-dir", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["dod"], shown)
        self.assertEqual(shown, reader.status(SPRINT)["dod"])
        listing = reads.sprint_list()["sprints"]["items"]
        self.assertEqual(shown, next(row for row in listing if row["ref"] == SPRINT)["dod"])
        page = sprint_page(document)
        self.assertIn("в main 1 / 2", page)
        self.assertIn("проверено 0 / 2", page)
        self.assertIn("ожидает живой проверки", page)
        self.assertIn("release_merge", page)
        self.assertIn("a" * 40, page)
        for owner, method in ((self.store.audit, "events"), (SprintReader, "linked_cards")):
            with self.subTest(method=method), mock.patch.object(owner, method, side_effect=OSError("source lost")):
                failed = reads.sprint_state(SPRINT)
                dod = failed["sprint"]["value"]["dod"]
                self.assertIsNone(dod["in_main"])
                self.assertEqual(dod["items"][0]["delivery"]["state"], "unknown")
                self.assertIn("source lost", dod["items"][0]["delivery"]["reason"])
                self.assertIn("в main unknown / 2", sprint_page(failed))

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

    def test_shared_free_text_sprint_fixture_supports_unlinked_creation(self):
        with open_sprint(SPRINT, "demo"):
            card = self.create(dod_item="")["task"]
        self.assertEqual((card["sprint"], card["dod_item"]), (SPRINT, None))

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
            with (self.subTest(change=change), mock.patch.dict(self.store.meta[1]),
                  mock.patch.dict(self.sprint_meta)):
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
