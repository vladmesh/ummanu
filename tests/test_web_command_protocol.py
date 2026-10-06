"""The command half of the transport-independent layer: the two reads and the written contract.

The acceptance criteria of secretary-1579, as tests rather than as prose. Four of them are about
what an answer must never let a caller believe -- that an audit nobody could read is an empty
history, that a request id nobody could look up was never sent, that a page which ended is the same
as one the limit cut short, and that a read may repair what it reports on -- so they are checked on
the documents themselves and not only on the happy path.

Nothing here touches a live installation: every command, staged record and fault runs against the
fixture's own data plane.
"""

from __future__ import annotations

import json
import re
import unittest
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from tests.webproto_sprint_fixtures import SprintProtocolFixture
from ummanu.board.events import BoardEventCanon
from ummanu.board.models import Actor, EntityKind, Event, EventKind
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.config import validate
from ummanu.tasks import task_audit_for
from ummanu.webproto import command_reads
from ummanu.webproto.boundary import operations
from ummanu.webproto.command_reads import (
    COMMAND_ERRORS,
    HISTORY_SCOPE,
    OPERATION_IDENTITY,
    STATE_COMMITTED,
    STATE_NOT_FOUND,
    STATE_PENDING,
    STATE_UNKNOWN,
    CommandReadLayer,
)
from ummanu.webproto.commands import (
    EXIT_CONFLICT,
    EXIT_PENDING,
    run_web_read_commands,
    run_web_read_request,
)
from ummanu.webproto.cursor import Cursor
from ummanu.webproto.errors import InvalidCursor, OperationPending, ReadError, ValidationRefused
from ummanu.webproto.journal import MAX_LIMIT
from ummanu.webproto.ops import OperationLayer
from ummanu.webproto.pause_ops import PauseOperationLayer
from ummanu.webproto.section import Section, SectionSet, sections
from ummanu.webproto.sprint_ops import SprintOperationLayer

DOCS = Path(__file__).resolve().parents[1] / "docs"

#: The operation layers the identity contract is derived from. Every mutation of this package lives
#: on one of them, so "which operations take a request id" is a question about these three classes
#: and never a list somebody keeps by hand.
OPERATION_LAYERS = (OperationLayer, SprintOperationLayer, PauseOperationLayer)


