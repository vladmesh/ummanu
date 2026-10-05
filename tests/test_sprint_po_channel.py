"""A sprint's PO session and allowed productions (revision 0016): stored at create, printed, carried.

Unit-level: the sprint writer and reader run over a mock board client and an in-memory PO store
(`tests.po_fake_store`), and the SQL adapter over a client that answers its queries by hand. The
PostgreSQL path of the same create is covered by the integration-board suite.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.po_fake_store import FakePoStore
from tests.po_channel_fixtures import ADMINISTRATIVE_PO_NOTES, REQUEST_PO_NOTES, NEUTRAL_PO_NOTES
from ummanu import sprint_commands
from ummanu.board.po_channel import requests_po
from ummanu.board.sprint_read import RESUME_FIELDS, SprintResume
from ummanu.board.sprint_write import SprintCreateIntent
from ummanu.board.sql_sprints import SqlSprintRecords
from ummanu.cli import main
from ummanu.data import normalize_sprint_entity
from ummanu.dispatch.observer import render_observer_prompt
from ummanu.po import PO_SESSION_ENV
from ummanu.po.store import SESSION_CLOSED, SessionNotFound
from ummanu.restore import _restore_sprint_metadata, _sprint_core
from ummanu.sprint_observer import observer_choice
from ummanu.sprints import (
    ALLOWED_PRODUCTIONS_FIELD,
    PO_SESSION_FIELD,
    SprintReader,
    SprintWriter,
)
from ummanu.tasks import TaskError
from ummanu.webproto.sprint_reads import _identity, _sprint_value

# The metadata of a sprint row as the SQL adapter answers it for a sprint opened before 0016.
PRE_0016_META = {
    "sprint_goal": "ship it",
    "sprint_definition_of_done": "green",
    "sprint_status": "open",
    "sprint_repositories": "[]",
    "sprint_observer": '{"kind":"none"}',
    "sprint_current_task": "",
    "sprint_resume": "",
    "sprint_budget": '{"by_type":{}}',
}


class ObserverRequestAdmissionTests(unittest.TestCase):
    def test_direct_requests_and_waits_in_both_languages(self):
        for body in REQUEST_PO_NOTES:
            with self.subTest(body=body):
                self.assertTrue(requests_po(body))

    def test_notes_negation_quotations_and_future_implementation_are_not_requests(self):
        for body in NEUTRAL_PO_NOTES:
            with self.subTest(body=body):
                self.assertFalse(requests_po(body))

    def test_typed_wait_roundtrips_without_changing_historical_six_field_readback(self):
        old = dict.fromkeys(RESUME_FIELDS, "ordinary analysis")
        legacy = SprintResume.from_legacy(old, required=True, now=lambda: "2026-10-05T00:00:00Z")
        self.assertNotIn("po_request", legacy.to_document())
        wait = {**old, "current_task": "ummanu-1", "po_request": {"card": "ummanu-1", "action": "choose route"}}
        parsed = SprintResume.from_legacy(wait, required=True)
        self.assertEqual(parsed.to_document()["po_request"], wait["po_request"])
        for invalid in ({"card": "ummanu-1"}, {"card": "ummanu-1", "action": ""}, "ummanu-1"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                SprintResume.from_legacy({**old, "po_request": invalid}, required=True)

    def test_administrative_preposition_and_component_modifiers_are_not_addressees(self):
        for body in ADMINISTRATIVE_PO_NOTES:
            with self.subTest(body=body):
                self.assertFalse(requests_po(body))

    def test_other_english_request_wording_needs_explicit_representation(self):
        for body in ("The PO must decide", "Awaiting PO decision", "Let the PO decide"):
            with self.subTest(body=body):
                self.assertFalse(requests_po(body))
                self.assertTrue(requests_po("[observer:request] " + body))


def po_session_state(store: FakePoStore):
    def state(session_id: str) -> str | None:
        try:
            return store.session(session_id).state
        except SessionNotFound:
            return None

    return state


class CreateCommandTests(unittest.TestCase):
    """`sprint create --po-session/--allow-production`, down to the writer's arguments."""

    def create(self, *extra: str, env: dict[str, str] | None = None) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        class Writer:
            def create(self, **kwargs: Any) -> dict[str, Any]:
                captured.update(kwargs)
                return {}

        def write(_args, operation) -> int:
            operation(Writer())
            return 0

        environ = {key: value for key, value in os.environ.items() if key != PO_SESSION_ENV}
        environ.update(env or {})
        with (
            mock.patch.object(sprint_commands, "_write", write),
            mock.patch.dict(os.environ, environ, clear=True),
        ):
            code = main(
                [
                    "sprint",
                    "create",
                    "--role",
                    "po",
                    "--instance",
                    "/nowhere",
                    "--goal",
                    "g",
                    "--product",
                    "ummanu",
                    "--issue",
                    "issue:open",
                    "--project",
                    "ummanu",
                    "--observer",
                    "none",
                    *extra,
                ]
            )
        self.assertEqual(code, 0)
        return captured

    def test_the_flag_is_recorded(self) -> None:
        self.assertEqual(self.create("--po-session", "s-flag")["po_session"], "s-flag")

    def test_create_passes_quoted_owner_decisions_and_the_append_command_reads_the_same_format(self) -> None:
        entries = [{"id": "owner-1", "scope": "sprint", "kind": "e2e_grant", "value": 2, "quotation": "Two more runs."}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.json"
            path.write_text(json.dumps(entries), encoding="utf-8")
            self.assertEqual(self.create("--owner-decisions-file", str(path))["standing_decisions"], entries)
            captured = {}
            class Writer:
                def record_owner_decisions(self, **kwargs):
                    captured.update(kwargs)
                    return {}
            with mock.patch.object(sprint_commands, "_write", side_effect=lambda _args, operation: (operation(Writer()), 0)[1]):
                self.assertEqual(main(["sprint", "record-owner-decisions", "--instance", "/nowhere", "--ref", "sprint:7", "--role", "po", "--decisions-file", str(path), "--request-id", "r"]), 0)
            self.assertEqual(captured["entries"], entries)
            self.assertEqual(captured["request_id"], "r")

    def test_local_run_exceptions_json_file_and_default_reach_writer(self) -> None:
        entries = [{"project": "ummanu", "argv": ["docker", "run", "two words", ""], "rationale": "owner's probe"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exceptions.json"
            path.write_text(json.dumps(entries), encoding="utf-8")
            self.assertEqual(self.create("--local-run-exceptions-file", str(path))["local_run_exceptions"], entries)
        self.assertEqual(self.create()["local_run_exceptions"], [])

    def test_local_run_file_refuses_invalid_json_and_null_before_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exceptions.json"
            for value in ("not JSON", "null", "{}"):
                path.write_text(value, encoding="utf-8")
                with mock.patch.object(sprint_commands, "_write") as write, mock.patch("sys.stderr"):
                    code = main(["sprint", "create", "--role", "po", "--goal", "g", "--product", "ummanu",
                                 "--issue", "issue:open", "--project", "ummanu", "--observer", "none",
                                 "--local-run-exceptions-file", str(path)])
                self.assertEqual(code, 2)
                write.assert_not_called()

    def test_inside_a_po_turn_the_environment_is_the_default_and_the_flag_wins(self) -> None:
        self.assertEqual(self.create(env={PO_SESSION_ENV: "s-turn"})["po_session"], "s-turn")
        self.assertEqual(
            self.create("--po-session", "s-flag", env={PO_SESSION_ENV: "s-turn"})["po_session"], "s-flag"
        )

    def test_with_neither_no_session_is_passed(self) -> None:
        self.assertIsNone(self.create()["po_session"])
        self.assertIsNone(self.create(env={PO_SESSION_ENV: ""})["po_session"])

    def test_productions_default_to_none_and_repeat(self) -> None:
        self.assertEqual(self.create()["allowed_productions"], [])
        self.assertEqual(
            self.create("--allow-production", "ummanu", "--allow-production", "site")[
                "allowed_productions"
            ],
            ["ummanu", "site"],
        )


class WriterFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = self.root / "instance"
        (self.instance / "projects").mkdir(parents=True)
        for project in ("ummanu", "site"):
            (self.instance / "projects" / f"{project}.yaml").write_text(f"id: {project}\n", encoding="utf-8")
        self.po = FakePoStore()
        self.open_session = self.po.claim_session(
            session_id="s-open", cli="claude", model="opus", cwd="/po", cli_session_id="c-1"
        )[0].session_id
        self.po.claim_session(
            session_id="s-closed", cli="claude", model="opus", cwd="/po", cli_session_id="c-2"
        )
        self.po.close_session("s-closed", "owner")
        self.client = mock.MagicMock()
        self.writer = SprintWriter(
            self.client,
            data_dir=self.root / "data",
            instance=self.instance,
            po_session_state=po_session_state(self.po),
        )

    def intent(self, **options: Any) -> SprintCreateIntent:
        return self.writer._create_intent(
            role="po",
            actor="po",
            goal="g",
            definition_of_done="",
            repositories=[],
            product="ummanu",
            issues=["issue:open"],
            reservations=["ummanu"],
            reference="",
            observer=observer_choice("none"),
            **options,
        )


class CreateCheckTests(WriterFixture):
    def test_an_open_session_and_registered_productions_pass(self) -> None:
        intent = self.intent(po_session=" s-open ", allowed_productions=["site", "ummanu", "site"])
        self.writer._check_po_channel(intent)
        self.assertEqual((intent.po_session, intent.allowed_productions), ("s-open", ("site", "ummanu")))

    def test_neither_is_nothing_to_check(self) -> None:
        intent = self.intent()
        self.writer._check_po_channel(intent)
        self.assertEqual((intent.po_session, intent.allowed_productions), (None, ()))
        self.assertEqual(
            self.writer._create_values(intent).keys() & {PO_SESSION_FIELD, ALLOWED_PRODUCTIONS_FIELD}, set()
        )

    def test_an_unknown_or_closed_session_and_an_unknown_project_are_refused(self) -> None:
        for options, message in (
            ({"po_session": "s-nowhere"}, "there is no PO session s-nowhere"),
            ({"po_session": "s-closed"}, f"PO session s-closed is {SESSION_CLOSED}"),
            ({"allowed_productions": ["ummanu", "elsewhere"]}, "unknown registered project(s): elsewhere"),
        ):
            with self.subTest(options=options):
                with self.assertRaises(TaskError) as raised:
                    self.writer._check_po_channel(self.intent(**options))
                self.assertEqual(raised.exception.code, "validation")
                self.assertIn(message, raised.exception.message)
        with self.assertRaises(TaskError):
            self.intent(allowed_productions=[" "])

    def test_a_refused_create_writes_nothing(self) -> None:
        """The check sits with the ownership check: before the request is claimed or a row exists."""
        self.writer.transactions = mock.Mock()
        self.writer.transactions.existing.return_value = (None, None)
        with (
            mock.patch.object(self.writer, "_check_ownership"),
            mock.patch.object(self.writer, "_check_conflicts"),
            mock.patch.object(self.writer, "_begin_create") as begin,
        ):
            for options in ({"po_session": "s-closed"}, {"allowed_productions": ["elsewhere"]}):
                with self.subTest(options=options), self.assertRaises(TaskError):
                    self.writer._create_under_admission("r-1", self.intent(**options))
        begin.assert_not_called()
        self.assertEqual(self.client.call.call_args_list, [])

    def test_both_are_inputs_of_the_request_and_an_older_intent_still_replays(self) -> None:
        plain = self.intent()
        chosen = self.intent(po_session="s-open", allowed_productions=["ummanu"])
        self.assertNotIn("po_session", plain.to_document())
        self.assertNotIn("allowed_productions", plain.to_document())
        self.assertEqual(chosen.to_document()["po_session"], "s-open")
        self.assertEqual(chosen.to_document()["allowed_productions"], ["ummanu"])
        self.assertNotEqual(plain.to_document(), chosen.to_document())
        self.assertEqual(SprintCreateIntent.from_document(chosen.to_document()), chosen)
        self.assertEqual(self.writer._create_values(chosen)[ALLOWED_PRODUCTIONS_FIELD], '["ummanu"]')
        self.assertEqual(self.writer._create_values(chosen)[PO_SESSION_FIELD], "s-open")


class ReadTests(unittest.TestCase):
    def normalize(self, meta: dict[str, str]) -> dict[str, Any]:
        return SprintReader(mock.MagicMock())._normalize(
            {"id": 7, "reference": "sprint:7"}, meta, comments=[], include_resume_freshness=False
        )

    def test_a_sprint_opened_before_0016_reads_null_and_empty(self) -> None:
        sprint = self.normalize(PRE_0016_META)
        self.assertEqual((sprint["po_session"], sprint["allowed_productions"]), (None, []))
        status = SprintReader(mock.MagicMock())._status({**sprint, "resume_freshness": {}}, None)
        self.assertEqual((status["po_session"], status["allowed_productions"]), (None, []))

    def test_show_status_and_the_protocol_documents_carry_both(self) -> None:
        sprint = self.normalize(
            {**PRE_0016_META, PO_SESSION_FIELD: "s-1", ALLOWED_PRODUCTIONS_FIELD: '["ummanu","site"]'}
        )
        self.assertEqual(
            (sprint["po_session"], sprint["allowed_productions"]), ("s-1", ["ummanu", "site"])
        )
        status = SprintReader(mock.MagicMock())._status({**sprint, "resume_freshness": {}}, None)
        self.assertEqual(
            (status["po_session"], status["allowed_productions"]), ("s-1", ["ummanu", "site"])
        )
        value = _sprint_value(sprint)
        self.assertEqual((value["po_session"], value["allowed_productions"]), ("s-1", ["ummanu", "site"]))
        identity = _identity(sprint, status)
        self.assertEqual(
            (identity["po_session"], identity["allowed_productions"]), ("s-1", ["ummanu", "site"])
        )

    def test_the_observer_document_names_both(self) -> None:
        sprint = self.normalize(
            {**PRE_0016_META, PO_SESSION_FIELD: "s-1", ALLOWED_PRODUCTIONS_FIELD: '["ummanu"]'}
        )
        document = render_observer_prompt(sprint)
        self.assertIn("## PO session\n\ns-1\n", document)
        self.assertIn("## Allowed productions\n\n- ummanu\n", document)
        older = render_observer_prompt(self.normalize(PRE_0016_META))
        self.assertIn("## PO session\n\n(none recorded)\n", older)
        self.assertIn(
            "## Allowed productions\n\n- (none: this sprint's operations may touch no production)\n", older
        )


class SqlAdapterTests(unittest.TestCase):
    """The `sprints` columns behind the metadata: a pre-0016 row, and the two written at create."""

    class Client:
        def __init__(self, po_session: str | None, productions: list[str]) -> None:
            self.row = (
                7,
                "sprint:7",
                "g",
                "d",
                None,
                "open",
                {"kind": "none"},
                None,
                None,
                None,
                None,
                po_session,
                productions,
                # `e2e_budget` and `e2e_used`, as 0023 gave every sprint (secretary-1796).
                3,
                0,
                [],  # creation-only local-run exceptions (0026)
                [],  # quoted standing owner decisions (0027)
            )
            self.executed: list[tuple[str, tuple]] = []

        def _staged(self, _kind: str) -> dict:
            return {}

        def _query(self, sql: str, params: tuple = ()) -> list:
            if sql.startswith("SELECT board_key, ref, goal"):
                return [self.row]
            return []

        def _execute(self, sql: str, params: tuple = ()) -> None:
            self.executed.append((sql, params))

    def test_a_pre_0016_row_loads_without_either_field(self) -> None:
        # After 0016 an existing row holds NULL and the column default, '{}'.
        meta = SqlSprintRecords(self.Client(None, [])).metadata(7)
        self.assertNotIn(PO_SESSION_FIELD, meta)
        self.assertNotIn(ALLOWED_PRODUCTIONS_FIELD, meta)
        sprint = ReadTests.normalize(ReadTests(), {**meta, "sprint_budget": '{"by_type":{}}'})
        self.assertEqual((sprint["po_session"], sprint["allowed_productions"]), (None, []))

    def test_a_row_with_both_reads_them_back(self) -> None:
        meta = SqlSprintRecords(self.Client("s-1", ["ummanu"])).metadata(7)
        self.assertEqual(meta[PO_SESSION_FIELD], "s-1")
        self.assertEqual(json.loads(meta[ALLOWED_PRODUCTIONS_FIELD]), ["ummanu"])

    def test_create_inserts_both_and_an_update_writes_them(self) -> None:
        client = self.Client(None, [])
        records = SqlSprintRecords(client)
        with mock.patch.object(SqlSprintRecords, "_replace_relations"):
            records._apply("sprint:7", {PO_SESSION_FIELD: "s-2", ALLOWED_PRODUCTIONS_FIELD: '["site"]'})
        [(sql, params)] = client.executed
        self.assertIn("po_session = %s", sql)
        self.assertIn("allowed_productions = %s::text[]", sql)
        self.assertEqual(params[:2], ("s-2", ["site"]))

        staged: dict = {
            5: {
                "reference": "sprint:5",
                "title": "g",
                "created_at": None,
                "metadata": {
                    "sprint_goal": "g",
                    "sprint_definition_of_done": "",
                    "sprint_status": "open",
                    PO_SESSION_FIELD: "s-3",
                    ALLOWED_PRODUCTIONS_FIELD: '["ummanu"]',
                },
            }
        }
        client = self.Client(None, [])
        client._staged = lambda _kind: staged  # type: ignore[method-assign]
        with mock.patch.object(SqlSprintRecords, "_replace_relations"):
            SqlSprintRecords(client)._finish_staged(5)
        [(sql, params)] = client.executed
        self.assertIn("po_session, allowed_productions", sql)
        self.assertIn("s-3", params)
        self.assertIn(["ummanu"], params)


class CheckpointTests(unittest.TestCase):
    def test_export_and_restore_carry_both_only_where_set(self) -> None:
        base = {"ref": "sprint:7", "goal": "g", "status": "open", "budget": {"by_type": {}}, "audit": {}}
        older = normalize_sprint_entity({**base, "po_session": None, "allowed_productions": []})
        self.assertNotIn("po_session", older)
        self.assertNotIn("allowed_productions", older)
        self.assertNotIn(PO_SESSION_FIELD, _restore_sprint_metadata(older))

        record = normalize_sprint_entity({**base, "po_session": "s-1", "allowed_productions": ["ummanu"]})
        self.assertEqual((record["po_session"], record["allowed_productions"]), ("s-1", ["ummanu"]))
        values = _restore_sprint_metadata(record)
        self.assertEqual(
            (values[PO_SESSION_FIELD], values[ALLOWED_PRODUCTIONS_FIELD]), ("s-1", '["ummanu"]')
        )
        self.assertNotEqual(_sprint_core(record), _sprint_core(older))


if __name__ == "__main__":
    unittest.main()
