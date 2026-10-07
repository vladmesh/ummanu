"""A complete production tick over an in-process, counting board transport."""

from __future__ import annotations

import contextlib
import copy
import inspect
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.board.backend import record_key
from ummanu.board.e2e_record import AfterMergeMark, E2eRun, E2eState, e2e_state
from ummanu.board.sql_cards import BOARD_COLUMNS, BOARD_ID, SPRINT_BOARD_ID, SqlCardClient
from ummanu.board.tick_snapshot import current_snapshot, select_cards, select_sprints, tick_snapshot
from ummanu.board.wait_card import build_wait_spec
from ummanu.dispatch import e2e_after_merge, observer, observer_fence, production, wait_cards
from ummanu.sprints import SprintReader
from ummanu.tasks import TaskError, TaskReader, TaskWriter

SPRINT = "sprint:1"
SHA = "a" * 40


class CountingStore(SqlCardClient):
    """Keep SqlCardClient.call's writer boundary, replace its SQL handlers with memory.

    Every full row enumeration records its Python callers; filtered archive reads and
    sprint enumerations are separate counters. No PostgreSQL, subprocess or network.
    """

    def __init__(self, root):
        self.instance_dir = root
        self._local = SimpleNamespace(depth=0)
        self.rows = {}
        self.meta = {}
        self.comments = {}
        self.full_reads = []
        self.sprint_reads = []
        self.archive_reads = []
        self.point_reads = []
        self.writes = []
        self.before_point = None
        self.audit = MemoryAudit(root)
        self.add(1, "in_progress", project="alpha")
        self.add(2, "validate", project="beta")
        self.add(3, "ready", kind="wait")
        self.add(4, "done", project="pending", e2e=E2eState(after_merge=AfterMergeMark(SHA, "pending")))
        run = E2eRun("run-archived", SHA, "org/repo", "main", "e2e.yml", "2026-10-07T00:00:00Z",
                     placement="after_merge", covered=[{"ref": "demo-5", "merge_sha": SHA}],
                     acted=True, resolution="green")
        self.add(5, "done", closed=True, e2e=E2eState(after_merge_runs=[run]))
        self.add(6, "blocked", project="hotfix")
        self.meta[6]["sprint_ref"] = ""
        # The ready wait reaches a future time, so it remains In progress after claiming.
        spec = build_wait_spec(until="2026-10-08T01:00:00Z", deadline="2d",
                               returns=("dependents",), sprint=SPRINT, now=datetime(2026, 10, 7, tzinfo=UTC))
        self.meta[3]["wait"] = spec.text()
        key = record_key("sprint", SPRINT)
        self.rows[key] = {"id": key, "reference": SPRINT, "project_id": SPRINT_BOARD_ID,
                          "title": "Tick budget", "description": "", "column_id": 1, "is_active": 1}
        self.meta[key] = {"sprint_status": "open", "sprint_goal": "Tick budget",
                          "sprint_repositories": '["alpha","beta","demo"]',
                          "sprint_observer": '{"kind":"head","profile":"test-observer"}'}

    def add(self, number, state, *, project="demo", kind="code", closed=False, e2e=None):
        column = next(identifier for identifier, title in BOARD_COLUMNS if title.lower().replace(" ", "_") == state)
        self.rows[number] = {"id": number, "reference": f"demo-{number}", "title": f"Card {number}",
                             "description": "", "project_id": BOARD_ID, "column_id": column,
                             "position": number, "is_active": int(not closed)}
        self.meta[number] = {"project": project, "task_type": kind, "sprint_ref": SPRINT}
        if e2e:
            self.meta[number]["e2e"] = e2e.text()

    def _session(self):
        return contextlib.nullcontext()

    def transaction(self):
        return contextlib.nullcontext()

    def _execute(self, sql, params=()):
        return 1

    def _query(self, sql, params=()):
        # Empty durable owner/origin outboxes and no advisory-lock result.
        if "FROM tasks" in sql:
            raise AssertionError("unmodelled task SQL: " + sql)
        return []

    def call_batch(self, calls):
        return [self.call(method, **params) for method, params in calls]

    def _rpc_getProjectByName(self, *, name):
        return {"id": SPRINT_BOARD_ID if name == "Ummanu sprints" else BOARD_ID, "name": name}

    def _rpc_getColumns(self, *, project_id):
        return [{"id": identifier, "title": title} for identifier, title in BOARD_COLUMNS]

    def _rpc_getActiveSwimlanes(self, *, project_id):
        return []

    def _rpc_getAllTasks(self, *, project_id, status_id=1):
        callers = " -> ".join(frame.function for frame in inspect.stack()[1:12])
        (self.sprint_reads if project_id == SPRINT_BOARD_ID else self.full_reads).append(callers)
        return copy.deepcopy([row for row in self.rows.values()
                              if row["project_id"] == project_id and row["is_active"] == status_id])

    def _rpc_getBoardRows(self, *, project_id):
        self.full_reads.append("checkpoint -> export -> getBoardRows")
        return copy.deepcopy([row for row in self.rows.values() if row["project_id"] == project_id])

    def _rpc_getArchivedAfterMergeTasks(self, *, project_id):
        self.archive_reads.append("archived_after_merge_cards")
        return copy.deepcopy([row for key, row in self.rows.items() if row["project_id"] == project_id
                              and not row["is_active"] and '"after_merge' in self.meta[key].get("e2e", "")])

    def _rpc_getTaskByReference(self, *, project_id, reference):
        self.point_reads.append(reference)
        if self.before_point:
            self.before_point(reference)
        return copy.deepcopy(next((row for row in self.rows.values()
                                   if row["project_id"] == project_id and row["reference"] == reference), None))

    def _rpc_getTaskById(self, *, project_id, task_id):
        self.point_reads.append(str(task_id))
        return copy.deepcopy(self.rows.get(task_id))

    def _rpc_getCapacityReferences(self):
        from ummanu.tasks import ACTIVE_STATES
        active_columns = {identifier for identifier, title in BOARD_COLUMNS
                          if title.lower().replace(" ", "_") in ACTIVE_STATES}
        return [row["reference"] for row in self.rows.values()
                if row["project_id"] == BOARD_ID and row["is_active"]
                and row["column_id"] in active_columns]

    def _rpc_lockOwnershipReference(self, *, reference, observer=False):
        return any(row["reference"] == reference for row in self.rows.values())

    def _rpc_getNextTaskReference(self, *, project):
        numbers = [int(row["reference"].removeprefix(project + "-")) for row in self.rows.values()
                   if row["reference"].startswith(project + "-")]
        return project + "-" + str(max(numbers, default=0) + 1)

    def _rpc_createTask(self, *, project_id, title, description, column_id, reference, **kwargs):
        number = max(key for key in self.rows if key < 2_000_000_000) + 1
        self.rows[number] = {"id": number, "reference": reference, "title": title, "description": description,
                             "project_id": project_id, "column_id": column_id, "is_active": 1, "position": number}
        self.meta[number] = {}
        return number

    def _rpc_getTaskMetadata(self, *, task_id):
        return copy.deepcopy(self.meta[task_id])

    def _rpc_getSprintE2eBudget(self, *, sprint_ref):
        return {"budget": 10, "used": 0, "charges": []}

    def _rpc_getAllComments(self, *, task_id):
        return copy.deepcopy(self.comments.get(task_id, []))

    def _rpc_saveTaskMetadata(self, *, task_id, values):
        self.writes.append(("metadata", task_id))
        self.meta[task_id].update(values)
        return True

    def _rpc_moveTaskPosition(self, *, task_id, column_id, **kwargs):
        self.writes.append(("move", task_id))
        self.rows[task_id]["column_id"] = column_id
        return True

    def _rpc_createComment(self, *, task_id, user_id, content):
        self.comments.setdefault(task_id, []).append({"comment": content, "date_creation": 1})
        return 1