class CommandProtocolFixture(SprintProtocolFixture):
    """One installation, one data plane, and a committed audit with commands on several entities.

    The history is the card audit (`requests`), so the board is a real store.
    """

    def layer(self, **kwargs: Any) -> CommandReadLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            # The audit is the card client's: `requests` in the fixture's own store.
            "board_client": self.board,
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return CommandReadLayer(self.instance, **options)

    # -- the history a case wants ---------------------------------------------------------------

    def audit(self) -> SqlTaskAudit:
        return task_audit_for(self.board)

    def canon(self) -> BoardEventCanon:
        return BoardEventCanon(self.audit())

    def journal(self) -> Path:
        return self.data_dir / "board" / "events.ndjson"

    def event(
        self,
        *,
        event_id: str,
        kind: EventKind = EventKind.CARD_STARTED,
        entity: EntityKind = EntityKind.CARD,
        ref: str = "ummanu-12",
        role: str = "dispatcher",
        actor: str = "ummanu-production",
        reason: str = "claimed for the worker",
        minute: int = 0,
        transition: tuple[str, str] | None = ("ready", "in_progress"),
    ) -> Event:
        return Event(
            event_id=event_id,
            kind=kind,
            entity_kind=entity,
            ref=ref,
            actor=Actor(role, actor),
            reason=reason,
            occurred_at=datetime(2026, 9, 7, 12, minute, tzinfo=UTC),
            source_state=None if transition is None else transition[0],
            target_state=None if transition is None else transition[1],
        )

    def commit(self, request_id: str, **kwargs: Any) -> Event:
        """One committed typed protocol event, written the way its writer writes one."""
        event = self.event(**kwargs)
        self.canon().commit(request_id, event)
        return event

    def stage(self, request_id: str, **kwargs: Any) -> Event:
        """One staged event whose backend effect never confirmed: a request that is part-done.

        Staged through the canon rather than by writing a file, because staging *is* what a writer
        does before its backend effect: this is the state a create leaves behind when the process
        dies between the stage and the commit, not a simulation of it.
        """
        event = self.event(**kwargs)
        self.canon().stage(request_id, event)
        return event

    def generic(
        self,
        request_id: str,
        *,
        ref: str = "ummanu-12",
        kind: str = "commented",
        event_id: str = "evt_generic",
    ) -> None:
        """One released generic audit record beside the typed ones, as this journal really holds."""
        self.audit().append(
            request_id,
            {
                "schema_version": 1,
                "event_id": event_id,
                "kind": kind,
                "ref": ref,
                "occurred_at": "2026-09-07T12:30:00Z",
                "actor": {"role": "po", "id": "operator"},
                "outcome": "success",
                "payload": {"marker": "dispatcher"},
            },
        )

    def history(self, **kwargs: Any) -> dict[str, Any]:
        return self.layer().command_history(**kwargs)

    def refs(self, document: dict[str, Any]) -> list[str]:
        return [item["entity"]["ref"] for item in document["commands"]["items"]]

    def ids(self, document: dict[str, Any]) -> list[str]:
        return [item["event_id"] for item in document["commands"]["items"]]

    # -- faults -------------------------------------------------------------------------------

    def data_plane(self) -> dict[str, bytes]:
        """Every file of this installation's data plane, so a write anywhere in it is visible."""
        return {
            str(path.relative_to(self.data_dir)): path.read_bytes()
            for path in sorted(self.data_dir.rglob("*"))
            if path.is_file()
        }

    # -- reading a document --------------------------------------------------------------------

    @staticmethod
    def source_of(document: dict[str, Any], *path: str) -> dict[str, Any]:
        node: Any = document
        for step in path:
            node = node[step]
        return dict(node["source"])

    def assert_available(self, document: dict[str, Any], *path: str) -> None:
        self.assertEqual(self.source_of(document, *path)["state"], "available")

    def assert_unavailable(self, document: dict[str, Any], *path: str) -> dict[str, Any]:
        source = self.source_of(document, *path)
        self.assertEqual(source["state"], "unavailable")
        return source


