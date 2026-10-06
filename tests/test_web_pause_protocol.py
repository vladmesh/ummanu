"""The pause half of the transport-independent layer: the two reads and the two operations.

The acceptance criteria of secretary-1576, as tests rather than as prose. Four of them are about
what the answer must never let a caller believe -- that a pause is per sprint, that a soft pause
stops a running head, that a freeze can be reached by asking for a drain, and that a source which
refused may still answer -- so they are checked on the documents themselves and not only on the
happy path.

Nothing here touches a live installation: every pause, resume, conflict and freeze runs against the
fixture's own data plane and its `FakeHost`.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from tests.webproto_pause_fixtures import EXISTING_CARD, PauseProtocolFixture
from ummanu.board.sql_cards import BOARD_ID
from ummanu.config import validate
from ummanu.dispatch import pause_ops as dispatcher_pause_ops
from ummanu.dispatch.pause_ops import PauseCommandCompleted
from ummanu.dispatch.pause_ops import pause as dispatcher_pause
from ummanu.dispatch.pause_ops import resume as dispatcher_resume
from ummanu.dispatch.types import DispatcherError
from ummanu.webproto.errors import OwnerConflict, ReadError, ValidationRefused
from ummanu.webproto.pause_ops import PAUSE_ERRORS, PauseOperationLayer
from ummanu.webproto.pause_reads import DRAIN, PIPELINE_WIDE, PauseReadLayer
from ummanu.webproto.section import Section, SectionSet, sections

#: Board methods that change something. A read may call none of them.
BOARD_WRITES = ("createTask", "createProject", "saveTaskMetadata", "updateTask", "moveTaskPosition")


class ScopeReadTests(PauseProtocolFixture):
    """Criterion 2: what a command would reach, answered before the command is issued."""

    def test_the_scope_says_the_pause_is_pipeline_wide_and_not_per_sprint(self) -> None:
        self.create()
        document = self.pause_reads().pause_scope()
        self.assertEqual(document["kind"], "pause_scope")
        self.assertEqual(
            document["extent"], {"scope": "pipeline", "per_sprint": False, "statement": PIPELINE_WIDE}
        )
        # In words, and not only as a flag a client has to know to look at.
        self.assertIn("no per-sprint pause", document["extent"]["statement"])

    def test_the_scope_names_the_dispatcher_and_the_files_a_command_would_write(self) -> None:
        document = self.pause_reads().pause_scope()
        self.assertEqual(document["target"]["dispatcher"], "production")
        self.assertEqual(document["target"]["pause_file"], str(self.pause_file()))
        self.assertEqual(document["target"]["state_file"], str(self.production_path()))
        self.assertTrue(document["target"]["legacy_mirror_file"])
        self.assertEqual(document["dispatcher"]["kind"], "production")
        self.assertEqual(document["dispatcher"]["phase"], "production")

    def test_the_scope_lists_every_open_sprint(self) -> None:
        """Every open sprint, because the flag is one and covers all of them."""
        first = self.reference_of(self.create())
        second = self.add_sprint_row("sprint:900", goal="a second open sprint")
        self.add_sprint_row("sprint:901", status="closed", goal="a sprint that ended")

        document = self.pause_reads().pause_scope()
        self.assertEqual([item["ref"] for item in document["sprints"]["items"]], sorted([first, second]))
        self.assertEqual(document["sprints"]["other_sprints"], 1)

    def test_the_scope_names_every_card_on_the_board_and_not_only_the_linked_ones(self) -> None:
        """A drain stops the dispatcher claiming a card whether or not a sprint holds it.

        So the scope names every card, with the sprint that holds it where one does. Listing only
        the sprint-linked cards and saying "there are others" is not the scope; it is the admission
        that the scope was not shown.
        """
        sprint = self.add_sprint_row("sprint:900", goal="a second open sprint")
        self.link_card(EXISTING_CARD, sprint, state="in_progress")
        self.add_card("ummanu-77", state="ready")
        self.add_card("ummanu-78", state="blocked", sprint="sprint:901")

        document = self.pause_reads().pause_scope()
        self.assertEqual(
            document["cards"]["items"],
            [
                {"ref": EXISTING_CARD, "sprint": sprint, "state": "in_progress"},
                # No sprint holds it, and the relationship is null rather than absent or empty.
                {"ref": "ummanu-77", "sprint": None, "state": "ready"},
                # Held by a sprint that is not open: still a card a drain stops claiming.
                {"ref": "ummanu-78", "sprint": "sprint:901", "state": "blocked"},
            ],
        )
        # And the Product and Issue records that live on the same board are not cards: the board's
        # own rule is that such a record never takes a claim, so a pause reaches none of them.
        listed = {item["ref"] for item in document["cards"]["items"]}
        self.assertEqual(listed & {"product:ummanu", "issue:open", "issue:foreign"}, set())

    def test_the_card_list_costs_no_extra_board_pass(self) -> None:
        """Removing a filter, not adding a read: the Pipeline is listed once for the document."""
        self.add_card("ummanu-77")
        before = len(self.board.calls)
        self.pause_reads().pause_scope()
        listings = [
            method
            for method, params in self.board.calls[before:]
            if method == "getAllTasks" and params.get("project_id") == BOARD_ID
        ]
        self.assertEqual(len(listings), 1)

    def test_the_extent_statement_claims_no_omission_the_document_does_not_make(self) -> None:
        """The prose fails with the code, rather than after it.

        Twice on this card a behaviour change left a public sentence behind, and this is the sentence
        that was left: while the scope listed only the sprints' cards, `extent.statement` said so,
        and when the scope grew to the whole board the sentence still said the cards no sprint holds
        were not listed. So the claim is checked against the document rather than read: every
        claimable card on the board is in `cards.items`, and the statement makes no omission claim.
        """
        sprint = self.add_sprint_row("sprint:900", goal="an open sprint")
        self.link_card(EXISTING_CARD, sprint, state="in_progress")
        self.add_card("ummanu-77", state="ready")
        document = self.pause_reads().pause_scope()

        # Every live card the board holds: Products and Issues are not cards (§3.1, §3.2).
        claimable = {
            str(row["reference"]) for row in self.board.restore_card_rows() if row["is_active"]
        }
        self.assertEqual({item["ref"] for item in document["cards"]["items"]}, claimable)
        statement = document["extent"]["statement"].lower()
        for claim in ("does not list", "not listed", "omit", "there are others", "only the cards"):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, statement)
        # And it still says the thing it exists to say.
        self.assertIn("no per-sprint pause", statement)
        self.assertIn("every card", statement)

    def test_the_scope_says_a_drain_stops_no_running_head_and_lists_the_heads_it_leaves(self) -> None:
        self.tracked_head()
        document = self.pause_reads().pause_scope()
        self.assertEqual(
            document["heads"]["cards"],
            [
                {
                    "ref": EXISTING_CARD,
                    "state": "claimed",
                    "worker": "running",
                    "reviewer": "not-running",
                    "workspace": str(self.data_dir / "workspaces" / EXISTING_CARD),
                }
            ],
        )
        drain = document["modes"]["drain"]
        self.assertEqual(drain["mode"], DRAIN)
        self.assertIn("stops no running head", drain["statement"])
        self.assertIn("a worker head that is already running", drain["does_not_stop"])

    def test_the_scope_says_what_a_freeze_would_stop_without_offering_one(self) -> None:
        """Criterion 5, on the document: the other mode is described, and it is not on offer here."""
        document = self.pause_reads().pause_scope()
        freeze = document["modes"]["freeze"]
        self.assertEqual(freeze["mode"], "freeze")
        # No operation of this layer produces one, and the document says so rather than naming a
        # variant a client could call.
        self.assertIsNone(freeze["operation"])
        self.assertIn("the live worker and reviewer heads of every tracked card", freeze["stops"])
        self.assertIn("never reached implicitly", freeze["statement"])
        self.assertEqual(document["modes"]["drain"]["operation"], "pause_drain")

    def test_no_field_of_either_document_says_a_soft_pause_stops_a_head(self) -> None:
        """Criterion 4, over the whole document rather than at the fields somebody thought of.

        A drained pipeline is read back and every `stopped_*` list must be empty: those lists are
        what a freeze fills, and a drain filling one would be the claim this card exists to prevent.
        """
        self.tracked_head()
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        for document in (self.pause_reads().pause_state(), self.pause_reads().pause_scope()):
            with self.subTest(kind=document["kind"]):
                state = document["state"]
                self.assertEqual(state["mode"], DRAIN)
                self.assertEqual(state["stopped_worker"], [])
                self.assertEqual(state["stopped_reviewer"], [])
                self.assertEqual(state["stopped_observer"], [])
                self.assertIn("cards already in flight kept running", state["on_resume"])
                self.assertEqual(document["heads"]["cards"][0]["worker"], "running")


class ReadsWriteNothingTests(PauseProtocolFixture):
    """Criterion 3: the reads write nothing and start, stop or wake nothing."""

    def test_the_scope_read_changes_no_byte_of_the_data_plane(self) -> None:
        self.create()
        self.tracked_head()
        self.link_card(EXISTING_CARD, "sprint:1")
        before = self.data_plane()
        calls = len(self.board.calls)
        document = self.pause_reads().pause_scope()
        self.assertEqual(self.data_plane(), before)
        self.assertEqual(document["kind"], "pause_scope")
        self.assertEqual([method for method, _ in self.board.calls[calls:] if method in BOARD_WRITES], [])
        self.assertFalse(self.pause_file().exists())

    def test_the_state_read_changes_no_byte_of_the_data_plane(self) -> None:
        self.tracked_head()
        before = self.data_plane()
        self.pause_reads().pause_state()
        self.assertEqual(self.data_plane(), before)

    def test_a_read_of_a_paused_pipeline_neither_lifts_nor_extends_it(self) -> None:
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        before = self.data_plane()
        self.pause_reads().pause_scope()
        self.pause_reads().pause_state()
        self.assertEqual(self.data_plane(), before)

    def test_the_reads_create_no_sprint_board(self) -> None:
        """A read creates nothing, and the board this installation has never had is one of them."""
        document = self.pause_reads().pause_scope()
        self.assertEqual(document["sprints"]["items"], [])
        self.assertFalse(any(method == "createProject" for method, _ in self.board.calls))

    def test_no_head_is_touched_by_either_read(self) -> None:
        self.tracked_head()
        self.pause_reads().pause_scope()
        self.pause_reads().pause_state()
        # The read layer has no host seam at all; this is the belt to that brace.
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.stopped, [])


class SourceIsolationTests(PauseProtocolFixture):
    """Criterion 7: a refused source reaches no claim, alone and in combination."""

    def test_an_unreadable_pause_flag_takes_only_the_pause_state(self) -> None:
        self.create()
        self.tracked_head()
        self.unreadable(self.pause_file())
        document = self.pause_reads().pause_scope()

        source = self.assert_unavailable(document, "state")
        self.assertEqual(source["name"], "pause")
        self.assertIn("the pause flag could not be read", source["reason"])
        # The rule `ProductionPause.load` already holds, said as the consequence of the refusal and
        # never as a claim that the pipeline is frozen.
        self.assertIn("reads an unreadable flag as a freeze", source["reason"])
        self.assertIsNone(document["state"]["paused"])
        self.assertIsNone(document["state"]["mode"])
        # And everything the flag does not own still answers.
        self.assert_available(document, "heads")
        self.assert_available(document, "sprints")
        self.assert_available(document, "cards")
        self.assertEqual(document["heads"]["cards"][0]["worker"], "running")

    def test_an_unreadable_production_state_takes_only_the_heads_and_the_dispatcher(self) -> None:
        self.create()
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        self.unreadable(self.production_path())
        document = self.pause_reads().pause_scope()

        for name in ("heads", "dispatcher"):
            source = self.assert_unavailable(document, name)
            self.assertEqual(source["name"], "liveness")
            self.assertIn("production state could not be read", source["reason"])
        self.assertIsNone(document["heads"]["cards"])
        self.assertIsNone(document["heads"]["observers"])
        self.assert_available(document, "state")
        self.assertEqual(document["state"]["mode"], DRAIN)
        self.assert_available(document, "sprints")

    def test_both_unreadable_leaves_no_claim_and_still_says_the_pause_is_pipeline_wide(self) -> None:
        self.create()
        self.unreadable(self.pause_file())
        self.unreadable(self.production_path())
        document = self.pause_reads().pause_scope()

        self.assert_unavailable(document, "state")
        self.assert_unavailable(document, "heads")
        self.assert_unavailable(document, "dispatcher")
        self.assertIsNone(document["state"]["paused"])
        self.assertIsNone(document["dispatcher"]["kind"])
        # The extent is read from no source, so it is stated exactly when a reader most needs it.
        self.assertEqual(document["extent"]["scope"], "pipeline")
        self.assertFalse(document["extent"]["per_sprint"])
        self.assert_available(document, "sprints")

    def test_an_unreadable_sprint_board_leaves_the_pause_and_the_heads_standing(self) -> None:
        self.tracked_head()
        with mock.patch.object(type(self.board), "call", side_effect=OSError("the sprint board is gone")):
            document = self.pause_reads().pause_scope()
        for name in ("sprints", "cards"):
            source = self.assert_unavailable(document, name)
            self.assertIn("could not be read", source["reason"])
        # `null` and never `[]`: an empty listing is the claim that no sprint is inside the scope.
        self.assertIsNone(document["sprints"]["items"])
        self.assertIsNone(document["cards"]["items"])
        self.assert_available(document, "state")
        self.assert_available(document, "heads")

    def test_a_flag_that_parses_but_is_not_a_pause_state_refuses_as_a_source(self) -> None:
        """Semantic corruption, which is the half a narrower catch let escape as a TypeError."""
        self.create()
        self.tracked_head()
        self.corrupt_pause_flag(stopped_worker=1)
        document = self.pause_reads().pause_scope()

        source = self.assert_unavailable(document, "state")
        self.assertEqual(source["name"], "pause")
        self.assertIn("does not hold a pause state", source["reason"])
        # And it does not borrow the unreadable-flag sentence: this file parses, so the tick still
        # reads it and behaves by it. What is unestablished is what the flag says here.
        self.assertNotIn("reads an unreadable flag as a freeze", source["reason"])
        self.assertIsNone(document["state"]["paused"])
        self.assert_available(document, "heads")
        self.assert_available(document, "sprints")

    def test_a_production_state_that_parses_but_holds_an_unconvertible_record_refuses(self) -> None:
        self.create()
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        self.corrupt_production_state()
        document = self.pause_reads().pause_scope()

        for name in ("heads", "dispatcher"):
            source = self.assert_unavailable(document, name)
            self.assertEqual(source["name"], "liveness")
            self.assertIn("production state could not be read", source["reason"])
        self.assertIsNone(document["heads"]["cards"])
        self.assert_available(document, "state")
        self.assertEqual(document["state"]["mode"], DRAIN)

    def test_both_semantically_corrupt_leaves_no_claim_at_all(self) -> None:
        self.create()
        self.corrupt_pause_flag(stopped_reviewer={"not": "a list"})
        self.corrupt_production_state()
        document = self.pause_reads().pause_scope()

        self.assert_unavailable(document, "state")
        self.assert_unavailable(document, "heads")
        self.assert_unavailable(document, "dispatcher")
        self.assertIsNone(document["state"]["paused"])
        self.assertIsNone(document["dispatcher"]["kind"])
        self.assertEqual(document["extent"]["scope"], "pipeline")
        self.assert_available(document, "cards")

    def test_both_reads_survive_every_semantic_fault_the_same_way(self) -> None:
        """The two documents answer the same way, and the state read is not the softer of the two."""
        self.corrupt_pause_flag(stopped_worker=1)
        self.corrupt_production_state()
        for document in (self.pause_reads().pause_state(), self.pause_reads().pause_scope()):
            with self.subTest(kind=document["kind"]):
                self.assertEqual(document["sources"]["pause"]["source"]["state"], "unavailable")
                self.assertEqual(document["sources"]["liveness"]["source"]["state"], "unavailable")

    def test_a_record_shape_the_dispatcher_refuses_marks_the_source_unavailable(self) -> None:
        """The failure a list of exception types could not have anticipated, and did not.

        `DispatcherRecord.from_json` refuses record shapes this release does not store -- a flat
        `worker_retained_at` is one, an unknown outcome terminal path another -- with a
        `DispatcherError`, which was in no tuple. It is the production state failing to answer, so
        it marks that source unavailable and leaves the pause state readable beside it.
        """
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        self.tracked_head(worker_retained_at=1)
        document = self.pause_reads().pause_scope()

        source = self.assert_unavailable(document, "heads")
        self.assertEqual(source["name"], "liveness")
        self.assertIn("DispatcherError", source["reason"])
        self.assertIsNone(document["heads"]["cards"])
        self.assert_available(document, "state")
        self.assertEqual(document["state"]["mode"], DRAIN)

    def test_a_defect_outside_a_source_read_still_travels_as_itself(self) -> None:
        """The other half of the span: assembling a document is this layer's work, not a source.

        A broad catch that covered the whole read would turn a defect of this layer into "a source
        could not answer", which is the reader losing the one thing that would let them fix it.
        """
        with (
            mock.patch(
                "ummanu.webproto.pause_reads.extent", side_effect=ValueError("a defect of this layer")
            ),
            self.assertRaises(ValueError),
        ):
            self.pause_reads().pause_scope()

    def test_every_section_names_the_source_that_answered_it(self) -> None:
        self.create()
        self.tracked_head()
        for document in (self.pause_reads().pause_state(), self.pause_reads().pause_scope()):
            with self.subTest(kind=document["kind"]):
                for name in ("target", "dispatcher", "state", "heads"):
                    source = self.source_of(document, name)
                    self.assertIn(source["name"], {"installation", "pause", "liveness"})
                    self.assertIn(source["state"], {"available", "unavailable"})
                for name, mark in document["sources"].items():
                    self.assertEqual(mark["source"]["name"], name)

    def test_no_section_of_this_document_is_assembled_outside_the_seam(self) -> None:
        """Every section is a builder of the set, and the set is what holds the invariant."""
        from ummanu.webproto import pause_reads

        self.assertTrue(issubclass(pause_reads.PauseSections, SectionSet))
        for name in sections(pause_reads.PauseSections):
            with self.subTest(section=name):
                builder = getattr(pause_reads.SECTIONS, name)
                self.assertTrue(getattr(builder, "__webproto_section__", False))
        produced = pause_reads.SECTIONS.state(
            pause_reads.SourceSet(
                [
                    pause_reads.Reading(
                        pause_reads.SOURCE_PAUSE,
                        pause_reads.sources.available(self.clock),
                        pause_reads._flag_state({}),
                    )
                ]
            )
        )
        self.assertIsInstance(produced, Section)
        self.assertTrue(produced.trusted)


class DrainOperationTests(PauseProtocolFixture):
    """Criteria 1, 5 and 6 on the soft pause itself."""

    def test_a_drain_sets_the_one_flag_and_says_it_changed_something(self) -> None:
        document = self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        self.assertEqual(document["kind"], "pause_command")
        self.assertEqual(document["operation"], "pause_drain")
        self.assertEqual(document["action"], "paused")
        self.assertTrue(document["changed"])
        self.assertIsNone(document["restored"])
        self.assertEqual(self.pause_payload()["mode"], DRAIN)
        self.assertEqual(self.pause_payload()["actor"], "operator")
        # The state read is on the answer, from the same sections a watcher reads.
        self.assertEqual(document["state"]["state"]["mode"], DRAIN)
        self.assertEqual(document["state"]["kind"], "pause_state")

    def test_a_repeat_in_the_same_mode_is_a_no_op_that_is_told_apart(self) -> None:
        first = self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        written = self.pause_payload()
        second = self.pause_ops().pause_drain(actor="somebody-else", reason="a different reason")
        self.assertEqual(first["action"], "paused")
        self.assertEqual(second["action"], "noop")
        self.assertFalse(second["changed"])
        # A no-op writes nothing: the flag still names the pause that is actually held.
        self.assertEqual(self.pause_payload(), written)
        self.assertEqual(second["state"]["state"]["actor"], "operator")

    def test_a_drain_stops_no_head(self) -> None:
        """Criterion 4 at the operation: the record and the pane are exactly as they were."""
        self.tracked_head()
        before = json.loads(self.production_path().read_text(encoding="utf-8"))
        document = self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        self.assertEqual(json.loads(self.production_path().read_text(encoding="utf-8")), before)
        self.assertEqual(self.host.stopped, [])
        self.assertEqual(document["state"]["heads"]["cards"][0]["worker"], "running")

    def test_a_drain_asked_for_while_frozen_is_refused_and_writes_nothing(self) -> None:
        """The existing `pause_conflict`, preserved and carried as `owner_conflict`."""
        dispatcher_pause(self.runtime, mode="freeze", actor="steward", reason="a maintenance window")
        frozen = self.pause_payload()
        with self.assertRaises(OwnerConflict) as refused:
            self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        self.assertEqual(refused.exception.code, "owner_conflict")
        self.assertIn("already paused (freeze)", refused.exception.message)
        # Nothing was changed by the refusal: the freeze the operator has is still the freeze.
        self.assertEqual(self.pause_payload(), frozen)

    def test_a_drain_names_no_mode_and_no_freeze_is_reachable_from_this_layer(self) -> None:
        """Criterion 5 as structure: there is no argument, default or path that produces a freeze.

        Checked on the source rather than by calling it, because what has to hold is that no
        spelling exists: an operation that took a mode would pass this suite by never being handed
        `freeze` in a test, and fail an operator the first time something else passed one down.
        """
        source = (
            Path(__file__).resolve().parents[1] / "src" / "ummanu" / "webproto" / "pause_ops.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        layer = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "PauseOperationLayer"
        )
        methods = {node.name: node for node in layer.body if isinstance(node, ast.FunctionDef)}
        # The public surface: two operations, and neither of them is a freeze.
        self.assertEqual(
            sorted(name for name in methods if not name.startswith("_")),
            ["pause_drain", "pause_resume"],
        )
        drain = methods["pause_drain"]
        arguments = [argument.arg for argument in (*drain.args.args, *drain.args.kwonlyargs)]
        self.assertEqual(arguments, ["self", "actor", "reason"])
        self.assertEqual(drain.args.defaults, [])
        self.assertEqual([default for default in drain.args.kw_defaults if default], [])
        # And the mode it hands down is the drain constant, not a value that reached it.
        modes = [
            keyword.value
            for node in ast.walk(drain)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "mode"
        ]
        self.assertEqual([getattr(mode, "id", None) for mode in modes], ["DRAIN"])

    def test_a_missing_actor_or_reason_is_a_validation_refusal(self) -> None:
        for actor, reason in (("", "a reason"), ("operator", "")):
            with self.subTest(actor=actor, reason=reason):
                with self.assertRaises(ValidationRefused):
                    self.pause_ops().pause_drain(actor=actor, reason=reason)
                self.assertFalse(self.pause_file().exists())


class ResumeOperationTests(PauseProtocolFixture):
    """Criterion 6: a resume reports what it actually put back."""

    def test_resuming_a_drain_says_there_was_nothing_to_put_back(self) -> None:
        self.tracked_head()
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        document = self.pause_ops().pause_resume(actor="operator")
        self.assertEqual(document["action"], "resumed")
        self.assertTrue(document["changed"])
        restored = document["restored"]
        self.assertEqual(restored["resumed_mode"], DRAIN)
        self.assertEqual(restored["relaunched"], [])
        self.assertIn("the drain stopped no head", restored["statement"])
        self.assertFalse(self.pause_file().exists())
        self.assertFalse(document["state"]["state"]["paused"])

    def test_resuming_a_pipeline_that_was_not_paused_is_a_no_op(self) -> None:
        document = self.pause_ops().pause_resume(actor="operator")
        self.assertEqual(document["action"], "noop")
        self.assertFalse(document["changed"])
        self.assertIsNone(document["restored"]["resumed_mode"])
        self.assertIn("was not paused", document["restored"]["statement"])

    def test_resuming_a_freeze_reports_the_heads_it_actually_dealt_with(self) -> None:
        """The lists are `resume`'s own, which is what makes them what was really put back."""
        self.tracked_head()
        frozen = dispatcher_pause(self.runtime, mode="freeze", actor="steward", reason="a maintenance window")
        self.assertEqual(frozen["stopped_worker"], [EXISTING_CARD])
        document = self.pause_ops().pause_resume(actor="operator")
        restored = document["restored"]
        self.assertEqual(restored["resumed_mode"], "freeze")
        # The fixture's card stands in Ready, so its worker is not relaunched into work nobody
        # claimed: `resume` parks it for the next tick, and that is what is reported.
        self.assertEqual(
            restored["relaunched"] + restored["parked"] + restored["skipped"], [f"{EXISTING_CARD}:worker"]
        )
        self.assertIn("the freeze was lifted", restored["statement"])
        self.assertFalse(self.pause_file().exists())


