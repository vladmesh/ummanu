"""The web layers keep their own contracts (ummanu-238), hermetically: no board store, no network.

* One cause, one code: an instance config that cannot be used is `InstallationUnavailable` (503)
  from every layer, readers and writers alike (CON-07).
* The refusal names the config and its schema even when the report lists no error (BUG-24).
* What a layer answers validates against the schema it is published under (CON-02).
* A sprint create answers `attention` from the owner events it was given (BUG-24), and a listing
  takes that reading once, not once per sprint (INEF-15).
* The leaf helpers live in one module, and the dashboard reads nothing it does not use (DUP-W3,
  INEF-17).

The same contracts over a real board are in the `integration-board` suites (`test_web_*_protocol`).
"""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest import mock

from tests import source_trees
from tests.owner_event_fakes import CountingOwnerEventLayer
from ummanu.config import InstanceReport, validate
from ummanu.web import pages
from ummanu.web.statuses import status_for
from ummanu.webproto import sources
from ummanu.webproto.card_ops import CardOperationLayer
from ummanu.webproto.command_reads import CommandReadLayer
from ummanu.webproto.errors import InstallationUnavailable, ReadError
from ummanu.webproto.ops import OperationLayer
from ummanu.webproto.pause_ops import PauseOperationLayer
from ummanu.webproto.pause_reads import PauseReadLayer
from ummanu.webproto.reads import ReadLayer
from ummanu.webproto.section import Reading, SourceSet
from ummanu.webproto.sprint_ops import SprintOperationLayer
from ummanu.webproto.sprint_reads import SOURCE_CARDS, SOURCE_SPRINTS, SprintReadLayer

SRC = Path(__file__).resolve().parents[1] / "src" / "ummanu"


class BrokenInstallation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.broken = self.tmp / "broken"
        self.broken.mkdir()
        # Validates as YAML, not as an installation: no data directory.
        (self.broken / "instance.yaml").write_text("version: 1\nname: broken\n", encoding="utf-8")

    def refusal(self, call) -> ReadError:
        with self.assertRaises(ReadError) as refused:
            call()
        return refused.exception


class InstallationCodeTests(BrokenInstallation):
    """CON-07: the pause and command-history reads answered `validation` (400) where all others gave 503."""

    def calls(self) -> dict[str, Any]:
        broken = self.broken
        return {
            "reads.system_snapshot": lambda: ReadLayer(broken).system_snapshot(),
            "ops.run_list": lambda: OperationLayer(broken).run_list("ummanu-1"),
            "sprint_reads.sprint_list": lambda: SprintReadLayer(broken).sprint_list(),
            "sprint_ops.sprint_comment": lambda: SprintOperationLayer(broken).sprint_comment(
                request_id="r", actor="web", reference="sprint:1", body="x"
            ),
            "card_ops.task_comment": lambda: CardOperationLayer(broken).task_comment(
                request_id="r", actor="web", reference="ummanu-1", body="x"
            ),
            "pause_reads.pause_state": lambda: PauseReadLayer(broken).pause_state(),
            "pause_reads.pause_scope": lambda: PauseReadLayer(broken).pause_scope(),
            "pause_ops.pause_drain": lambda: PauseOperationLayer(broken).pause_drain(actor="web", reason="x"),
            "pause_ops.pause_resume": lambda: PauseOperationLayer(broken).pause_resume(actor="web"),
            "command_reads.command_history": lambda: CommandReadLayer(broken).command_history(),
            "command_reads.command_request": lambda: CommandReadLayer(broken).command_request("r"),
        }

    def test_every_layer_refuses_an_unusable_config_with_one_code_and_one_status(self) -> None:
        for name, call in self.calls().items():
            with self.subTest(operation=name):
                refused = self.refusal(call)
                self.assertIsInstance(refused, InstallationUnavailable)
                self.assertEqual(status_for(refused.code), 503)

    def test_report_and_data_dir_refuse_the_same_way_in_the_two_reads_that_differed(self) -> None:
        for layer in (PauseReadLayer(self.broken), CommandReadLayer(self.broken)):
            for name, call in (("report", layer.report), ("data_dir", layer.data_dir)):
                with self.subTest(layer=type(layer).__name__, call=name):
                    self.assertIsInstance(self.refusal(call), InstallationUnavailable)

    def test_with_a_data_directory_the_reads_still_answer_and_mark_the_installation(self) -> None:
        """An explicit data directory keeps what does not need the config; only the installation source refuses."""
        data = self.tmp / "data"
        data.mkdir()
        state = PauseReadLayer(self.broken, data_dir=data).pause_state()
        installation = state["sources"]["installation"]["source"]
        self.assertEqual(installation["state"], "unavailable")
        self.assertIn("does not validate", installation["reason"])
        self.assertEqual(state["sources"]["pause"]["source"]["state"], "available")