class HistoryReadTests(CommandProtocolFixture):
    """Criterion 1: a page of recent commands across entities, with this layer's paging."""

    def test_the_history_crosses_entities_and_is_not_one_card_slice(self) -> None:
        self.commit("req-card", event_id="evt_1", ref="ummanu-12", minute=1)
        self.commit(
            "req-sprint",
            event_id="evt_2",
            kind=EventKind.SPRINT_CLOSED,
            entity=EntityKind.SPRINT,
            ref="sprint:1431",
            reason="the sprint reached its definition of done",
            transition=None,
            minute=2,
        )
        self.generic("req-generic", ref="ummanu-99")
        document = self.history()
        self.assertEqual(sorted(self.refs(document)), ["sprint:1431", "ummanu-12", "ummanu-99"])

    def test_every_row_carries_the_initiator_the_action_the_entity_and_the_result(self) -> None:
        self.commit("req-card", event_id="evt_1", reason="claimed for the worker")
        row = self.history()["commands"]["items"][0]
        self.assertEqual(row["actor"], {"id": "ummanu-production", "role": "dispatcher"})
        self.assertEqual(row["action"], EventKind.CARD_STARTED.value)
        self.assertEqual(row["entity"], {"ref": "ummanu-12", "kind": "card"})
        self.assertEqual(row["result"], {"outcome": None, "reason": "claimed for the worker"})
        self.assertEqual(row["request_id"], "req-card")

    def test_a_generic_record_keeps_its_own_outcome_and_never_borrows_a_reason(self) -> None:
        """Both shapes are on this journal, and neither field is renamed into the other's."""
        self.generic("req-generic")
        row = self.history()["commands"]["items"][0]
        self.assertFalse(row["typed"])
        self.assertEqual(row["result"], {"outcome": "success", "reason": None})

    def test_the_page_is_newest_first_in_the_order_the_journal_was_appended_in(self) -> None:
        """Appended order reversed, and deliberately not a sort by `occurred_at`.

        The two are made to disagree here: the second command appended is stamped a minute *before*
        the first. The writer stamps `occurred_at` and two commands can share a second, so the only
        order that is a fact about what happened is the order the journal grew in.
        """
        self.commit("req-1", event_id="evt_1", minute=30)
        self.commit("req-2", event_id="evt_2", minute=10)
        document = self.history()
        self.assertEqual(self.ids(document), ["evt_2", "evt_1"])
        self.assertEqual(document["extent"]["order"], "journal_append_reversed")

    def test_a_page_continues_into_older_commands_and_never_repeats_one(self) -> None:
        for index in range(5):
            self.commit(f"req-{index}", event_id=f"evt_{index}", minute=index)
        first = self.history(limit=2)
        self.assertEqual(self.ids(first), ["evt_4", "evt_3"])
        self.assertTrue(first["commands"]["has_more"])
        second = self.history(cursor=first["commands"]["next_cursor"], limit=2)
        self.assertEqual(self.ids(second), ["evt_2", "evt_1"])
        third = self.history(cursor=second["commands"]["next_cursor"], limit=2)
        self.assertEqual(self.ids(third), ["evt_0"])
        # Criterion 4: the page that reached the beginning says so, and the ones before it did not.
        self.assertFalse(third["commands"]["has_more"])

    def test_reading_the_same_cursor_twice_returns_the_same_page(self) -> None:
        for index in range(4):
            self.commit(f"req-{index}", event_id=f"evt_{index}", minute=index)
        first = self.history(limit=2)
        again = self.history(cursor=first["commands"]["next_cursor"], limit=2)
        self.commit("req-new", event_id="evt_new", minute=9)
        self.assertEqual(
            self.ids(again), self.ids(self.history(cursor=first["commands"]["next_cursor"], limit=2))
        )

    def test_a_cursor_belonging_to_a_card_is_refused_rather_than_answered(self) -> None:
        """The ref binding is what keeps the two positions in this journal apart."""
        self.commit("req-1", event_id="evt_1")
        with self.assertRaises(InvalidCursor):
            self.history(cursor=Cursor(ref="ummanu-12", offset=0).encode())

    def test_a_cursor_past_the_end_of_a_journal_that_only_grows_is_refused(self) -> None:
        self.commit("req-1", event_id="evt_1")
        with self.assertRaises(InvalidCursor) as refused:
            self.history(cursor=Cursor(ref=HISTORY_SCOPE, offset=99).encode())
        self.assertEqual(refused.exception.code, "validation")

    def test_a_page_is_a_page_and_a_caller_cannot_raise_the_ceiling(self) -> None:
        self.commit("req-1", event_id="evt_1")
        self.assertEqual(self.history(limit=10_000)["limit"], MAX_LIMIT)
        self.assertEqual(self.history(limit=0)["limit"], 1)