class LayerPropertyTests(PauseProtocolFixture):
    """The properties of the pause half, checked rather than described."""

    def test_every_document_validates_against_the_published_schema(self) -> None:
        self.create()
        self.tracked_head()
        drained = self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        resumed = self.pause_ops().pause_resume(actor="operator")
        documents = (
            self.pause_reads().pause_state(),
            self.pause_reads().pause_scope(),
            drained,
            resumed,
        )
        for document in documents:
            with self.subTest(kind=document["kind"]):
                self.assertEqual(validate(document, "web-pause", document["kind"]), [])
                json.dumps(document)

    def test_a_refused_document_still_validates_against_the_schema(self) -> None:
        self.unreadable(self.pause_file())
        self.unreadable(self.production_path())
        document = self.pause_reads().pause_scope()
        self.assertEqual(validate(document, "web-pause", document["kind"]), [])

    def test_the_operations_open_no_second_flag_lock_or_store(self) -> None:
        """Criterion 1: a drain writes the one flag, under the dispatcher's own tick lock.

        The lock file is the production tick's, which is the point: the operation takes the lock
        that already serialises a pause against a tick rather than opening one of its own.
        """
        before = set(self.data_plane())
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        self.assertEqual(
            sorted(set(self.data_plane()) - before),
            ["dispatcher/pause.json", "dispatcher/production-tick.lock"],
        )
        self.assertEqual(
            str(self.runtime.production_state.tick_lock),
            str(self.data_dir / "dispatcher" / "production-tick.lock"),
        )

    def test_the_legacy_mirror_still_travels_with_the_pause(self) -> None:
        """The flag the background roles read is written and cleared by the same command, unchanged."""
        mirror = Path(
            self.pause_ops().pause_drain(actor="operator", reason="host maintenance")["state"]["state"][
                "legacy_mirror"
            ]["path"]
        )
        self.assertTrue(mirror.exists())
        self.pause_ops().pause_resume(actor="operator")
        self.assertFalse(mirror.exists())

    def test_a_failure_of_the_dispatcher_reaches_the_caller_as_a_typed_code(self) -> None:
        layer = PauseOperationLayer(self.instance, data_dir=self.data_dir, runtime=self.runtime)
        with (
            mock.patch(
                "ummanu.webproto.pause_ops._pause",
                side_effect=OSError("the flag could not be written"),
            ),
            self.assertRaises(ReadError) as refused,
        ):
            layer.pause_drain(actor="operator", reason="host maintenance")
        self.assertEqual(refused.exception.code, "backend_unavailable")


