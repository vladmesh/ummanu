"""In-process board transport for card writers and dispatcher snapshot checks."""

from __future__ import annotations

import contextlib
import copy
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

from ummanu.board.backend import record_key
from ummanu.board.e2e_record import AfterMergeMark, E2eRun, E2eState
from ummanu.board.sql_cards import BOARD_COLUMNS, BOARD_ID, SPRINT_BOARD_ID, SqlCardClient
from ummanu.board.wait_card import build_wait_spec
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