class EmptyReportMessageTests(BrokenInstallation):
    """BUG-24: a report with no errors (a config with no data directory) gave `does not validate: `."""

    def report(self) -> InstanceReport:
        return InstanceReport(
            instance_path=self.broken / "instance.yaml",
            name="broken",
            projects=0,
            adapters=0,
            adapter_drafts=0,
            has_manifest=False,
            manifest_path=None,
            errors=[],
            warnings=[],
            bindings=[],
            host={},
            instance={},
            data_dir=None,
        )

    def test_every_layer_names_the_config_and_its_schema_without_an_error_list(self) -> None:
        # Where each layer looks its config up: the two installation-source reads share `section`'s.
        layers = (
            ("ops", OperationLayer),
            ("reads", ReadLayer),
            ("sprint_ops", SprintOperationLayer),
            ("card_ops", CardOperationLayer),
            ("sprint_reads", SprintReadLayer),
            ("section", PauseReadLayer),
            ("section", CommandReadLayer),
        )
        for module, layer in layers:
            with self.subTest(layer=layer.__name__):
                with mock.patch(f"ummanu.webproto.{module}.validate_instance", return_value=self.report()):
                    message = self.refusal(layer(self.broken).report).message
                self.assertIn(str(self.broken / "instance.yaml"), message)
                self.assertIn("`instance` schema", message)
                self.assertIn("names no data directory", message)
                self.assertFalse(message.rstrip().endswith(":"), message)

    def test_a_report_with_errors_keeps_its_sentence(self) -> None:
        message = self.refusal(OperationLayer(self.broken).report).message
        self.assertTrue(
            message.startswith("this instance config does not validate: instance.yaml: <root>: "), message
        )
        self.assertIn("data_dir", message)


