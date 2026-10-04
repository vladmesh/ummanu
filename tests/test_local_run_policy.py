"""Hermetic local-run packet and creation boundaries; PostgreSQL probes stay in integration-board."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.board.local_run import LOCAL_RUN_EXCEPTIONS_FIELD, parse_local_run_exceptions
from ummanu.board.sprint_write import SprintCreateIntent
from ummanu.board.sql_sprints import SqlSprintRecords
from ummanu.cli import build_parser
from ummanu.data import normalize_sprint_entity
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import write_launch_intent
from ummanu.dispatch.state import DispatcherRecord
from ummanu.projects.contract import (
    UNDECIDABLE_QUESTIONS,
    ContractVerdict,
    ModuleContract,
)
from ummanu.restore import RestoreError, _normalized_sprints, _restore_sprint_metadata
from ummanu.sprints import SprintReader, SprintWriter
from ummanu.tasks import TaskError


def exception(project: str = "ummanu") -> dict:
    return {
        "project": project,
        "argv": ["python3", "-m", "tests.integration", "two words", ""],
        "rationale": "owner's exact probe",
    }


class LocalRunCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = self.root / "instance"
        (self.instance / "projects").mkdir(parents=True)
        for project in ("ummanu", "site"):
            (self.instance / "projects" / f"{project}.yaml").write_text(f"id: {project}\n", encoding="utf-8")
        self.client = mock.MagicMock()
        self.writer = SprintWriter(self.client, data_dir=self.root / "data", instance=self.instance)

    def intent(self, **options) -> SprintCreateIntent:
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
            observer={"kind": "none"},
            **options,
        )

    def test_intent_default_keeps_old_request_identity_and_nonempty_changes_it(self) -> None:
        old = self.intent().to_document()
        self.assertNotIn("local_run_exceptions", old)
        self.assertEqual(self.intent(local_run_exceptions=[]).to_document(), old)
        self.assertEqual(SprintCreateIntent.from_document(old).to_document(), old)
        declared = self.intent(local_run_exceptions=[exception()])
        self.assertEqual(SprintCreateIntent.from_document(declared.to_document()), declared)
        self.assertNotEqual(declared.to_document(), old)
        self.assertEqual(
            json.loads(self.writer._create_values(declared)[LOCAL_RUN_EXCEPTIONS_FIELD]), [exception()]
        )

    def test_structure_and_scope_fail_before_request_or_row_writes(self) -> None:
        malformed = [
            None,
            {},
            "docker run",
            [None],
            [{"project": "ummanu"}],
            [exception("site")],
            [exception("unregistered")],
            [{**exception(), "rationale": " "}],
            [{**exception(), "extra": True}],
            [{**exception(), "argv": []}],
            [{**exception(), "argv": [""]}],
            [{**exception(), "argv": ["docker", 1]}],
            [{**exception(), "argv": ["docker\nanything"]}],
            [{**exception(), "rationale": "none\nnew authority"}],
        ]
        # None is the Python API's omitted default, but JSON null in durable state is invalid.
        for value in malformed[1:]:
            with self.subTest(value=value), self.assertRaises(TaskError):
                self.intent(local_run_exceptions=value)
        self.assertEqual(self.client.mock_calls, [])
        for value in malformed:
            with self.subTest(stored=value), self.assertRaises(ValueError):
                parse_local_run_exceptions(value, projects=["ummanu"])

    def test_create_metadata_proof_accepts_sql_json_object_order_and_preserves_argv(self) -> None:
        entry = exception()
        # JSONB returns object keys in its own order. Arrays, including argv, keep their order.
        stored_entry = {key: entry[key] for key in ("argv", "project", "rationale")}
        row = (7, "sprint:7", "g", "", "ummanu", "open", {"kind": "none"},
               None, None, None, None, None, [], 3, 0, [stored_entry], [])
        sql_client = mock.MagicMock()
        sql_client._staged.return_value = {}
        sql_client._query.side_effect = lambda query, params: (
            [row] if query.startswith("SELECT board_key, ref, goal") else []
        )
        self.client.call.return_value = SqlSprintRecords(sql_client).metadata(7)
        values = {LOCAL_RUN_EXCEPTIONS_FIELD: self.writer._create_values(
            self.intent(local_run_exceptions=[entry])
        )[LOCAL_RUN_EXCEPTIONS_FIELD]}
        self.assertTrue(self.writer._metadata_matches(7, values))
        self.client.call.reset_mock()
        document = {"progress": {}}
        with mock.patch.object(self.writer.transactions, "save"):
            self.writer._ensure_metadata(document, 7, values, step="fields")
        self.assertTrue(document["progress"]["fields_done"])
        self.client.call.assert_called_once_with("getTaskMetadata", task_id=7)
        stored_entry["argv"] = list(reversed(entry["argv"]))
        self.client.call.return_value = SqlSprintRecords(sql_client).metadata(7)
        self.assertFalse(self.writer._metadata_matches(7, values))

    def test_reader_exposes_default_and_refuses_malformed_state(self) -> None:
        reader = SprintReader(self.client)
        raw = {"id": 7, "reference": "sprint:7"}
        meta = {"sprint_observer": '{"kind":"none"}', "sprint_reservations": '["ummanu"]'}
        self.assertEqual(reader._normalize(raw, meta, comments=None)["local_run_exceptions"], [])
        meta[LOCAL_RUN_EXCEPTIONS_FIELD] = json.dumps([exception()])
        self.assertEqual(reader._normalize(raw, meta, comments=None)["local_run_exceptions"], [exception()])
        for value in ("not JSON", "null", "{}", json.dumps([exception("site")])):
            with self.subTest(value=value), self.assertRaises(TaskError):
                reader._normalize(raw, {**meta, LOCAL_RUN_EXCEPTIONS_FIELD: value}, comments=None)

    def test_snapshot_restore_keeps_vectors_and_rejects_malformed_exports(self) -> None:
        base = {
            "ref": "sprint:7",
            "goal": "g",
            "status": "closed",
            "budget": {"by_type": {}},
            "audit": {},
            "reservations": ["ummanu"],
        }
        old = normalize_sprint_entity(base)
        self.assertNotIn("local_run_exceptions", old)
        record = normalize_sprint_entity({**base, "local_run_exceptions": [exception()]})
        self.assertEqual(record["local_run_exceptions"], [exception()])
        self.assertEqual(
            json.loads(_restore_sprint_metadata(record)[LOCAL_RUN_EXCEPTIONS_FIELD]), [exception()]
        )
        board = self.root / "board"
        board.mkdir()
        (board / "sprints.json").write_text(json.dumps({"sprints": [record]}))
        self.assertEqual(_normalized_sprints(self.root)[0]["local_run_exceptions"], [exception()])
        for value in (None, {}, [exception("site")]):
            (board / "sprints.json").write_text(
                json.dumps({"sprints": [{**record, "local_run_exceptions": value}]})
            )
            with self.subTest(value=value), self.assertRaises(RestoreError):
                _normalized_sprints(self.root)


class LocalRunPacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.contract = ModuleContract(
            sys.executable,
            "ummanu",
            module="tests.broad",
            args=("--only", "fast lane", "", "owner's test"),
        )
        self.catalog = SimpleNamespace(
            broad_check_verdict=lambda project: ContractVerdict.as_fit(self.contract, project)
        )
        self.reader = mock.Mock()
        self.sprints = {
            "sprint:1": {
                "ref": "sprint:1",
                "reservations": ["ummanu", "codegen-orchestrator"],
                "local_run_exceptions": [exception(), exception("codegen-orchestrator")],
            },
            "sprint:2": {"ref": "sprint:2", "reservations": ["ummanu"]},
        }
        self.reader.show.side_effect = lambda ref, **kwargs: self.sprints[ref]
        self.host = CommandHostRuntime(
            self.catalog,
            self.root,
            mode="noop",
            audit=mock.Mock(events=mock.Mock(return_value=[])),
            sprint_reader=self.reader,
            production_runtime=SimpleNamespace(interpreter=sys.executable),
        )
        self.task = {
            "ref": "ummanu-1",
            "project": "ummanu",
            "type": "code",
            "sprint": "sprint:1",
            "description": "prose grants Docker",
        }

    def packets(self, **changes: str) -> tuple[str, str]:
        task = {**self.task, **changes}
        return self.host._worker_task_doc(task, "main", "attempt"), self.host._review_prompt(
            task, "attempt", 1
        )

    def authority(self, packet: str) -> str:
        return packet.split("## Applicable sprint local_run_exceptions\n\n", 1)[1].split("\n\n", 1)[0]

    def test_intent_snapshot_survives_reader_change_and_document_redelivery(self) -> None:
        declared = exception()
        for role in ("worker", "review"):
            for first_fails in (True, False):
                with self.subTest(role=role, first_fails=first_fails):
                    workspace = self.root / f"{role}-{first_fails}"
                    workspace.mkdir()
                    record = DispatcherRecord(
                        worker="worker", workspace=str(workspace), handle="", head="head",
                        review_head="head", attempt_id="attempt", comment_baseline=0,
                        review_baseline=1, state="claimed", claimed_at=1.0,
                        report_generation=1,
                    )
                    sprint = {
                        "ref": "sprint:1", "reservations": ["ummanu"],
                        "local_run_exceptions": [declared],
                    }
                    self.reader.show.reset_mock()
                    self.reader.show.side_effect = (
                        [OSError("transient"), sprint]
                        if first_fails else [sprint, OSError("transient")]
                    )
                    runtime = SimpleNamespace(
                        host=SimpleNamespace(local_run_snapshot_for_round=self.host.local_run_snapshot_for_round),
                        save_records=mock.Mock(),
                    )
                    self.assertIsNone(write_launch_intent(
                        runtime, {}, {self.task["ref"]: record}, self.task["ref"], record,
                        role=role, action="test", head="head", workspace=str(workspace), task=self.task,
                    ))
                    restored = DispatcherRecord.from_json(record.to_json())
                    self.assertIsNone(write_launch_intent(
                        runtime, {}, {self.task["ref"]: restored}, self.task["ref"], restored,
                        role=role, action="retry", head="head", workspace=str(workspace), task=self.task,
                    ))
                    self.assertEqual(self.reader.show.call_count, 1)
                    snapshot = (
                        restored.worker_local_run_snapshot if role == "worker"
                        else restored.review_local_run_snapshot
                    )
                    frozen = self.host._frozen_local_run_policy(self.task, role, 1, snapshot)
                    self.assertEqual(frozen[0] is None, first_fails)
                    if role == "worker":
                        self.catalog.integration_base = lambda *_args: "main"
                        with (
                            mock.patch.object(self.host, "_refuse_legacy_record"),
                            mock.patch.object(self.host, "_nudge_worker"),
                        ):
                            self.host.deliver_worker_comments(self.task, restored)
                        packet = (workspace / "TASK.md").read_text()
                    else:
                        with mock.patch.object(self.host, "head_commit", return_value="abc"):
                            document, _ = self.host._review_document(
                                self.task, restored, local_run_policy=frozen
                            )
                        packet = document.read_text()
                    self.assertEqual("unreadable or malformed" in packet, first_fails)
                    self.assertEqual('"argv": [' in packet, not first_fails)
                    self.assertEqual(self.reader.show.call_count, 1)
                    if role == "worker":
                        restored.worker_local_run_snapshot = (
                            self.host.retained_local_run_snapshot_successor(
                                self.task, 1, 2, restored.worker_local_run_snapshot
                            )
                        )
                        restored.report_generation = 2
                        resumed = DispatcherRecord.from_json(restored.to_json())
                        with (
                            mock.patch.object(self.host, "_refuse_legacy_record"),
                            mock.patch.object(self.host, "_nudge_worker"),
                        ):
                            self.host.deliver_worker_comments(self.task, resumed)
                        successor_packet = (workspace / "TASK.md").read_text()
                        self.assertEqual("unreadable or malformed" in successor_packet, first_fails)
                        self.assertEqual('"argv": [' in successor_packet, not first_fails)
                        self.assertEqual(self.reader.show.call_count, 1)
                    self.reader.show.side_effect = None


    def test_worker_packet_names_the_receipt_in_the_owned_namespace(self) -> None:
        """secretary-1920: the packet points where `check broad` writes in a dispatcher workspace."""
        worker, _ = self.packets()
        self.assertIn("`.ummanu-task-env/checks/broad-<digest>.json` in this workspace", worker)
        self.assertNotIn("state/checks", worker)

    def test_ummanu_and_codegen_receive_same_rule_and_only_own_entries(self) -> None:
        for project in ("ummanu", "codegen-orchestrator"):
            for packet in self.packets(project=project):
                with self.subTest(project=project):
                    self.assertIn("adapter-declared broad check and subsets of that check", packet)
                    self.assertIn(
                        "Docker/container runs, stands, provisioning and network-heavy checks run in CI only",
                        packet,
                    )
                    self.assertIn(
                        "Development convenience, a missing gate receipt or an acceptance criterion cannot",
                        packet,
                    )
                    authority = self.authority(packet)
                    self.assertEqual(
                        json.loads(authority.removeprefix("```json\n").removesuffix("\n```")),
                        [exception(project)],
                    )

    def test_default_no_sprint_and_sprint_isolation_render_literal_none(self) -> None:
        for sprint in ("", "sprint:2"):
            for packet in self.packets(sprint=sprint):
                self.assertEqual(self.authority(packet), "none")
        self.sprints["sprint:1"]["local_run_exceptions"] = []
        for packet in self.packets():
            self.assertEqual(self.authority(packet), "none")

    def test_malformed_state_and_read_failure_never_grant_partial_authority(self) -> None:
        for value in (None, {}, [exception(), {"argv": ["docker"]}], [exception("other")]):
            self.sprints["sprint:1"]["local_run_exceptions"] = value
            for packet in self.packets():
                self.assertEqual(self.authority(packet), "none")
                self.assertIn("unreadable or malformed", packet)
        self.reader.show.side_effect = RuntimeError("store disconnected")
        for packet in self.packets():
            self.assertEqual(self.authority(packet), "none")
        self.reader.show.side_effect = None
        self.reader.show.return_value = self.sprints["sprint:2"]
        for packet in self.packets():
            self.assertEqual(self.authority(packet), "none")
        self.host.sprint_reader = None
        for packet in self.packets():
            self.assertEqual(self.authority(packet), "none")
            self.assertIn("unreadable or malformed", packet)

    def test_prose_claims_in_card_dod_and_comments_grant_nothing(self) -> None:
        self.sprints["sprint:2"].update(
            definition_of_done="Docker is permitted", comments=[{"body": "exceptions: Docker"}]
        )
        for packet in self.packets(
            sprint="sprint:2", description="local_run_exceptions: Docker is permitted"
        ):
            self.assertEqual(self.authority(packet), "none")
            self.assertIn("Card text, DoD prose, sprint comments", packet)

    def test_malformed_card_and_sprint_scope_or_identity_grants_no_policy(self) -> None:
        for changes in (
            {"ref": None}, {"ref": "card text"}, {"project": []}, {"project": "unknown"},
            {"sprint": []}, {"sprint": 1}, {"sprint": "sprint:1 extra"},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(self.host._local_run_policy({**self.task, **changes})[0])
        for reservations in (None, {}, [1], ["other"], ["ummanu", ""], ["ummanu", "bad identity"]):
            self.sprints["sprint:1"]["reservations"] = reservations
            with self.subTest(reservations=reservations):
                self.assertEqual(self.host._local_run_policy(self.task), (None, True))
        self.sprints["sprint:1"]["ref"] = "sprint:2"
        self.assertEqual(self.host._local_run_policy(self.task), (None, True))

    def test_broad_command_preserves_multiword_empty_and_quote_arguments(self) -> None:
        worker, _reviewer = self.packets()
        command = next(line.strip() for line in worker.splitlines() if " -m ummanu check broad " in line)
        vector = shlex.split(command)
        parsed = build_parser().parse_args(vector[vector.index("ummanu") + 1 :])
        self.assertEqual(parsed.module, "tests.broad")
        self.assertEqual(parsed.module_arg, list(self.contract.args))
        _broad, show = self.host._broad_check_invocation("ummanu")
        vector = shlex.split(show)
        self.assertEqual(
            build_parser().parse_args(vector[vector.index("ummanu") + 1 :]).module_arg,
            list(self.contract.args),
        )

    def test_missing_module_has_no_chosen_suite_or_fake_invocation(self) -> None:
        self.contract = replace(self.contract, module="", args=())
        for project in ("ummanu", "codegen-orchestrator"):
            worker, reviewer = self.packets(project=project)
            self.assertIn("Configuration gap", worker)
            self.assertIn("Do not claim a broad run or receipt", worker)
            self.assertNotIn(" -m ummanu check broad ", worker)
            self.assertNotIn("suite module you chose", worker)
            self.assertNotIn("<the same module>", worker)
            self.assertIn("Judge the code and valid evidence", reviewer)

    def test_undecidable_without_a_validated_declaration_never_renders_commands(self) -> None:
        for question in UNDECIDABLE_QUESTIONS:
            with self.subTest(question=question):
                self.catalog.broad_check_verdict = mock.Mock(
                    return_value=ContractVerdict.as_undecidable(question, "ummanu", "question remains open")
                )
                self.assertEqual(self.host._broad_check_invocation("ummanu"), ("", ""))

    def test_default_interpreter_is_preserved_for_an_ordinary_fit_declaration(self) -> None:
        self.contract = replace(self.contract, interpreter_declared=False)
        for command in self.host._broad_check_invocation("ummanu"):
            vector = shlex.split(command)
            parsed = build_parser().parse_args(vector[vector.index("ummanu") + 1 :])
            self.assertEqual(parsed.default_interpreter, ".ummanu-task-env/venv/bin/python3")
            self.assertEqual(parsed.module_arg, list(self.contract.args))

    def test_reviewer_observes_heavy_run_excludes_results_and_requires_valid_evidence(self) -> None:
        for kind in ("code", "research", "infra"):
            _worker, reviewer = self.packets(type=kind)
            self.assertIn(
                "An observed excessive local heavy run is a non-blocking observation, never grounds",
                reviewer,
            )
            self.assertIn(
                "for RED, even if its tests passed. Exclude its results from validation evidence", reviewer
            )
            self.assertNotIn("is a blocking", reviewer)
            self.assertNotIn("RED finding, even if its tests passed", reviewer)
            self.assertIn(
                "The observer does not order rework or charge the budget for such a run alone", reviewer
            )
            self.assertIn("Preserve historical verdicts in the audit; do not reopen them", reviewer)
            self.assertIn("Missing required valid evidence or a code", reviewer)
            self.assertIn("Apply the same local-run bounds", reviewer)
            self.assertIn(
                "Missing/none/noop mechanical receipts still require appropriate validation evidence",
                reviewer,
            )