class RequestReadTests(CommandProtocolFixture):
    """Criterion 2: what became of one request id, from the audit's own lookups."""

    def test_a_request_this_installation_never_saw_is_not_found(self) -> None:
        self.commit("req-1", event_id="evt_1")
        operation = self.layer().command_request("never-sent")["operation"]
        self.assertEqual(operation["state"], STATE_NOT_FOUND)
        self.assertIsNone(operation["entity"])
        self.assertIsNone(operation["continuation"])

    def test_a_committed_request_answers_with_its_result_and_the_entity_it_produced(self) -> None:
        self.commit("req-1", event_id="evt_1", reason="claimed for the worker")
        operation = self.layer().command_request("req-1")["operation"]
        self.assertEqual(operation["state"], STATE_COMMITTED)
        self.assertEqual(operation["entity"], {"ref": "ummanu-12", "kind": "card"})
        self.assertEqual(operation["result"]["reason"], "claimed for the worker")
        self.assertEqual(operation["event_id"], "evt_1")
        self.assertFalse(operation["staged"])
        self.assertIsNone(operation["continuation"])

    def test_a_staged_request_says_what_is_done_and_how_to_continue_safely(self) -> None:
        """Criterion 5's second fault: the staged event exists and its committed one does not."""
        self.commit("req-committed", event_id="evt_committed")
        self.stage("req-staged", event_id="evt_staged", reason="staged before the backend effect")
        operation = self.layer().command_request("req-staged")["operation"]
        self.assertEqual(operation["state"], STATE_PENDING)
        self.assertTrue(operation["staged"])
        self.assertEqual(operation["entity"], {"ref": "ummanu-12", "kind": "card"})
        continuation = operation["continuation"]
        self.assertTrue(continuation["repeat_request"])
        self.assertEqual(continuation["request_id"], "req-staged")
        # A staged record is an intention whose backend write may still fail, so its reason is not
        # published as a result: that would report what was meant as what happened.
        self.assertIsNone(operation["result"])

    def test_the_read_repairs_nothing_it_finds_staged(self) -> None:
        """A read that repairs is not a read: the pending record is exactly where it was."""
        self.commit("req-committed", event_id="evt_committed")
        self.stage("req-staged", event_id="evt_staged", minute=1)
        before = self.data_plane()
        self.layer().command_request("req-staged")
        self.assertEqual(self.data_plane(), before)
        self.assertIsNotNone(self.audit().pending_event("req-staged"))
        self.assertIsNone(self.audit().committed_event("req-staged"))

    def test_the_read_needs_the_identifier_the_operation_was_sent_with(self) -> None:
        with self.assertRaises(ValidationRefused):
            self.layer().command_request("")

    def test_the_answer_carries_the_identity_contract_it_has_to_be_read_with(self) -> None:
        self.commit("req-committed", event_id="evt_committed")
        self.stage("req-staged", event_id="evt_staged", minute=1)
        identity = self.layer().command_request("req-staged")["identity"]
        self.assertIn("sprint_create", identity["with_request_id"])
        self.assertIn("pause_drain", identity["without_request_id"])

    def test_no_writer_is_reachable_from_either_read(self) -> None:
        """Criterion 2, structurally: the reads perform no operation, so none is even called."""
        with (
            mock.patch("ummanu.sprints.SprintWriter.create") as created,
            mock.patch("ummanu.board.sql_audit.SqlTaskAudit.append") as appended,
            mock.patch("ummanu.board.sql_audit.SqlTaskAudit.claim") as claimed,
            mock.patch("ummanu.board.sql_audit.SqlTaskAudit.reconcile") as reconciled,
            mock.patch("ummanu.board.sql_audit.SqlTaskAudit.discard") as discarded,
        ):
            self.layer().command_history()
            self.layer().command_request("req-1")
        for never in (created, appended, claimed, reconciled, discarded):
            never.assert_not_called()


class ReadsWriteNothingTests(CommandProtocolFixture):
    """Criterion 3: both reads write nothing and start nothing, over the whole data plane."""

    def test_the_history_changes_no_byte_of_the_data_plane(self) -> None:
        self.create()
        self.commit("req-committed", event_id="evt_1")
        before = self.data_plane()
        self.layer().command_history()
        self.assertEqual(self.data_plane(), before)

    def test_the_request_read_changes_no_byte_of_the_data_plane(self) -> None:
        self.create()
        self.stage("req-staged", event_id="evt_staged")
        before = self.data_plane()
        for request in ("req-1", "req-staged", "never-sent"):
            self.layer().command_request(request)
        self.assertEqual(self.data_plane(), before)

    def test_neither_read_creates_a_journal_that_was_not_there(self) -> None:
        """The one write a reader of a missing file is most likely to make by accident."""
        self.assertFalse(self.journal().exists())
        self.layer().command_history()
        self.layer().command_request("req-1")
        self.assertFalse(self.journal().exists())