class PublishedSchemaTests(unittest.TestCase):
    """CON-02 and the schemas the card and provider writes gained."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_cards_run_listing_validates_against_web_run(self) -> None:
        listing = OperationLayer(self.tmp, data_dir=self.tmp / "data", clock=lambda: 0.0).run_list("ummanu-1")
        self.assertEqual(listing["kind"], "product_runs")
        self.assertEqual(validate(listing, "web-run", listing["kind"]), [])

    def test_a_card_comment_and_move_validate_against_web_card(self) -> None:
        layer = CardOperationLayer(self.tmp, data_dir=self.tmp, board_client=object(), clock=lambda: 0.0)
        written = {"action": "commented", "event_id": "evt_1", "replayed": False}
        with mock.patch.object(CardOperationLayer, "_write", return_value=written):
            documents = [
                layer.task_comment(request_id="r-1", actor="web", reference="ummanu-1", body="hold on"),
                layer.task_move(request_id="r-2", actor="web", reference="ummanu-1", target="blocked", reason="x"),
            ]
        with mock.patch.object(CardOperationLayer, "_write", return_value={}):
            documents.append(
                layer.task_comment(request_id="r-3", actor="web", reference="ummanu-1", body="no event")
            )
        self.assertIsNone(documents[-1]["event_id"])
        for document in documents:
            with self.subTest(kind=document["kind"], request=document["request_id"]):
                self.assertEqual(validate(document, "web-card", document["kind"]), [])

    def test_the_card_schema_refuses_a_kind_it_does_not_publish(self) -> None:
        layer = CardOperationLayer(self.tmp, data_dir=self.tmp, board_client=object(), clock=lambda: 0.0)
        with mock.patch.object(CardOperationLayer, "_write", return_value={}):
            document = layer.task_comment(request_id="r-1", actor="web", reference="ummanu-1", body="x")
        self.assertNotEqual(validate({**document, "kind": "card_decided"}, "web-card", "card"), [])

    def test_the_new_schemas_ship_with_the_package(self) -> None:
        """`ummanu.schemas` package data is `*.json`, so a schema file there is in the wheel."""
        for name in ("web-card", "web-provider", "web-run"):
            self.assertTrue((SRC / "schemas" / f"{name}.schema.json").is_file(), name)


class SprintAttentionTests(BrokenInstallation):
    """BUG-24 and INEF-15, at the layer seams; the same over a real board is `test_web_sprint_protocol`."""

    def test_the_create_answers_through_a_read_layer_holding_its_owner_events(self) -> None:
        events = CountingOwnerEventLayer()
        layer = SprintOperationLayer(self.broken, data_dir=self.tmp, owner_events=events)
        self.assertIs(layer._reads().owner_events, events)

    def test_the_served_sprint_operations_get_the_bells_owner_events(self) -> None:
        from ummanu.web import commands as web_commands

        served: list[Any] = []
        args = Namespace(
            instance=str(self.broken), data_dir=str(self.tmp), offline=True, heads_registry=None,
            host="127.0.0.1", port=0,
        )
        with (
            mock.patch.object(web_commands, "hold_store_exclusion", return_value=None),
            mock.patch.object(web_commands, "serve", side_effect=lambda app, **_: served.append(app) or 0),
        ):
            self.assertEqual(web_commands.run_web_serve(args), 0)
        [app] = served
        self.assertIs(app.sprint_ops.owner_events, app.owner_events)
        self.assertIs(app.sprint_reads.owner_events, app.owner_events)

    def test_one_reading_answers_every_sprint_of_a_document(self) -> None:
        events = CountingOwnerEventLayer([{"event_id": 7, "subject_ref": "ummanu-9", "sprint_ref": "sprint:2"}])
        layer = SprintReadLayer(self.broken, data_dir=self.tmp, owner_events=events)
        waits = layer._waits()
        answered = {
            ref: layer._attention(self.sprint(ref, status), waits)
            for ref, status in (("sprint:1", "open"), ("sprint:2", "open"), ("sprint:3", "closed"))
        }
        self.assertEqual(events.snapshots, 1)
        self.assertEqual([answered[ref]["state"] for ref in sorted(answered)], ["none", "waiting", "none"])
        self.assertEqual(answered["sprint:2"]["event_ids"], [7])

    def test_no_sprint_that_needs_it_takes_no_reading(self) -> None:
        events = CountingOwnerEventLayer()
        layer = SprintReadLayer(self.broken, data_dir=self.tmp, owner_events=events)
        layer._attention(self.sprint("sprint:3", "closed"), layer._waits())
        self.assertEqual(events.snapshots, 0)

    @staticmethod
    def sprint(ref: str, status: str) -> SourceSet:
        now = 0.0
        return SourceSet(
            [
                Reading(SOURCE_SPRINTS, sources.available(now), ({"ref": ref, "status": status}, {})),
                Reading(SOURCE_CARDS, sources.available(now), None),
            ]
        )


class LeafHelperTests(unittest.TestCase):
    """DUP-W3 and INEF-17, as the tree states them."""

    def test_the_value_helpers_are_defined_only_in_their_leaf_module(self) -> None:
        names = {"_reason", "_text", "_float", "_typed_reason"}
        found = []
        for path in sorted((SRC / "webproto").glob("*.py")):
            tree = source_trees.parse(path.read_text(encoding="utf-8"))
            found += [
                (path.name, node.name)
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names
            ]
        self.assertEqual(sorted(found), sorted(("_values.py", name) for name in names))

    def test_the_dashboard_takes_no_provider_limits(self) -> None:
        self.assertNotIn("limits", inspect.signature(pages.dashboard).parameters)
        for path in sorted((SRC / "web").glob("*.py")):
            self.assertNotIn("del limits", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
