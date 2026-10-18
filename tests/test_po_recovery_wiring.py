"""The recovery-side PO restore: identity crossing, legacy absence and its doctor findings."""

import getpass
import json
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes.installation import _checkpoint
from tests.po_checkpoint_fixtures import po_records, write_po
from tests.po_fake_store import FakeBoard, FakePoStore
from ummanu import _proc
from ummanu._fsutil import write_json
from ummanu.board.po_checkpoint import LEGACY_PO_COVERAGE
from ummanu.data import init_layout
from ummanu.installation.materialize import materialize_checkpoint
from ummanu.installation.models import InstallError
from ummanu.po.recovery_admission import PoRecoveryAdmission
from ummanu.restore import RestoreError, restore_findings, restore_po, restore_state


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
        self.assertEqual(self.artifacts(), [])

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

    def test_doctor_names_an_unfinished_po_phase_and_not_a_finished_or_legacy_one(self):
        complete = {"board": "complete", "sprints": "complete", "memory_index": "complete",
                    "reconcile": "complete"}
        for po, finding in (
            ({"po_sql_coverage": "captured", "po_sql_restore": "imported"}, ["PO SQL restore is incomplete at imported"]),
            ({"po_sql_coverage": "captured", "po_sql_restore": "finished"}, []),
            ({"po_sql_coverage": "unavailable", "po_sql_restore": "unavailable"}, []),
        ):
            with self.subTest(po=po):
                (self.data / "restore-state.json").write_text(json.dumps({**complete, **po}), encoding="utf-8")
                self.assertEqual(restore_findings(self.data), finding)


if __name__ == "__main__":
    unittest.main()