class HonestyTests(CommandProtocolFixture):
    """Criterion 4: what the reads refuse to let a caller believe."""

    def test_an_unreadable_audit_is_an_unavailable_source_and_not_an_empty_history(self) -> None:
        self.commit("req-1", event_id="evt_1")
        with mock.patch.object(SqlTaskAudit, "events_page", side_effect=PermissionError("journal denied")):
            document = self.layer().command_history()
        source = self.assert_unavailable(document, "commands")
        self.assertIn("PermissionError", source["reason"])
        self.assertIsNone(document["commands"]["items"])
        self.assertIsNone(document["commands"]["has_more"])
        # The extent is not read from a source, so it is said even here.
        self.assertEqual(document["extent"]["entities"], "all")

    def test_an_empty_history_and_an_unavailable_one_are_different_answers(self) -> None:
        # An empty `requests` table is a read that happened.
        document = self.layer().command_history()
        self.assert_available(document, "commands")
        self.assertEqual(document["commands"]["items"], [])

    def test_a_request_id_over_an_unreadable_audit_is_unknown_and_never_not_found(self) -> None:
        with mock.patch.object(SqlTaskAudit, "committed_event", side_effect=PermissionError("journal denied")):
            document = self.layer().command_request("req-1")
        self.assert_unavailable(document, "operation")
        self.assertEqual(document["operation"]["state"], STATE_UNKNOWN)
        self.assertIsNone(document["operation"]["staged"])

    def test_a_staged_record_beside_an_unreadable_journal_is_unknown_and_not_pending(self) -> None:
        """`unknown` covers the audit as a whole, not one lookup of it.

        The staged record is right there, but a committed record for the same request cannot be
        ruled out while the journal cannot be read -- and answering `pending` would be the claim
        that it was ruled out.
        """
        self.commit("req-committed", event_id="evt_committed")
        self.stage("req-staged", event_id="evt_staged", minute=1)
        with mock.patch.object(SqlTaskAudit, "committed_event", side_effect=PermissionError("denied")):
            document = self.layer().command_request("req-staged")
        self.assert_unavailable(document, "operation")
        self.assertEqual(document["operation"]["state"], STATE_UNKNOWN)
        self.assertIsNone(document["operation"]["continuation"])

    def test_an_entity_kind_the_record_does_not_carry_is_null_and_never_inferred(self) -> None:
        self.generic("req-generic", ref="ummanu-99")
        row = self.history()["commands"]["items"][0]
        self.assertEqual(row["entity"], {"ref": "ummanu-99", "kind": None})

    def test_a_config_that_does_not_validate_takes_only_what_it_owns(self) -> None:
        """With an explicit data directory the audit still answers; without one nothing can be located."""
        broken = self.tmp / "not-an-installation"
        broken.mkdir(exist_ok=True)
        (broken / "instance.yaml").write_text("version: 1\nname: broken\n", encoding="utf-8")
        self.commit("req-1", event_id="evt_1")
        document = CommandReadLayer(
            broken, data_dir=self.data_dir, board_client=self.board, clock=lambda: self.clock
        ).command_history()
        self.assert_unavailable(document, "sources", "installation")
        self.assert_available(document, "commands")
        with self.assertRaises(ValidationRefused):
            CommandReadLayer(broken, board_client=self.board).command_history()