class ErrorContractTests(PauseProtocolFixture):
    """The published error contract, checked against the operations and against the document.

    `PAUSE_ERRORS` is the contract as a value. One test drives every code in it out of the real
    operation; the other reads the operations table out of `docs/PROTOCOLS.md` and holds it to the
    same value. Between them, a code that moves fails here instead of leaving a public sentence
    behind, which is what happened twice on this card.
    """

    def _broken_instance(self) -> Path:
        broken = self.tmp / "not-an-installation"
        broken.mkdir(exist_ok=True)
        (broken / "instance.yaml").write_text("version: 1\nname: broken\n", encoding="utf-8")
        return broken

    def _code(self, call) -> str:
        with self.assertRaises(ReadError) as refused:
            call()
        return refused.exception.code

    def test_every_documented_code_is_one_the_operation_actually_raises(self) -> None:
        raised: dict[str, set[str]] = {name: set() for name in PAUSE_ERRORS}
        broken = str(self._broken_instance())

        # validation, on all four: an installation whose config does not validate, and for the
        # drain also the operation's own missing input.
        raised["pause_drain"].add(
            self._code(lambda: self.pause_ops().pause_drain(actor="operator", reason=""))
        )
        raised["pause_drain"].add(
            self._code(lambda: PauseOperationLayer(broken).pause_drain(actor="operator", reason="why"))
        )
        raised["pause_resume"].add(
            self._code(lambda: PauseOperationLayer(broken).pause_resume(actor="operator"))
        )
        raised["pause_state"].add(self._code(PauseReadLayer(broken).pause_state))
        raised["pause_scope"].add(self._code(PauseReadLayer(broken).pause_scope))

        # owner_conflict: a well-formed drain refused on the state of the world.
        dispatcher_pause(self.runtime, mode="freeze", actor="steward", reason="a maintenance window")
        raised["pause_drain"].add(
            self._code(lambda: self.pause_ops().pause_drain(actor="operator", reason="host maintenance"))
        )

        # backend_unavailable: the durable write the operation makes could not be made.
        with mock.patch("ummanu.webproto.pause_ops._pause", side_effect=OSError("no disk")):
            raised["pause_drain"].add(
                self._code(lambda: self.pause_ops().pause_drain(actor="operator", reason="why"))
            )
        with mock.patch("ummanu.webproto.pause_ops._resume", side_effect=OSError("no disk")):
            raised["pause_resume"].add(self._code(lambda: self.pause_ops().pause_resume(actor="operator")))

        for operation, documented in PAUSE_ERRORS.items():
            with self.subTest(operation=operation):
                self.assertEqual(raised[operation], set(documented))

    def test_the_published_table_records_exactly_those_codes(self) -> None:
        """`docs/PROTOCOLS.md` is held to the contract, not trusted to have kept up with it."""
        protocols = (Path(__file__).resolve().parents[1] / "docs" / "PROTOCOLS.md").read_text(
            encoding="utf-8"
        )
        documented: dict[str, tuple[str, ...]] = {}
        for line in protocols.splitlines():
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) != 6:
                continue
            operation = re.fullmatch(r"`(pause_[a-z]+)`", cells[1])
            if operation is None or operation.group(1) not in PAUSE_ERRORS:
                continue
            documented[operation.group(1)] = tuple(re.findall(r"`([a-z_]+)`", cells[4]))
        self.assertEqual(documented, PAUSE_ERRORS)