class MemoryAudit:
    def __init__(self, root):
        self.board_dir = root / "board"
        self.log = []
        self.pending = {}

    def committed_event(self, request_id):
        return next((event for event in self.log if event["request_id"] == request_id), None)

    def pending_event(self, request_id):
        return self.pending.get(request_id)

    def stage(self, request_id, event):
        self.pending[request_id] = copy.deepcopy(event)

    def append(self, request_id, event):
        event = copy.deepcopy(event)
        event.setdefault("event_id", "event-" + request_id)
        self.log.append(event)
        self.pending.pop(request_id, None)
        return event["event_id"]

    def events(self, reference="", kind=None, **kwargs):
        return [event for event in self.log if (not reference or event["ref"] == reference)
                and (kind is None or event["kind"] == kind)]

    def uncharged_budget_candidates(self, *, limit):
        return [event for event in self.log if event["kind"] == "created"
                and not self.committed_event("sprint-budget-" + event["event_id"])][:limit]


class GuardedWriter(TaskWriter):
    """Real claim admission and metadata methods; model the typed transition receipt.

    SQL event canon/host state effects are covered by their integration shard. This
    unit double keeps the live transition guard and the production transport writes.
    """

    def __init__(self, client, root):
        self.client = client
        self.reader = TaskReader(client)
        self.data_dir = root
        self.instance_dir = root
        self.workspace = None
        self._redaction_cache = None
        self.audit = client.audit

    def _typed_event(self, request_id):
        return None

    def _transition_card(self, *, reference, target, request_id, finish=None, **kwargs):
        task = self.reader.show(reference)
        if target.value == "in_progress" and task["state"] != "ready":
            raise TaskError("claim_conflict", "claim requires a Ready task", 3)
        number = int(task["id"].rsplit("_", 1)[1])
        column = next(identifier for identifier, title in BOARD_COLUMNS
                      if title.lower().replace(" ", "_") == target.value)
        self.client.call("moveTaskPosition", task_id=number, column_id=column)
        if finish:
            finish(None)
        return SimpleNamespace(event=SimpleNamespace(event_id="event-" + request_id))

    def _card_superseded(self, reference):
        return False


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
            host=SimpleNamespace(), catalog=SimpleNamespace(instance={}, observer_profile=lambda head: {}),
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

    def test_due_checkpoint_stays_within_three_reads(self):
        exported = []

        def write():
            exported.extend(self.reader.export())
            self.runtime.sprints.export()
            return SimpleNamespace(to_json=lambda: {"status": "unchanged"})

        self.runtime.checkpoint = SimpleNamespace(write=write)
        result = self.run_tick()
        self.assertEqual(result["errors"], [])
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


if __name__ == "__main__":
    unittest.main()