class SectionSeamTests(CommandProtocolFixture):
    """Criterion 5: every section goes through the seam and names the source that answered it."""

    def test_every_section_names_the_source_that_answered_it(self) -> None:
        self.commit("req-1", event_id="evt_1")
        documents = (self.layer().command_history(), self.layer().command_request("req-1"))
        for document in documents:
            with self.subTest(kind=document["kind"]):
                name = "commands" if document["kind"] == "command_history" else "operation"
                self.assertEqual(self.source_of(document, name)["name"], "audit")
                for key, mark in document["sources"].items():
                    self.assertEqual(mark["source"]["name"], key)

    def test_no_section_of_either_document_is_assembled_outside_the_seam(self) -> None:
        self.assertTrue(issubclass(command_reads.CommandSections, SectionSet))
        for name in sections(command_reads.CommandSections):
            with self.subTest(section=name):
                self.assertTrue(getattr(getattr(command_reads.SECTIONS, name), "__webproto_section__", False))
        produced = command_reads.SECTIONS.operation(
            command_reads.SourceSet(
                [
                    command_reads.Reading(
                        command_reads.SOURCE_AUDIT,
                        command_reads.sources.available(self.clock),
                        command_reads._Lookup("req-1"),
                    )
                ]
            )
        )
        self.assertIsInstance(produced, Section)
        self.assertTrue(produced.trusted)

    def test_a_defect_outside_a_source_read_still_travels_as_itself(self) -> None:
        self.commit("req-1", event_id="evt_1")
        with (
            mock.patch.object(command_reads, "_page", side_effect=ValueError("a defect of this layer")),
            self.assertRaises(ValueError),
        ):
            self.layer().command_history()