class CommandClientTests(PauseProtocolFixture):
    """Criterion 8: the three commands are clients, and their exit statuses do not move."""

    def _args(self, **kwargs) -> Namespace:
        defaults = {
            "instance": str(self.instance),
            "data_dir": str(self.data_dir),
            "owner": "ummanu-dispatcher",
            "actor": "operator",
            "host_mode": "noop",
            "reason": "host maintenance",
            "reason_file": None,
            "exclude_workspace": [],
        }
        return Namespace(**{**defaults, **kwargs})

    def _run(self, handler, **kwargs) -> tuple[int, dict]:
        from ummanu.dispatch import commands as dispatcher_commands

        with (
            mock.patch.object(dispatcher_commands, "_pause_operations", return_value=self.pause_ops()),
            mock.patch.object(dispatcher_commands, "_pause_reads", return_value=self.pause_reads()),
            mock.patch("sys.stdout") as out,
            mock.patch("sys.stderr") as err,
        ):
            status = handler(self._args(**kwargs))
            written = "".join(
                call.args[0] for call in (*out.write.call_args_list, *err.write.call_args_list)
            ).strip()
        return status, json.loads(written) if written else {}

    def test_pause_drain_resume_and_the_two_reads_are_clients_of_the_operations(self) -> None:
        from ummanu.dispatch import commands as dispatcher_commands

        status, document = self._run(dispatcher_commands.run_pause, mode="drain")
        self.assertEqual(status, 0)
        self.assertEqual(document["operation"], "pause_drain")

        status, document = self._run(dispatcher_commands.run_pause_status)
        self.assertEqual(status, 0)
        self.assertEqual(document["kind"], "pause_state")
        self.assertEqual(document["state"]["mode"], DRAIN)

        status, document = self._run(dispatcher_commands.run_pause_scope)
        self.assertEqual(status, 0)
        self.assertEqual(document["kind"], "pause_scope")

        status, document = self._run(dispatcher_commands.run_resume)
        self.assertEqual(status, 0)
        self.assertEqual(document["operation"], "pause_resume")

    def test_a_command_whose_state_cannot_be_rendered_still_answers_zero_with_its_action(self) -> None:
        """Criterion 8 over the degraded path: a completed drain is a success at the command too.

        The exit status is what a script branches on, so a pause that took must not answer with the
        status of a pause that did not.
        """
        from ummanu.dispatch import commands as dispatcher_commands

        self.tracked_head(worker_retained_at=1)
        status, document = self._run(dispatcher_commands.run_pause, mode="drain")
        self.assertEqual(status, 0)
        self.assertEqual(document["action"], "paused")
        self.assertEqual(self.pause_payload()["mode"], DRAIN)

    def test_the_conflict_keeps_the_exit_status_it_always_answered_with(self) -> None:
        from ummanu.dispatch import commands as dispatcher_commands

        dispatcher_pause(self.runtime, mode="freeze", actor="steward", reason="a maintenance window")
        status, document = self._run(dispatcher_commands.run_pause, mode="drain")
        self.assertEqual(status, 3)
        self.assertEqual(document["error"]["code"], "owner_conflict")

    def test_a_pause_without_a_reason_keeps_its_usage_status(self) -> None:
        from ummanu.dispatch import commands as dispatcher_commands

        status, document = self._run(dispatcher_commands.run_pause, mode="drain", reason=None)
        self.assertEqual(status, 2)
        self.assertEqual(document["error"]["code"], "usage")

    def test_an_invalid_instance_keeps_the_exit_status_it_always_had(self) -> None:
        """Criterion 8 on the configuration refusal, which used to be the dispatcher's own exit 2.

        Before these commands were clients, every one of them reached the dispatcher through
        `runtime_from_args`, whose `invalid_instance` is a `DispatcherError` with exit status 2. A
        script reading that status must keep reading it, so the layer's typed code for a config that
        does not validate is `validation` and not `backend_unavailable`. No layer is substituted
        here: these run the real handlers over a real broken installation.
        """
        from ummanu.dispatch import commands as dispatcher_commands

        broken = self.tmp / "broken-instance"
        broken.mkdir()
        (broken / "instance.yaml").write_text("version: 1\nname: broken\n", encoding="utf-8")
        commands = (
            ("pause drain", dispatcher_commands.run_pause, {"mode": "drain"}),
            ("resume", dispatcher_commands.run_resume, {}),
            ("pause-status", dispatcher_commands.run_pause_status, {}),
            ("pause-scope", dispatcher_commands.run_pause_scope, {}),
        )
        for name, handler, extra in commands:
            with self.subTest(command=name):
                with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    status = handler(self._args(instance=str(broken), data_dir=None, **extra))
                self.assertEqual(status, 2)

    def test_pause_freeze_does_not_go_through_the_soft_path(self) -> None:
        """Criterion 5 at the command: the two spellings reach two implementations."""
        from ummanu.dispatch import commands as dispatcher_commands

        with (
            mock.patch.object(dispatcher_commands, "_pause_operations") as operations,
            mock.patch.object(dispatcher_commands, "_run_production", return_value=0) as production,
        ):
            self.assertEqual(dispatcher_commands.run_pause(self._args(mode="freeze")), 0)
        operations.assert_not_called()
        production.assert_called_once()


