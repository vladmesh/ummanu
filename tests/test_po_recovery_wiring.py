"""The recovery-side PO restore: identity crossing, notes publication, legacy absence and doctor findings."""

import getpass
import json
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes.installation import _checkpoint
from tests.po_checkpoint_fixtures import po_records, write_notes, write_po
from tests.po_fake_store import FakeBoard, FakePoStore
from ummanu import _proc
from ummanu._fsutil import write_json
from ummanu.board.po_checkpoint import LEGACY_PO_COVERAGE, PoCheckpointError
from ummanu.board.po_notes_checkpoint import (
    ABSENT_NOTES_COVERAGE,
    LEGACY_NOTES_COVERAGE,
    RESTORED_NOTES_COVERAGE,
)
from ummanu.data import init_layout
from ummanu.installation.materialize import materialize_checkpoint
from ummanu.installation.models import InstallError
from ummanu.po.recovery_admission import PoRecoveryAdmission
from ummanu.po.workspace import NOTES_FILE, NOTES_SEED, workspace_dir
from ummanu.restore import RestoreError, restore_findings, restore_po, restore_state

NOTES = b"# Decisions\r\nkeep option B \xd1\x8f"


class _ClosingStore(FakePoStore):
    def close(self):
        return None


def _failing_callback():
    raise ValueError("callback failure")


class RunAsUserTests(unittest.TestCase):
    def test_unprivileged_caller_runs_the_callback_in_this_process(self):
        with mock.patch("os.geteuid", return_value=1000), mock.patch("os.fork") as fork:
            self.assertEqual(_proc.run_as_user("runtime", os.getpid), os.getpid())
        fork.assert_not_called()

    def test_root_runs_the_callback_in_a_child_after_dropping_to_the_account(self):
        account = pwd.getpwnam(getpass.getuser())
        with (
            mock.patch("os.geteuid", return_value=0),
            mock.patch("os.initgroups"),
            mock.patch("os.setgid"),
            mock.patch("os.setuid") as setuid,
        ):
            answer = _proc.run_as_user(account.pw_name, lambda: {
                "pid": os.getpid(), "uid": setuid.call_args.args[0] if setuid.called else None,
            })
            with self.assertRaisesRegex(_proc.RuntimeUserError, "^callback failure$"):
                _proc.run_as_user(account.pw_name, _failing_callback)
        self.assertNotEqual(answer["pid"], os.getpid())
        self.assertEqual(answer["uid"], account.pw_uid)

    def test_root_refuses_an_unknown_account_without_running_the_callback(self):
        called = []
        with mock.patch("os.geteuid", return_value=0), \
                self.assertRaisesRegex(_proc.RuntimeUserError, "does not exist"):
            _proc.run_as_user("no-such-ummanu-account", lambda: called.append(1))
        self.assertEqual(called, [])


class RestorePoBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.data = self.root / "data"
        init_layout(self.data)

    def artifacts(self):
        return sorted(path.name for path in self.data.iterdir() if path.name.startswith(".po-"))

    def test_legacy_checkpoint_stays_unavailable_without_credentials_or_recovery_artifacts(self):
        write_json(self.data / "board" / "export.json", {"version": 1})
        with mock.patch("ummanu.board.store.resolve_role", side_effect=AssertionError("credentials read")):
            for _ in range(2):
                self.assertFalse(restore_po(self.data, source=self.data / "board", instance=self.root / "instance"))
        state = restore_state(self.data)
        self.assertEqual(
            (state["po_sql_coverage"], state["po_sql_restore"], state["po_sql_diagnostic"]),
            ("unavailable", "unavailable", LEGACY_PO_COVERAGE),
        )
        self.assertEqual(
            (state["po_notes_coverage"], state["po_notes_parity"], state["po_notes_diagnostic"]),
            ("unavailable", "unavailable", LEGACY_NOTES_COVERAGE),
        )
        self.assertEqual(self.artifacts(), [])
        self.assertFalse(workspace_dir(self.data).exists())

    def test_missing_export_is_named_before_any_coverage_reader(self):
        with mock.patch("ummanu.restore.read_checkpoint_po", side_effect=AssertionError("PO reader")), \
                self.assertRaisesRegex(RestoreError, "missing board/export.json"):
            restore_po(self.data, source=self.data / "board", instance=self.root / "instance")
        self.assertEqual(restore_state(self.data), {})
        self.assertEqual(self.artifacts(), [])

    def test_materialization_names_a_missing_export_before_the_outbox_reader(self):
        instance = self.root / "instance"
        _checkpoint(instance, self.root / "target")
        (instance / "state" / "board" / "export.json").unlink()
        with self.assertRaisesRegex(InstallError, "missing state/board/export.json"):
            materialize_checkpoint(instance, self.root / "target")

    def test_admission_holds_before_the_board_publishes_and_a_refused_begin_publishes_nothing(self):
        write_json(self.data / "board" / "export.json", {"version": 1})
        write_po(self.data / "board", po_records())
        board = FakeBoard()
        published = []

        def publish():
            published.append(PoRecoveryAdmission(self.data, FakePoStore(board)).status())
            raise RuntimeError("board import interrupted after its commit")

        with (
            mock.patch("ummanu.board.store.resolve_role", return_value=object()),
            mock.patch("ummanu.po.store.PoStore", side_effect=lambda _credentials: _ClosingStore(board)),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted after its commit"):
                restore_po(self.data, source=self.data / "board", instance=self.root / "instance", board=publish)
            self.assertEqual([status["reason"] for status in published], ["recovery_hold"])
            self.assertEqual(restore_state(self.data)["po_sql_restore"], "holding")
            busy = self.root / "busy"
            init_layout(busy)
            write_json(busy / "board" / "export.json", {"version": 1})
            write_po(busy / "board", po_records())
            running = FakePoStore(FakeBoard())
            session = running.claim_session(session_id="live", cli="codex", model="model", cwd=str(busy),
                                            cli_session_id=None, request_id=None, effort="high")[0]
            running.claim_turn(session.session_id, "live input", lambda seq: busy / str(seq))
            with (
                mock.patch("ummanu.po.store.PoStore", side_effect=lambda _credentials: _ClosingStore(running.board)),
                self.assertRaisesRegex(RestoreError, "failed at admission: .*turns are running"),
            ):
                restore_po(busy, source=busy / "board", instance=self.root / "instance", board=publish)
        self.assertEqual(len(published), 1)
        self.assertEqual(restore_state(busy)["po_sql_restore"], "captured")
        self.assertFalse((busy / ".po-recovery-admission.json").exists())

    def notes(self) -> Path:
        return workspace_dir(self.data) / NOTES_FILE

    def test_captured_notes_publish_before_the_po_sql_phase_and_a_conflict_keeps_admission_held(self):
        write_json(self.data / "board" / "export.json", {"version": 1})
        write_po(self.data / "board", po_records())
        write_notes(self.data / "board", NOTES)
        board = FakeBoard()
        self.notes().parent.mkdir()
        self.notes().write_text(NOTES_SEED, encoding="utf-8")
        at_plan = []

        def plan(*_args, **_kwargs):
            at_plan.append(self.notes().read_bytes())
            raise PoCheckpointError("stopped at the plan")

        with (
            mock.patch("ummanu.board.store.resolve_role", return_value=object()),
            mock.patch("ummanu.po.store.PoStore", side_effect=lambda _credentials: _ClosingStore(board)),
            mock.patch("ummanu.board.po_restore.prepare_po_restore", side_effect=plan),
        ):
            with self.assertRaisesRegex(RestoreError, "PO notes restore failed: PO notes target conflict"):
                restore_po(self.data, source=self.data / "board", instance=self.root / "instance")
            self.assertEqual(self.notes().read_text(encoding="utf-8"), NOTES_SEED)
            state = restore_state(self.data)
            self.assertEqual((state["po_notes_coverage"], state["po_notes_parity"]), ("captured", "failed"))
            self.assertIn("PO notes restore parity is incomplete", restore_findings(self.data))
            self.assertEqual(PoRecoveryAdmission(self.data, FakePoStore(board)).status()["reason"], "recovery_hold")
            self.assertEqual(at_plan, [])
            self.notes().unlink()
            for _ in range(2):
                with self.assertRaisesRegex(RestoreError, "failed at plan: stopped at the plan"):
                    restore_po(self.data, source=self.data / "board", instance=self.root / "instance")
        self.assertEqual(at_plan, [NOTES, NOTES])
        self.assertEqual(self.notes().stat().st_mode & 0o777, 0o600)
        state = restore_state(self.data)
        self.assertEqual(
            (state["po_notes_coverage"], state["po_notes_parity"], state["po_notes_diagnostic"]),
            ("captured", "complete", RESTORED_NOTES_COVERAGE),
        )
        self.assertNotIn("PO notes restore parity is incomplete", restore_findings(self.data))
        self.assertEqual(PoRecoveryAdmission(self.data, FakePoStore(board)).status()["reason"], "recovery_hold")

    def test_captured_absence_leaves_an_existing_notes_file_and_claims_no_parity(self):
        write_json(self.data / "board" / "export.json", {"version": 1})
        write_notes(self.data / "board", None)
        self.notes().parent.mkdir()
        self.notes().write_text(NOTES_SEED, encoding="utf-8")
        for _ in range(2):
            self.assertFalse(restore_po(self.data, source=self.data / "board", instance=self.root / "instance"))
        self.assertEqual(self.notes().read_text(encoding="utf-8"), NOTES_SEED)
        state = restore_state(self.data)
        self.assertEqual(
            (state["po_notes_coverage"], state["po_notes_parity"], state["po_notes_diagnostic"]),
            ("absent", "unavailable", ABSENT_NOTES_COVERAGE),
        )

    def test_a_damaged_declared_notes_capture_is_refused_before_admission_or_any_write(self):
        write_json(self.data / "board" / "export.json", {"version": 1})
        write_po(self.data / "board", po_records())
        write_notes(self.data / "board", NOTES)
        summary = json.loads((self.data / "board" / "export.json").read_text())
        summary["po_notes"]["sha256"] = "0" * 64
        write_json(self.data / "board" / "export.json", summary)
        board = mock.Mock()
        with (
            mock.patch("ummanu.board.store.resolve_role", side_effect=AssertionError("credentials read")),
            self.assertRaisesRegex(RestoreError, "PO notes size/digest mismatch"),
        ):
            restore_po(self.data, source=self.data / "board", instance=self.root / "instance", board=board)
        board.assert_not_called()
        self.assertEqual(restore_state(self.data), {})
        self.assertEqual(self.artifacts(), [])
        self.assertFalse(workspace_dir(self.data).exists())

    def test_doctor_names_an_unfinished_po_phase_and_not_a_finished_or_legacy_one(self):
        complete = {"board": "complete", "sprints": "complete", "memory_index": "complete",
                    "reconcile": "complete"}
        for po, finding in (
            ({"po_sql_coverage": "captured", "po_sql_restore": "imported"}, ["PO SQL restore is incomplete at imported"]),
            ({"po_sql_coverage": "captured", "po_sql_restore": "finished"}, []),
            ({"po_sql_coverage": "unavailable", "po_sql_restore": "unavailable"}, []),
            ({"po_notes_coverage": "captured", "po_notes_parity": "failed"}, ["PO notes restore parity is incomplete"]),
            ({"po_notes_coverage": "captured", "po_notes_parity": "complete"}, []),
            ({"po_notes_coverage": "absent", "po_notes_parity": "unavailable"}, []),
            ({"po_notes_coverage": "unavailable", "po_notes_parity": "unavailable"}, []),
        ):
            with self.subTest(po=po):
                (self.data / "restore-state.json").write_text(json.dumps({**complete, **po}), encoding="utf-8")
                self.assertEqual(restore_findings(self.data), finding)


if __name__ == "__main__":
    unittest.main()