class IdentityContractTests(CommandProtocolFixture):
    """Criterion 7: the operation-identity contract, derived from the code and held over the prose."""

    @staticmethod
    def _named(cls: type) -> dict[str, bool]:
        """Every public operation of one layer, and whether its signature names a `request_id`."""
        import inspect

        return {
            name: "request_id" in inspect.signature(getattr(cls, name)).parameters for name in operations(cls)
        }

    def test_the_operations_that_take_a_request_id_are_derived_and_not_restated(self) -> None:
        carried = {name for layer in OPERATION_LAYERS for name, takes in self._named(layer).items() if takes}
        self.assertEqual(set(OPERATION_IDENTITY["with_request_id"]), carried)
        for name, entry in OPERATION_IDENTITY["with_request_id"].items():
            with self.subTest(operation=name):
                layer = next(one for one in OPERATION_LAYERS if one.__name__ == entry["layer"])
                self.assertTrue(self._named(layer)[name])
                self.assertTrue(entry["repeat"].strip())

    def test_the_operations_that_deliberately_take_none_are_derived_too(self) -> None:
        """The pause is the whole of that set, and its idempotence is its own rule."""
        without = {name for name, takes in self._named(PauseOperationLayer).items() if not takes}
        self.assertEqual(set(OPERATION_IDENTITY["without_request_id"]), without)
        for name, entry in OPERATION_IDENTITY["without_request_id"].items():
            with self.subTest(operation=name):
                self.assertEqual(entry["layer"], "PauseOperationLayer")
                self.assertTrue(entry["reason"].strip())

    def test_the_promises_the_part_done_failures_make_are_the_ones_they_keep(self) -> None:
        contract = OPERATION_IDENTITY["errors"]
        self.assertEqual(contract["OperationPending"]["code"], OperationPending.code)
        self.assertEqual(contract["audit_pending"]["exit_status"], EXIT_PENDING)
        self.assertEqual(contract["close_conflict"]["exit_status"], EXIT_CONFLICT)
        # The action a pending refusal really carries, taken off the operation rather than retyped.
        action = SprintOperationLayer(self.instance, data_dir=self.data_dir)._pending_action("req-1")
        self.assertEqual(sorted(action["action"]), sorted(contract["OperationPending"]["action"]))
        self.assertTrue(action["action"]["repeat_request"])

    def test_a_real_pending_refusal_and_the_read_name_the_same_request_id(self) -> None:
        """The two halves of the same promise: what the failure says, and what the read says later.

        This is the scenario the read exists for. An operation refuses as part-done and names the id
        to repeat; the caller, or somebody else entirely, then asks this read what became of that id
        and is told the same thing without re-sending anything.
        """
        from ummanu.webproto import store_io

        real = store_io.write_text_atomic

        def refuse_the_reference(path, payload):
            if '"reference": "sprint:' in payload:
                raise RuntimeError(f"could not write export file {path}: [Errno 28] No space left")
            return real(path, payload)

        with (
            mock.patch.object(store_io, "write_text_atomic", refuse_the_reference),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        request_id = pending.exception.data["action"]["request_id"]
        operation = self.layer().command_request(request_id)["operation"]
        # The create's own audit event committed before the reference write failed, so the read
        # reports the operation as committed and the sprint row it produced.
        self.assertIn(operation["state"], {STATE_COMMITTED, STATE_PENDING})
        self.assertEqual(operation["entity"]["ref"], pending.exception.data["action"]["reference"])

    def test_the_published_protocols_table_records_exactly_this_contract(self) -> None:
        """`docs/PROTOCOLS.md` is held to the contract, not trusted to have kept up with it."""
        protocols = (DOCS / "PROTOCOLS.md").read_text(encoding="utf-8")
        documented: dict[str, set[str]] = {"with_request_id": set(), "without_request_id": set()}
        for line in protocols.splitlines():
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) != 5:
                continue
            operation = re.fullmatch(r"`([a-z_]+)`", cells[1])
            if operation is None:
                continue
            if cells[2] == "`request_id`":
                documented["with_request_id"].add(operation.group(1))
            elif cells[2] == "none":
                documented["without_request_id"].add(operation.group(1))
        self.assertEqual(documented["with_request_id"], set(OPERATION_IDENTITY["with_request_id"]))
        self.assertEqual(documented["without_request_id"], set(OPERATION_IDENTITY["without_request_id"]))

    def test_the_published_error_table_records_exactly_the_codes_the_reads_refuse_with(self) -> None:
        broken = self.tmp / "not-an-installation"
        broken.mkdir(exist_ok=True)
        (broken / "instance.yaml").write_text("version: 1\nname: broken\n", encoding="utf-8")
        raised: dict[str, set[str]] = {name: set() for name in COMMAND_ERRORS}
        for name, call in (
            ("command_history", lambda: CommandReadLayer(broken).command_history()),
            ("command_request", lambda: CommandReadLayer(broken).command_request("req-1")),
        ):
            with self.assertRaises(ReadError) as refused:
                call()
            raised[name].add(refused.exception.code)
        raised["command_history"].add(
            self._refused(lambda: self.history(cursor=Cursor(ref="ummanu-12", offset=0).encode()))
        )
        raised["command_request"].add(self._refused(lambda: self.layer().command_request("")))
        for name, documented in COMMAND_ERRORS.items():
            with self.subTest(operation=name):
                self.assertEqual(raised[name], set(documented))

    def test_the_published_error_table_is_the_same_value(self) -> None:
        """The doc restates the codes, so the doc is held to the value rather than trusted."""
        protocols = (DOCS / "PROTOCOLS.md").read_text(encoding="utf-8")
        documented: dict[str, tuple[str, ...]] = {}
        for line in protocols.splitlines():
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) != 5:
                continue
            read = re.fullmatch(r"`(command_[a-z]+)`", cells[1])
            if read is None or read.group(1) not in COMMAND_ERRORS:
                continue
            documented[read.group(1)] = tuple(re.findall(r"`([a-z_]+)`", cells[2]))
        self.assertEqual(documented, COMMAND_ERRORS)

    def _refused(self, call) -> str:
        with self.assertRaises(ReadError) as refused:
            call()
        return refused.exception.code

    def test_the_operator_scenario_is_written_down(self) -> None:
        operations_doc = (DOCS / "OPERATIONS.md").read_text(encoding="utf-8")
        for needle in ("web-read commands", "web-read request"):
            self.assertIn(needle, operations_doc)