class CompletedCommandTests(PauseProtocolFixture):
    """Criterion 6 where it is hardest: the command did something, and the pipeline cannot be read.

    `dispatch.pause_ops.pause` and `resume` write the flag and then render the status through
    `pause_status`, which converts every dispatcher record. A production state that no longer
    converts therefore refuses *after* the pause has taken, and the operator most likely to see it
    is the one reaching for a drain because something is already wrong with the pipeline. Reported
    as `backend_unavailable` with no action, that reads as "the pause did not take" over a safety
    control that silently did.

    So the shape is the one this card already owns: what is established is stated, and the source
    that could not answer is marked unavailable. Every case here runs against the fixture and its
    `FakeHost`; nothing touches a live installation.
    """

    def _state_that_refuses_conversion(self) -> None:
        """The reviewer's reproduction: a record shape this release does not store.

        `DispatcherRecord.from_json` refuses a flat `worker_retained_at` with a `DispatcherError`,
        and `pause_status` converts every record, so this is one of the several semantic corruptions
        -- an unknown outcome terminal path, a non-integer `attempt_round`, a truncated write --
        that reach the status read after the flag is already written.
        """
        self.tracked_head(worker_retained_at=1)

    def _make_the_records_refuse(self) -> None:
        """The same refusal, applied to the state a command has already written into.

        Separate because the shape cannot be there from the start when the case needs a freeze
        first: `pause freeze` reads the records itself and would be refused before it set anything.
        """
        payload = json.loads(self.production_path().read_text(encoding="utf-8"))
        payload["records"][EXISTING_CARD]["worker_retained_at"] = 1
        self.production_path().write_text(json.dumps(payload), encoding="utf-8")

    def test_a_drain_over_a_state_that_refuses_conversion_reports_the_pause_it_set(self) -> None:
        self._state_that_refuses_conversion()
        document = self.pause_ops().pause_drain(actor="operator", reason="host maintenance")

        self.assertEqual(document["kind"], "pause_command")
        self.assertEqual(document["action"], "paused")
        self.assertTrue(document["changed"])
        # The flag reached the state the command intended, which is what makes this an action.
        self.assertEqual(self.pause_payload()["mode"], DRAIN)
        # And the document says what could not be answered rather than dropping the answer: the
        # pause is readable, the liveness of the heads is not.
        self.assert_available(document["state"], "state")
        self.assertEqual(document["state"]["state"]["mode"], DRAIN)
        self.assert_unavailable(document["state"], "heads")
        self.assertIsNone(document["state"]["heads"]["cards"])
        self.assertTrue(
            any("could not render the pipeline state" in warning for warning in document["warnings"]),
            document["warnings"],
        )
        self.assertTrue(
            any("unsupported legacy dispatcher record" in warning for warning in document["warnings"]),
            document["warnings"],
        )

    def test_a_repeat_over_the_same_state_is_still_the_no_op_it_always_was(self) -> None:
        """The repeat contract survives the degraded path: a second drain changed nothing, and says so."""
        self._state_that_refuses_conversion()
        first = self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        written = self.pause_payload()
        second = self.pause_ops().pause_drain(actor="somebody-else", reason="a different reason")

        self.assertEqual(first["action"], "paused")
        self.assertEqual(second["action"], "noop")
        self.assertFalse(second["changed"])
        self.assertEqual(self.pause_payload(), written)

    def test_a_resume_over_a_state_that_refuses_conversion_reports_the_pause_it_lifted(self) -> None:
        self._state_that_refuses_conversion()
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        document = self.pause_ops().pause_resume(actor="operator")

        self.assertEqual(document["operation"], "pause_resume")
        self.assertEqual(document["action"], "resumed")
        self.assertTrue(document["changed"])
        self.assertFalse(self.pause_file().exists())
        restored = document["restored"]
        self.assertEqual(restored["resumed_mode"], DRAIN)
        # A drain stopped nothing, so what it put back is established by what a drain is rather than
        # by a list nobody could read: these are empty, not unknown.
        self.assertEqual(restored["relaunched"], [])
        self.assertIn("the drain stopped no head", restored["statement"])
        self.assert_unavailable(document["state"], "heads")

    def test_a_resume_whose_own_report_never_arrived_says_what_it_cannot_say(self) -> None:
        """A freeze lifted, and the buckets it produced lost with the answer that carried them.

        The status read is refused directly here rather than through a corrupt record, because that
        is the fault this pins: the *ordering*, not any one way of reaching it. What a freeze's
        resume relaunched, parked and skipped was in the answer that never arrived, so the lists are
        `null` -- nobody read them -- and never `[]`, which would claim it put nothing back.
        """
        self.tracked_head()
        dispatcher_pause(self.runtime, mode="freeze", actor="steward", reason="a maintenance window")
        with mock.patch(
            "ummanu.dispatch.pause_ops.pause_status",
            side_effect=DispatcherError("unsupported_legacy_record", "the records do not convert", 1),
        ):
            document = self.pause_ops().pause_resume(actor="operator")

        self.assertEqual(document["action"], "resumed")
        self.assertTrue(document["changed"])
        self.assertFalse(self.pause_file().exists())
        restored = document["restored"]
        self.assertEqual(restored["resumed_mode"], "freeze")
        self.assertIsNone(restored["relaunched"])
        self.assertIsNone(restored["parked"])
        self.assertIsNone(restored["skipped"])
        self.assertIn("not established here", restored["statement"])
        self.assertEqual(validate(document, "web-pause", document["kind"]), [])

    def test_a_command_that_did_not_reach_its_intended_state_still_fails(self) -> None:
        """The other half of the rule, and the one that keeps this from being a repair.

        `resume` of a freeze reads the records before it clears anything, so a state that refuses
        conversion stops it *before* the flag is touched. The pipeline is still frozen, and the
        caller is told so: the refusal travels unchanged and nothing invents an action.
        """
        self.tracked_head()
        dispatcher_pause(self.runtime, mode="freeze", actor="steward", reason="a maintenance window")
        self._make_the_records_refuse()

        with self.assertRaises(ReadError) as refused:
            self.pause_ops().pause_resume(actor="operator")
        self.assertEqual(refused.exception.code, "backend_unavailable")
        self.assertEqual(self.pause_payload()["mode"], "freeze")

    def test_a_refusal_of_the_pause_rules_is_never_reported_as_an_action(self) -> None:
        """Criterion 5 through the degraded path: a conflict is still a conflict, and writes nothing."""
        self.tracked_head()
        dispatcher_pause(self.runtime, mode="freeze", actor="steward", reason="a maintenance window")
        self._make_the_records_refuse()
        frozen = self.pause_payload()

        with self.assertRaises(OwnerConflict):
            self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        with self.assertRaises(ValidationRefused):
            self.pause_ops().pause_drain(actor="operator", reason="")
        self.assertEqual(self.pause_payload(), frozen)

    def test_the_degraded_documents_validate_against_the_published_schema(self) -> None:
        self._state_that_refuses_conversion()
        drained = self.pause_ops().pause_drain(actor="operator", reason="host maintenance")
        resumed = self.pause_ops().pause_resume(actor="operator")
        for document in (drained, resumed):
            with self.subTest(action=document["action"]):
                self.assertEqual(validate(document, "web-pause", document["kind"]), [])
                json.dumps(document)


