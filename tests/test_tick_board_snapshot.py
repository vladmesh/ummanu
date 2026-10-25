"""A complete production tick over an in-process, counting board transport."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.tick_board import SHA, SPRINT, CountingStore, GuardedWriter
from ummanu.board.backend import record_key
from ummanu.board.e2e_record import AfterMergeMark, E2eState, e2e_state
from ummanu.board.tick_snapshot import current_snapshot, select_cards, select_sprints, tick_snapshot
from ummanu.dispatch import e2e_after_merge, observer, observer_fence, production, wait_cards
from ummanu.infra.checkpoint_run import run_checkpoint
from ummanu.sprints import SprintReader
from ummanu.tasks import TaskError, TaskReader

class TickBoardSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = CountingStore(self.root)
        self.enterContext(mock.patch("ummanu.board.sql_audit.SqlTaskAudit", return_value=self.store.audit))
        self.reader = TaskReader(self.store)
        self.writer = GuardedWriter(self.store, self.root)
        self.runtime = SimpleNamespace(
            owner="unit-test", data_dir=self.root, reader=self.reader, writer=self.writer,
            audit=self.store.audit, sprints=SprintReader(self.store, data_dir=self.root),
            host=SimpleNamespace(head_run_end=lambda run, **facts: None),
            catalog=SimpleNamespace(instance={}, observer_profile=lambda head: {}),
            production_state=production.ProductionState(self.root), save_records=lambda payload, records: None,
            _tick_task=lambda task, *args: {"status": "ok", "ref": task["ref"], "action": "idle"},
        )
        self.payload = {"observers": {SPRINT: observer.ObserverRecord(
            SPRINT, head="test-observer", state="watching", bound=True).to_json()}}
        # Only host/accounting seams are replaced. Every phase's selection and its
        # card/sprint readers, claim admission and sprint budget write run normally.
        for method in ("publish_pending_attempt_usage", "publish_pending_attempt_outcomes"):
            self.enterContext(mock.patch.object(production.attempt_accounting, method, return_value=[]))
        self.enterContext(mock.patch.object(observer_fence, "observer_alive", return_value={"pid_known": True, "alive": True}))
        self.enterContext(mock.patch.object(observer, "_reconcile_open_sprint", side_effect=self.observe_sprint))
        self.enterContext(mock.patch.object(e2e_after_merge, "_start", return_value=None))
        self.enterContext(mock.patch.object(wait_cards, "utcnow", return_value=datetime(2026, 10, 7, tzinfo=UTC)))
        self.dispositions = self.enterContext(mock.patch.object(
            e2e_after_merge, "_reconcile_disposition", wraps=e2e_after_merge._reconcile_disposition))
        self.observed_cards = []

    def observe_sprint(self, runtime, payload, observers, ref, *, sprint, **kwargs):
        # Model an idle observer's status read, including its linked cards.
        self.observed_cards = runtime.sprints.show(sprint["ref"])["cards"]
        return {"status": "ok", "step": "observer-reconcile"}

    def run_tick(self):
        with production.tick_clock():
            return production._production_tick_work(self.runtime, self.payload, {}, {"mode": "running"}, None)

    def assert_budget(self):
        self.assertLessEqual(len(self.store.full_reads) + len(self.store.archive_reads), 3,
                             "full board read budget exceeded; callers:\n" + "\n".join(self.store.full_reads + self.store.archive_reads))

    def test_full_tick_one_read_and_regression_is_caught(self):
        result = self.run_tick()
        self.assertEqual(result["errors"], [])
        self.assert_budget()
        self.assertEqual(len(self.store.full_reads), 1)
        self.assertEqual(len(self.store.sprint_reads), 1)
        self.assertEqual(len(self.store.archive_reads), 1)
        self.assertEqual(self.reader.show("demo-3")["state"], "in_progress")
        self.assertEqual(len(self.dispositions.call_args_list), 1)
        self.assertEqual(self.payload["e2e_after_merge"]["pending"]["pending"][0]["ref"], "demo-4")
        self.assertIn("snapshot", self.payload["tick_telemetry"]["last"]["phases"])
        self.assertIsNone(current_snapshot(self.reader))
        self.store.full_reads.clear()
        real_advance = production._advance_active

        def regression(runtime, records, payload, cards):
            for card in select_cards(runtime.reader):
                runtime.reader.list()
            return real_advance(runtime, records, payload, cards)

        with mock.patch.object(production, "_advance_active", side_effect=regression):
            self.run_tick()
        with self.assertRaisesRegex(AssertionError, "budget exceeded; callers:[\\s\\S]*regression"):
            self.assert_budget()

    def test_independent_checkpoint_is_outside_the_tick_read_budget(self):
        exported = []

        def write():
            exported.extend(self.reader.export())
            self.runtime.sprints.export()
            return SimpleNamespace(to_json=lambda: {"status": "unchanged"})

        self.runtime.checkpoint = SimpleNamespace(write=write)
        result = self.run_tick()
        self.assertEqual(result["errors"], [])
        self.assertNotIn("checkpoint", result)
        self.assertEqual(exported, [])
        self.assert_budget()
        self.assertEqual(len(self.store.full_reads) + len(self.store.archive_reads), 2)
        self.assertEqual(len(self.store.sprint_reads), 1)
        result = run_checkpoint(self.runtime)
        self.assertEqual(result["checkpoint"]["status"], "unchanged")
        self.assertEqual(len(exported), 6)
        self.assertTrue(next(card for card in exported if card["reference"] == "demo-5")["closed"])
        self.assert_budget()
        self.assertEqual(len(self.store.full_reads) + len(self.store.archive_reads), 3)
        self.assertEqual(len(self.store.sprint_reads), 2)

    def test_claim_is_fresh_for_later_phase_and_next_tick_reads_again(self):
        actual_returns = production.reconcile_origin_returns
        seen = []

        def later_phase(runtime):
            seen.extend(select_cards(runtime.reader, states={"ready"}))
            self.assertIn("demo-3", [card["ref"] for card in select_cards(runtime.reader, states={"in_progress"})])
            return actual_returns(runtime)

        with mock.patch.object(production, "reconcile_origin_returns", side_effect=later_phase):
            self.run_tick()
        self.assertNotIn("demo-3", [card["ref"] for card in seen])
        self.assertIn("demo-3", self.store.point_reads)
        self.run_tick()
        self.assertEqual(len(self.store.full_reads), 2)
        self.reader.list()
        self.assertEqual(len(self.store.full_reads), 3, "CLI listings must remain live outside the tick")

    def test_other_process_changes_ready_card_before_claim(self):
        def external_move(reference):
            if reference == "demo-3":
                self.store.rows[3]["column_id"] = 6

        self.store.before_point = external_move
        result = self.run_tick()
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.reader.show("demo-3")["state"], "blocked")
        self.assertEqual(self.store.writes, [])
        self.assert_budget()

    def test_after_merge_release_is_fresh_for_budget(self):
        self.store.meta[2]["review"] = "skipped"

        def release(runtime, payload, records, carrier, dispatch_id):
            # A disposition writes a carrier's new mark before budget observes it.
            state = E2eState(after_merge=AfterMergeMark(SHA, "green"))
            runtime.writer.record_e2e_state(role="dispatcher", actor=runtime.owner,
                                           reference="demo-4", state=state.text())
            runtime.writer.move(role="dispatcher", actor=runtime.owner, reference="demo-2",
                                target="done", reason="after-merge release", request_id="release-code",
                                sprint_override=True, sprint_override_reason="Unit disposition")
            self.store.audit.append("hotfix-create", {
                "request_id": "hotfix-create", "event_id": "hotfix-created", "kind": "created",
                "ref": "demo-4", "actor": {"role": "dispatcher", "id": runtime.owner},
                "outcome": "success", "payload": {"sprint": SPRINT, "budget_event": "hotfix"}})
            return None

        actual_budget = production._reconcile_sprint_budget

        def budget(runtime):
            cards = select_cards(runtime.reader)
            current = next(card for card in cards if card["ref"] == "demo-4")
            self.assertEqual(e2e_state(current).after_merge.state, "green")
            released = next(card for card in cards if card["ref"] == "demo-2")
            self.assertEqual(released["state"], "done")
            return actual_budget(runtime)

        with mock.patch.object(e2e_after_merge, "_reconcile_disposition", side_effect=release), mock.patch.object(
            production, "_reconcile_sprint_budget", side_effect=budget
        ):
            result = self.run_tick()
        self.assertEqual(result["errors"], [])
        self.assertTrue(self.store.audit.committed_event("sprint-budget-hotfix-created"))
        self.assert_budget()
        self.assertEqual(len(self.store.sprint_reads), 1)

    def test_partial_write_and_rollback_invalidate_and_scope_drops_on_error(self):
        with self.assertRaisesRegex(RuntimeError, "tick died"), tick_snapshot(self.reader) as snapshot:
            snapshot.load()
            self.store.call("moveTaskPosition", task_id=3, column_id=3)
            # Model transaction rollback after invalidation. The next selection must see Ready.
            self.store.rows[3]["column_id"] = 2
            self.assertIn("demo-3", [card["ref"] for card in select_cards(self.reader, states={"ready"})])
            raise RuntimeError("tick died")
        self.assertIsNone(current_snapshot(self.reader))

    def test_creation_uses_point_confirmation_and_joins_snapshot(self):
        with tick_snapshot(self.reader) as snapshot:
            snapshot.load()
            reference = self.writer._create_backend(
                project="demo", task_type="code", title="Hotfix", description="Repair failure", target="blocked",
                reference="", blocked_by="", head="", review_head="", slug="", base_branch="", seed_ref="",
                supersedes="", complexity="standard", family_preference="auto", codex_launch_mode="",
                sprint="", review="required", live_impact=False, touches_production="", steward_report=False,
                event={"backend": {}}, request_id="create-hotfix")
            self.assertEqual(reference, "demo-7", "archived references still reserve their number")
            self.assertIn(reference, [card["ref"] for card in select_cards(self.reader, states={"blocked"})])
            self.assertIn("7", self.store.point_reads)
            self.assert_budget()
            self.assertEqual(len(self.store.full_reads), 1)

    def test_sprint_write_is_fresh_in_later_selection(self):
        with tick_snapshot(self.reader) as snapshot:
            snapshot.load()
            self.assertEqual(len(select_sprints(self.runtime.sprints, statuses={"open"})), 1)
            self.store.call("saveTaskMetadata", task_id=record_key("sprint", SPRINT), values={"sprint_status": "stopped"})
            self.assertEqual(select_sprints(self.runtime.sprints, statuses={"open"}), [])
            self.assertEqual(len(self.store.sprint_reads), 1)

    def test_invalid_allocation_writes_no_card_or_pending_event(self):
        original = self.store.call
        for reply in ({"unexpected": "shape"}, None, False, "other-7", "demo-0", "demo-7-tail"):
            with self.subTest(reply=reply), mock.patch.object(
                self.store, "call", side_effect=lambda method, reply=reply, **params: (
                    reply if method == "getNextTaskReference" else original(method, **params)
                )
            ), self.assertRaisesRegex(TaskError, "invalid task reference") as raised:
                self.writer._create_backend(
                    project="demo", task_type="code", title="Hotfix", description="Repair failure", target="blocked",
                    reference="", blocked_by="", head="", review_head="", slug="", base_branch="", seed_ref="",
                    supersedes="", complexity="standard", family_preference="auto", codex_launch_mode="",
                    sprint="", review="required", live_impact=False, touches_production="", steward_report=False,
                    event={"backend": {}}, request_id="invalid-allocation")
            self.assertEqual(raised.exception.code, "backend_error")
            self.assertEqual(set(self.store.rows), {1, 2, 3, 4, 5, 6, record_key("sprint", SPRINT)})
            self.assertEqual(self.store.writes, [])
            self.assertEqual(self.store.audit.log, [])
            self.assertEqual(self.store.audit.pending, {})


class CreateRepairMetadataTests(unittest.TestCase):
    """A pending create whose metadata write was lost is repaired to what a fresh create writes."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "projects").mkdir()
        for project in ("demo", "relay"):
            (self.root / "projects" / f"{project}.yaml").write_text(f"id: {project}\n", encoding="utf-8")
        self.store = CountingStore(self.root)
        self.enterContext(mock.patch("ummanu.board.sql_audit.SqlTaskAudit", return_value=self.store.audit))
        sprint = {"ref": SPRINT, "status": "open", "repositories": ["demo"], "reservations": ["demo"],
                  "dod": {"structured": False}}
        self.enterContext(mock.patch("ummanu.sprints.SprintReader.show", return_value=sprint))
        self.writer = GuardedWriter(self.store, self.root)

    def create(self, request_id, task_type, **fields):
        return self.writer.create(role="po", actor="po", project="demo", task_type=task_type, title="Card",
                                  request_id=request_id, **fields)

    def metadata(self, reference):
        return self.store.meta[next(key for key, row in self.store.rows.items() if row["reference"] == reference)]

    def test_repair_writes_the_whole_metadata_of_a_fresh_create(self):
        # An operation card is cut into a sprint; the PO's code card here is linked to none.
        for task_type, fields in (("operation", {"sprint": SPRINT, "touches_production": "relay"}), ("code", {})):
            with self.subTest(task_type=task_type):
                fresh = self.create(f"fresh-{task_type}", task_type, **fields)["task"]["ref"]
                lost = TaskError("backend_unavailable", "the board is unavailable", 1)
                with mock.patch.object(self.store, "_rpc_saveTaskMetadata", side_effect=lost), \
                        self.assertRaisesRegex(TaskError, "audit repair is required"):
                    self.create(f"lost-{task_type}", task_type, **fields)
                pending = self.store.audit.pending_event(f"lost-{task_type}")
                self.assertEqual(self.metadata(pending["ref"]), {})

                self.writer._finish_pending_cleanup(pending, None)

                self.assertEqual(self.metadata(pending["ref"]), self.metadata(fresh))
                self.assertEqual(self.metadata(fresh)["record_type"], "task")
                self.assertEqual(self.metadata(fresh).get("touches_production"), fields.get("touches_production"))


if __name__ == "__main__":
    unittest.main()