class LayerPropertyTests(CommandProtocolFixture):
    """The properties of the command half, checked rather than described."""

    def test_every_document_validates_against_the_published_schema(self) -> None:
        self.commit("req-1", event_id="evt_1")
        self.stage("req-staged", event_id="evt_staged", minute=5)
        self.generic("req-generic")
        documents = (
            self.layer().command_history(),
            self.layer().command_request("req-1"),
            self.layer().command_request("req-staged"),
            self.layer().command_request("never-sent"),
        )
        for document in documents:
            with self.subTest(kind=document["kind"], request=document.get("request_id")):
                self.assertEqual(validate(document, "web-command", document["kind"]), [])
                json.dumps(document)

    def test_a_refused_document_still_validates_against_the_schema(self) -> None:
        with mock.patch.object(SqlTaskAudit, "events_page", side_effect=PermissionError("denied")):
            history = self.layer().command_history()
        with mock.patch.object(SqlTaskAudit, "committed_event", side_effect=PermissionError("denied")):
            request = self.layer().command_request("req-1")
        for document in (history, request):
            with self.subTest(kind=document["kind"]):
                self.assertEqual(validate(document, "web-command", document["kind"]), [])

    def test_the_reads_open_no_second_store_index_or_scheduler(self) -> None:
        """Criterion 1 and the card's out-of-scope, as a fact about the data plane.

        The reads are given a data plane with commands on it and answer from it; afterwards the set
        of files under `<data>/` is the one they were handed. Nothing was indexed, cached or
        scheduled into existence.
        """
        self.create()
        self.commit("req-committed", event_id="evt_1")
        before = set(self.data_plane())
        self.layer().command_history()
        self.layer().command_request("req-committed")
        self.assertEqual(set(self.data_plane()), before)

    def test_the_layer_is_guarded_like_every_other_operation_of_this_package(self) -> None:
        for name in operations(CommandReadLayer):
            with self.subTest(operation=name):
                self.assertTrue(getattr(getattr(CommandReadLayer, name), "__webproto_guarded__", False))

    def test_an_implementation_failure_leaves_the_layer_as_a_typed_code(self) -> None:
        with (
            mock.patch.object(command_reads.CommandReadLayer, "_history", side_effect=OSError("no disk")),
            self.assertRaises(ReadError) as refused,
        ):
            self.layer().command_history()
        self.assertEqual(refused.exception.code, "backend_unavailable")


class CommandClientTests(CommandProtocolFixture):
    """Criterion 6: the two commands are clients holding no rule, with the existing exit statuses."""

    def _args(self, **kwargs: Any) -> Namespace:
        defaults: dict[str, Any] = {
            "instance": str(self.instance),
            "data_dir": str(self.data_dir),
            "json": True,
            "cursor": None,
            "limit": 50,
            "request_id": "req-1",
        }
        defaults.update(kwargs)
        return Namespace(**defaults)

    def test_both_commands_are_clients_of_the_named_reads(self) -> None:
        self.commit("req-1", event_id="evt_1")
        with mock.patch.object(
            CommandReadLayer, "command_history", return_value={"kind": "command_history"}
        ) as history:
            self.assertEqual(run_web_read_commands(self._args(cursor="c", limit=7)), 0)
        history.assert_called_once_with("c", limit=7)
        with mock.patch.object(
            CommandReadLayer, "command_request", return_value={"kind": "command_request"}
        ) as request:
            self.assertEqual(run_web_read_request(self._args(request_id="req-1")), 0)
        request.assert_called_once_with("req-1")

    def test_a_document_reaches_stdout_and_a_refusal_its_exit_status(self) -> None:
        self.commit("req-1", event_id="evt_1")
        self.assertEqual(run_web_read_commands(self._args()), 0)
        self.assertEqual(run_web_read_request(self._args()), 0)
        broken = self.tmp / "not-an-installation"
        broken.mkdir(exist_ok=True)
        (broken / "instance.yaml").write_text("version: 1\nname: broken\n", encoding="utf-8")
        # `validation` keeps the status `ummanu web-read` already answers it with.
        self.assertEqual(run_web_read_commands(self._args(instance=str(broken), data_dir=None)), 2)
        self.assertEqual(run_web_read_request(self._args(instance=str(broken), data_dir=None)), 2)

    def test_the_command_holds_no_rule_of_its_own(self) -> None:
        """Whatever the read says is what is printed: the client decides nothing about it."""
        answered = {"kind": "command_history", "commands": {"items": None}}
        with mock.patch.object(CommandReadLayer, "command_history", return_value=answered):
            self.assertEqual(run_web_read_commands(self._args()), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