class DecidedUnderTheLockTests(PauseProtocolFixture):
    """Criteria 1 and 2: the action is the one decided where the command was serialised.

    secretary-1576's last round established what a completed command reports by reading the pause
    flag before the call and again after a failure. Both reads are outside the tick lock, and a flag
    observed before and after an unlocked command is not evidence of which command set it: another
    operator command fits between the two reads and leaves exactly the observations this one expects.
    The interleave below is the reviewer's own reproduction, and it runs against the fixture and its
    `FakeHost` like everything else here.
    """

    def _state_that_refuses_conversion(self) -> None:
        """The production state that makes the status render refuse after the flag is written."""
        self.tracked_head(worker_retained_at=1)

    def _flag_mode(self) -> str:
        """What the flag holds, as the removed inference read it: a mode, or nothing at all."""
        return self.pause_payload()["mode"] if self.pause_file().exists() else ""

    def _interleaving(self, command):
        """Run `command`, with one `resume` taking the tick lock just before it gets there.

        The interleave is placed at the lock rather than by threads on purpose: what has to be
        exercised is the window between a caller entering the command and the command being
        serialised, and a hook on the lock puts another command in exactly that window, once,
        deterministically. The interleaved resume's own status render refuses over this state --
        that is the point of the state -- so what it did travels on `PauseCommandCompleted` and is
        of no interest to the command being tested.
        """
        real_lock = dispatcher_pause_ops.file_lock
        interleaved: list[str] = []

        def hook(path):
            if not interleaved:
                interleaved.append("resume")
                try:
                    dispatcher_resume(self.runtime, actor="somebody-else")
                except PauseCommandCompleted as completed:
                    self.assertEqual(completed.decision["action"], "resumed")
            return real_lock(path)

        with mock.patch.object(dispatcher_pause_ops, "file_lock", hook):
            document = command()
        self.assertEqual(interleaved, ["resume"])
        return document

    def test_a_second_drain_that_wrote_the_flag_is_told_from_one_that_found_it_held(self) -> None:
        """The reproduction: both drains see `drain` on either side of the call, and did not do the same thing."""
        self._state_that_refuses_conversion()
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")

        # No interleave: this one really did find the mode already held, and writes nothing.
        held = self.pause_payload()
        repeat = self.pause_ops().pause_drain(actor="second-operator", reason="the same window")
        self.assertEqual(repeat["action"], "noop")
        self.assertFalse(repeat["changed"])
        self.assertEqual(self.pause_payload(), held)

        # The interleave: a resume clears the flag before this drain reaches the lock, so this drain
        # sets the pause the pipeline now holds. A flag read before and after it says `drain` both
        # times -- the same pair the repeat above produced -- and the two are still told apart.
        before = self._flag_mode()
        document = self._interleaving(
            lambda: self.pause_ops().pause_drain(actor="third-operator", reason="a different window")
        )
        # The pair the removed inference ran on, and why it could not work: it is the pair a genuine
        # repeat produces, and this command did the other thing.
        self.assertEqual((before, self._flag_mode()), (DRAIN, DRAIN))
        self.assertEqual(document["action"], "paused")
        self.assertTrue(document["changed"])
        self.assertEqual(self.pause_payload()["actor"], "third-operator")
        self.assertEqual(self.pause_payload()["reason"], "a different window")

    def test_a_resume_that_lifted_nothing_is_told_from_one_that_lifted_a_pause(self) -> None:
        """The same interleave the other way round, where the inference read `resumed` for a no-op."""
        self._state_that_refuses_conversion()
        self.pause_ops().pause_drain(actor="operator", reason="host maintenance")

        # Another resume lifts the drain first, so this one arrives at a pipeline that is not paused.
        before = self._flag_mode()
        document = self._interleaving(lambda: self.pause_ops().pause_resume(actor="operator"))
        # And the pair a resume that really lifted the drain produces, over one that lifted nothing.
        self.assertEqual((before, self._flag_mode()), (DRAIN, ""))
        self.assertEqual(document["action"], "noop")
        self.assertFalse(document["changed"])
        self.assertIsNone(document["restored"]["resumed_mode"])
        self.assertIn("was not paused", document["restored"]["statement"])
        self.assertFalse(self.pause_file().exists())

    def test_the_action_is_decided_inside_the_tick_lock_and_assigned_nowhere_else(self) -> None:
        """Where the decision is made, checked on the source rather than by calling it.

        A test that only calls the operations passes for as long as nobody moves the decision back
        out; this fails the moment the assignment leaves the locked span.
        """
        tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "src" / "ummanu" / "dispatch" / "pause_ops.py").read_text(
                encoding="utf-8"
            )
        )
        functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        locked = next(
            node
            for node in ast.walk(functions["pause"])
            if isinstance(node, ast.With) and _takes_the_tick_lock(node)
        )
        inside = {id(node) for node in ast.walk(locked)}
        actions = [
            node
            for node in ast.walk(functions["pause"])
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") == "action" for target in node.targets)
        ]
        decided = sorted(node.value.value for node in actions)  # type: ignore[attr-defined]
        self.assertEqual(decided, ["noop", "paused"])
        for node in actions:
            self.assertIn(id(node), inside)
        # And the render is outside it: it walks every record, and holding the tick lock across it
        # would make a corrupt-state read a contention problem on the dispatcher's own lock.
        self.assertNotIn("_with_status", [_called(node) for node in ast.walk(locked)])
        resume_lock = next(
            node
            for node in ast.walk(functions["resume"])
            if isinstance(node, ast.With) and _takes_the_tick_lock(node)
        )
        called = [_called(node) for node in ast.walk(resume_lock)]
        self.assertIn("_resume_under_lock", called)
        self.assertNotIn("_with_status", called)

    def test_no_operation_of_the_layer_reads_the_flag_to_decide_what_it_did(self) -> None:
        """Criterion 1 at the layer: there is no flag read here to infer an action from."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "ummanu" / "webproto" / "pause_ops.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        layer = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "PauseOperationLayer"
        )
        # The runtime is handed to the dispatcher and never asked anything by this layer.
        self.assertEqual(
            sorted(
                {
                    node.attr
                    for node in ast.walk(layer)
                    if isinstance(node, ast.Attribute) and getattr(node.value, "id", "") == "runtime"
                }
            ),
            [],
        )
        for spelling in ("pause.load", "normalize_pause_mode", "UNREADABLE_FLAG"):
            self.assertNotIn(spelling, source)

    def test_a_caller_that_does_not_know_the_decision_answers_exactly_as_before(self) -> None:
        """`ummanu pause freeze` and the tick's auto-resume are not clients of this, and stay put.

        The completed command reaches them as the `DispatcherError` the render raised -- same code,
        same message, same exit status -- so a path that never asked what the command did cannot be
        changed by the answer being available.
        """
        refused = DispatcherError("unsupported_legacy_record", "the records do not convert", 1)
        with (
            mock.patch("ummanu.dispatch.pause_ops.pause_status", side_effect=refused),
            self.assertRaises(PauseCommandCompleted) as completed,
        ):
            dispatcher_pause(self.runtime, mode="drain", actor="operator", reason="host maintenance")
        self.assertEqual(completed.exception.code, refused.code)
        self.assertEqual(completed.exception.message, refused.message)
        self.assertEqual(completed.exception.exit_code, refused.exit_code)
        self.assertIs(completed.exception.cause, refused)
        self.assertEqual(completed.exception.decision, {"step": "pause", "action": "paused"})

    def test_the_ticks_auto_resume_names_the_failure_by_the_class_that_raised_it(self) -> None:
        """The one non-protocol caller that keys on the class name, pinned so a wrapper cannot rename it.

        `auto_resume_expired_freeze` reports a failed recovery as `f"{type(exc).__name__}: {exc}"`,
        which is not the code, message and exit status :class:`PauseCommandCompleted` preserves for
        every caller that reads those. So the wrapper is unwrapped at that boundary and the tick
        emits the render's own class, exactly as it did before the class existed. The loop is over
        the two shapes a render can refuse in -- a `DispatcherError` carrying a code, and anything
        else -- because the wrapper rewrites the message of the second one as well as its class.
        """
        for cause in (
            DispatcherError("unsupported_legacy_record", "the records do not convert", 1),
            RuntimeError("the state file was truncated"),
        ):
            with self.subTest(cause=type(cause).__name__):
                self.tracked_head()
                dispatcher_pause(
                    self.runtime, mode="freeze", actor="pipeline", reason="a backup that was killed"
                )
                stale = {**self.pause_payload(), "since": "2020-01-01T00:00:00Z"}
                self.pause_file().write_text(json.dumps(stale), encoding="utf-8")

                with mock.patch.object(dispatcher_pause_ops, "pause_status", side_effect=cause):
                    outcome = dispatcher_pause_ops.auto_resume_expired_freeze(self.runtime, source="tick")

                assert outcome is not None
                self.assertTrue(outcome["eligible"])
                self.assertEqual(outcome["source"], "tick")
                self.assertFalse(outcome["resumed"])
                self.assertEqual(outcome["error"], f"{type(cause).__name__}: {cause}")
                self.assertNotIn(PauseCommandCompleted.__name__, outcome["error"])
                # The resume itself happened; only its render refused, which is what makes the
                # error field the one thing this caller reports about it.
                self.assertFalse(self.pause_file().exists())

    def test_the_published_prose_says_where_the_action_is_decided(self) -> None:
        """Criterion 6: the sentence that keeps the next reader from reintroducing the inference."""
        protocols = (Path(__file__).resolve().parents[1] / "docs" / "PROTOCOLS.md").read_text(
            encoding="utf-8"
        )
        for promise in (
            "decided inside the production tick lock",
            "is not evidence of which command set it",
            "PauseCommandCompleted",
        ):
            self.assertIn(promise, protocols)


def _takes_the_tick_lock(node: ast.With) -> bool:
    return any(_called(item.context_expr) == "file_lock" for item in node.items)


def _called(node: ast.AST) -> str:
    """The name a call node calls, or the empty string for anything else."""
    if not isinstance(node, ast.Call):
        return ""
    function = node.func
    return getattr(function, "id", "") or getattr(function, "attr", "")


if __name__ == "__main__":
    unittest.main()
