from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support.completion_receipt import SHA, TREE, declared_receipt, save_receipt
from ummanu.broad_check import BroadCheckError, ContentIdentity
from ummanu.check_commands import completion_check
from ummanu.task_commands import resolve_data_dir
from ummanu.tasks import TaskError, TaskWriter


def _args(**overrides) -> argparse.Namespace:
    values = {"data_dir": None, "instance": None}
    values.update(overrides)
    return argparse.Namespace(**values)


class ResolveDataDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance_dir = Path(self.tmp.name) / "secretary-instance"
        self.instance_dir.mkdir()

    def write_instance(self, data_dir: str) -> Path:
        path = self.instance_dir / "instance.yaml"
        path.write_text(
            "version: 1\n"
            "name: test\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        return path

    def test_explicit_data_dir_wins(self) -> None:
        self.write_instance("/var/lib/ummanu-data")
        args = _args(data_dir="/elsewhere/data", instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), "/elsewhere/data")

    def test_absolute_data_dir_from_instance(self) -> None:
        self.write_instance("/var/lib/ummanu-data")
        args = _args(instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), "/var/lib/ummanu-data")

    def test_relative_instance_data_dir_pins_to_instance_not_cwd(self) -> None:
        self.write_instance("ummanu-data")
        args = _args(instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), str(self.instance_dir / "ummanu-data"))

    def test_workspace_cwd_never_becomes_the_data_dir(self) -> None:
        self.write_instance("/var/lib/ummanu-data")
        workspace = Path(self.tmp.name) / "workspace"
        workspace.mkdir()
        cwd = os.getcwd()
        os.chdir(workspace)
        self.addCleanup(os.chdir, cwd)
        args = _args(instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), "/var/lib/ummanu-data")
        self.assertFalse((workspace / "ummanu-data").exists())

    def test_instance_file_path_is_accepted(self) -> None:
        instance_file = self.write_instance("/var/lib/ummanu-data")
        args = _args(instance=str(instance_file))
        self.assertEqual(resolve_data_dir(args), "/var/lib/ummanu-data")

    def test_missing_instance_is_a_usage_error(self) -> None:
        args = _args(instance=str(self.instance_dir / "absent"))
        with self.assertRaises(TaskError) as caught:
            resolve_data_dir(args)
        self.assertEqual(caught.exception.code, "usage")
        self.assertIn("--data-dir", caught.exception.message)

    def test_instance_without_data_dir_is_a_usage_error(self) -> None:
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        args = _args(instance=str(self.instance_dir))
        with self.assertRaises(TaskError) as caught:
            resolve_data_dir(args)
        self.assertIn("data_dir", caught.exception.message)

    def test_env_data_dir_is_read_at_parse_time(self) -> None:
        from ummanu.cli import build_parser

        with mock.patch.dict(os.environ, {"UMMANU_DATA_DIR": "/env/data"}):
            parser = build_parser()
            args = parser.parse_args(["task", "report", "--ref", "x-1", "--role", "worker", "--kind", "done"])
        self.assertEqual(resolve_data_dir(args), "/env/data")


class CompletionAdmissionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "candidate"
        self.instance = Path(scratch.name) / "instance"
        self.spec, self.path, self.receipt = declared_receipt(self.root, self.instance)
        self.enterContext(mock.patch("ummanu.check_commands._same_repository", return_value=True))
        self.head = self.enterContext(mock.patch("ummanu.check_commands.subprocess.run", return_value=
                                     SimpleNamespace(returncode=0, stdout=f"{SHA}\n{TREE}\n")))
        self.enterContext(mock.patch("ummanu.broad_check.content_identity", return_value=ContentIdentity(TREE)))
        self.writer = object.__new__(TaskWriter)
        self.writer.instance_dir = self.instance
        self.writer.workspace = self.root
        self.writer._role = lambda role, *args, **kwargs: role
        self.writer._redact_for_board = lambda body: body
        self.writer._require_committed_workspace = mock.Mock()
        self.writer.board_host = SimpleNamespace(canon=SimpleNamespace(event=lambda request: None))
        self.task = {"project": "other", "type": "code", "description": "Deliver candidate"}
        self.writer.reader = SimpleNamespace(show=lambda ref: self.task)
        self.writer.audit = SimpleNamespace(events=lambda ref: [])
        self.writer._marker_write = mock.Mock(return_value={"accepted": True})

    def report(self, **overrides):
        args = {"role": "worker", "actor": "worker", "reference": "other-1", "kind": "done", "body": "ready", "request_id": "report-1"}
        return self.writer.report(**{**args, **overrides})

    def test_other_project_native_and_pytest_complete_green_admission(self):
        for module in ("shared", "pytest", "tests.broad"):
            with self.subTest(module=module):
                self.spec, self.path, self.receipt = declared_receipt(self.root, self.instance, module=module)
                self.report()
                data = self.writer._marker_write.call_args.kwargs["data"]["worker_check"]
                self.assertEqual(data["candidate_sha"], SHA)
                self.assertEqual(data["tree_sha"], TREE)
                self.assertEqual(data["receipt_digest"], self.receipt["receipt_digest"])
                self.assertEqual(data["receipt"]["check_set"], self.spec.check_set)

    def test_registration_identity_does_not_depend_on_binding_filename(self):
        (self.instance / "projects/other.yaml").rename(self.instance / "projects/registered.yaml")
        self.report()
        self.assertEqual(self.writer._marker_write.call_args.kwargs["data"]["worker_check"]["candidate_sha"], SHA)

    def test_missing_tampered_incomplete_red_stale_and_wrong_profile_refuse_before_marker(self):
        import copy
        import json

        original = copy.deepcopy(self.receipt)
        cases = ("missing", "tampered", "incomplete", "red", "stale", "profile", "interpreter", "checkout", "subset")
        for case in cases:
            receipt = copy.deepcopy(original)
            if case == "incomplete":
                receipt.update(status="incomplete", incomplete_reason="interrupted", verdict="unknown")
            elif case == "red":
                receipt.update(exit_code=1, verdict="failed")
            elif case == "stale":
                receipt["content_identity"]["tree_sha"] = "c" * 40
            elif case in {"profile", "subset"}:
                receipt["check_set"]["args"] = ["--", "one"]
                from ummanu.broad_check import check_set_digest
                receipt["command_or_check_set_digest"] = check_set_digest(receipt["check_set"])
            elif case == "interpreter":
                receipt["project_provenance"]["python"] = "/different/python"
            elif case == "checkout":
                receipt["project_provenance"]["cwd"] = "/foreign"
            save_receipt(self.path, receipt)
            if case == "missing":
                self.path.unlink()
            elif case == "tampered":
                receipt["exit_code"] = 1
                self.path.write_text(json.dumps(receipt))
            with self.subTest(case=case), self.assertRaises(TaskError) as caught:
                self.report(body=f"claimed SHA {SHA} and green hash")
            self.assertEqual(caught.exception.code, "done_receipt_required")
            self.assertIn("worker wrapper:", caught.exception.message)
            self.writer._marker_write.assert_not_called()

    def test_receipt_must_cover_committed_head_and_registered_checkout(self):
        self.head.return_value.stdout = f"{SHA}\n{'c' * 40}\n"
        with self.assertRaisesRegex(BroadCheckError, "committed HEAD tree"):
            completion_check("other", self.root, self.instance)
        with (mock.patch("ummanu.check_commands._same_repository", return_value=False),
              self.assertRaisesRegex(BroadCheckError, "registered project's checkout")):
            completion_check("other", self.root, self.instance)
        self.writer._marker_write.assert_not_called()

    def test_dispatcher_uses_real_admission_and_immutable_round_binding(self):
        from ummanu.dispatch.host import CommandHostRuntime
        from ummanu.dispatch.state import attempt_request_id
        from ummanu.dispatch.types import HostError

        host = object.__new__(CommandHostRuntime)
        host.mode = "real"
        host.catalog = SimpleNamespace(instance_dir=self.instance)
        host._run = mock.Mock(return_value=SimpleNamespace(stdout=""))
        record = SimpleNamespace(workspace=str(self.root), attempt_id="attempt", report_generation=1)
        task = {**self.task, "ref": "other-1"}
        evidence = completion_check("other", self.root, self.instance)
        events = [{"request_id": attempt_request_id("attempt", "worker-report-done", "other-1", "1"),
                   "record_type": "board.protocol_event", "data": {"marker": "report:done", "worker_check": evidence}}]
        host.audit = SimpleNamespace(events=lambda ref: events)
        host.verify_worker_result(task, record)
        events[0]["data"].pop("worker_check")
        with self.assertRaisesRegex(HostError, "drain pre-policy reports"):
            host.verify_worker_result(task, record)
        events[0]["data"]["worker_check"] = {**evidence, "candidate_sha": "c" * 40}
        with self.assertRaisesRegex(HostError, "immutable accepted report"):
            host.verify_worker_result(task, record)
        self.path.unlink()
        with self.assertRaisesRegex(HostError, "evidence unavailable"):
            host.verify_worker_result(task, record)

    def test_validate_keeps_admitted_snapshot_after_machinery_base_refresh_and_artifact_loss(self):
        from ummanu.dispatch.host import CommandHostRuntime
        from ummanu.dispatch.state import attempt_request_id
        from ummanu.dispatch.types import HostError

        evidence = completion_check("other", self.root, self.instance)
        host = object.__new__(CommandHostRuntime)
        host.mode = "real"
        host.catalog = SimpleNamespace(adapter=lambda project: {"broad_check": {}})
        host._run = mock.Mock(return_value=SimpleNamespace(stdout=TREE))
        record = SimpleNamespace(workspace=str(self.root), attempt_id="attempt", report_generation=1)
        events = [{"request_id": attempt_request_id("attempt", "worker-report-done", "other-1", "1"),
                   "record_type": "board.protocol_event", "data": {"marker": "report:done", "worker_check": evidence}}]
        host.audit = SimpleNamespace(events=lambda ref: events)
        self.path.unlink()
        host._require_worker_admission({**self.task, "ref": "other-1"}, record)
        self.assertEqual(host._run.call_args.args[0][-4:], ["merge-base", "--is-ancestor", SHA, "HEAD"])
        evidence["receipt"]["exit_code"] = 1
        with self.assertRaisesRegex(HostError, "missing or damaged"):
            host._require_worker_admission({**self.task, "ref": "other-1"}, record)

    def test_blocked_non_candidate_and_replay_do_not_require_receipt(self):
        self.path.unlink()
        self.assertEqual(self.report(kind="blocked", classification="external_fact"), {"accepted": True})
        self.task["type"] = "infra"
        self.report(body="## What was done\nWorked\n## How to verify\nInspect\n")
        self.task["type"] = "code"
        evidence = {"candidate_sha": SHA, "receipt_digest": "immutable"}
        self.writer.board_host.canon.event = lambda request: SimpleNamespace(data={"worker_check": evidence})
        self.writer.reader.show = mock.Mock(side_effect=AssertionError("replay read card"))
        self.writer._require_committed_workspace.side_effect = AssertionError("replay read checkout")
        self.report()
        self.assertEqual(self.writer._marker_write.call_args.kwargs["data"]["worker_check"], evidence)


if __name__ == "__main__":
    unittest.main()
