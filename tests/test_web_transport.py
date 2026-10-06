"""The web transport: a status table, a closed route list, cursors over HTTP, and one loopback bind.

Hermetic in the same sense the two layer suites are: no live Orca, no live board, no network beyond a
loopback socket this test binds itself, and no real worker. The board is a throwaway card store
(`tests/sql_backend_fixtures.py`), the head
backend is a fake that leaves behind exactly the artefacts a supervised head leaves, and every
route is driven through :class:`ummanu.web.app.WebApp` directly, which is the same object the
socket handler calls.

What is being pinned is that this transport is a transport: it adds no fact, and every refusal it
returns was decided one layer below and translated in exactly one table.
"""

from __future__ import annotations

import ast
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar
from unittest import mock
from urllib.parse import urlencode

import yaml
from jsonschema import Draft202012Validator

from tests.fakes.tasks import SEED_COLUMN, empty_seed
from tests.sql_backend_fixtures import card_store
from ummanu.config import ConfigError, load_schema
from ummanu.runtime.head.identity import publish_heartbeat
from ummanu.runtime.head.local_pty import RUN_EXITED, RUN_STARTED
from ummanu.runtime.head.run import HeadRun
from ummanu.runtime.head.runtime import (
    HEAD_OK,
    DeliverReceipt,
    ObserveReceipt,
    StartReceipt,
    StopReceipt,
)
from ummanu.runtime.heads import Registry
from ummanu.tasks import task_audit_for
from ummanu.web import pages
from ummanu.web.app import ROUTES, WebApp
from ummanu.web.server import (
    DEFAULT_HOST,
    LoopbackOnly,
    build_server,
    check_bind,
    resolve_bind,
)
from ummanu.web.statuses import HTTP_STATUS_BY_CODE, UNMAPPED_CODE_STATUS, status_for
from ummanu.webproto import errors as error_module
from ummanu.webproto.card_ops import CardOperationLayer
from ummanu.webproto.command_reads import CommandReadLayer
from ummanu.webproto.errors import (
    InstallationUnavailable,
    InvalidCursor,
    OwnerConflict,
    ReadError,
    RunNotFound,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from ummanu.webproto.ops import OperationLayer
from ummanu.webproto.pause_ops import PauseOperationLayer
from ummanu.webproto.pause_reads import PauseReadLayer
from ummanu.webproto.reads import ReadLayer
from ummanu.webproto.sprint_ops import SprintOperationLayer
from ummanu.webproto.sprint_reads import SprintReadLayer

SEED_STATE = {column: state for state, column in SEED_COLUMN.items()}

REPO_ROOT = Path(__file__).resolve().parents[1]

#: How many layers the application is built over; every one is handed in.
LAYERS = 8

WORKER_PROFILE = "codex-product-worker"
REVIEWER_PROFILE = "claude-product-reviewer"
PROFILES = {
    WORKER_PROFILE: {
        "resource": "openai-sub",
        "adapter": "codex",
        "model": "gpt-5.6-terra",
        "effort": "default",
        "runtime": "local-pty",
        "fallback": [],
    },
    REVIEWER_PROFILE: {
        "resource": "claude-sub",
        "adapter": "claude",
        "model": "opus",
        "effort": "high",
        "runtime": "local-pty",
        "fallback": [],
    },
}


def _registry() -> Registry:
    return Registry(
        resources={"openai-sub": {"account": "a"}, "claude-sub": {"account": "b"}},
        profiles={key: dict(value) for key, value in PROFILES.items()},
        role_defaults={},
    )


def _dead_pid() -> int:
    """A pid that names no process, so a heartbeat pointing at it classifies as dead."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip())
    for candidate in range(ceiling - 1, 1, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise unittest.SkipTest("this host has no free pid to prove a dead heartbeat with")


class FakeHeadRuntime:
    """A head backend that leaves a heartbeat and a journal behind, and counts what it raised."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.starts: list[str] = []

    def start(self, spec, workspace, task_ref, *, command, title, pointer=None, **options):
        run_id = options["run_id"]
        self.starts.append(run_id)
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        publish_heartbeat(
            str(run_dir / "head.pid"),
            {"run_id": run_id, "role": options["role"], "task": task_ref.ref},
        )
        with (run_dir / "journal.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(
                json.dumps(
                    {"schema_version": 1, "kind": RUN_STARTED, "seq": 1, "run_id": run_id, "at": 1.0},
                    sort_keys=True,
                )
                + "\n"
            )
        run = HeadRun(
            run_id=run_id,
            spec=spec,
            workspace=workspace,
            task_ref=task_ref,
            role=options["role"],
            handle=str(run_dir / "head.sock"),
            leaf=run_id,
            pid_file=str(run_dir / "head.pid"),
        ).working()
        return StartReceipt(status=HEAD_OK, run=run)

    def observe(self, run):
        return ObserveReceipt(status=HEAD_OK, run=run, evidence={"alive": True}, busy=False)

    def deliver(self, run, pointer, *, subject="", **options):
        return DeliverReceipt(status=HEAD_OK, run=run, delivery_state="complete")

    def stop(self, run, initiator, **options):
        return StopReceipt(status=HEAD_OK, run=run)


class TransportFixture(unittest.TestCase):
    """One instance, one fake board, one fake backend, and the application over both layers."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data_dir = self.tmp / "data"
        for leaf in ("dispatcher", "board", "sprints"):
            (self.data_dir / leaf).mkdir(parents=True)
        self.repo = self._repo()
        self.instance = self._instance()
        # Every test here says which cards exist, so it starts from an empty board.
        self.board = card_store(self, empty_seed(), instance_dir=self.data_dir)
        self._cards = 0
        self.runtime = FakeHeadRuntime(self.data_dir / "webproto" / "heads")
        self._production({})
        self.clock = 1788652800.0

    # -- fixture pieces ------------------------------------------------------------------------

    def _instance(self) -> Path:
        instance_dir = self.tmp / "instance"
        (instance_dir / "projects").mkdir(parents=True)
        (instance_dir / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        (instance_dir / "projects" / "ummanu.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "ummanu",
                    "repo": str(self.repo),
                    "enabled": True,
                    "adapter": "ummanu",
                    "default_branch": "main",
                }
            ),
            encoding="utf-8",
        )
        return instance_dir

    def _repo(self) -> Path:
        repo = self.tmp / "project"
        repo.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.tmp),
        }
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        for argv in (
            ["git", "init", "--initial-branch=main", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "seed"],
        ):
            subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True)
        return repo

    def _production(self, records: dict[str, Any]) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps({"phase": "production", "records": records}), encoding="utf-8"
        )

    def _card(self, reference: str = "ummanu-run-1", column: int = 1) -> int:
        task_id = 40 + self._cards
        self._cards += 1
        self.backlog_key = task_id
        self.board.add_card(
            task_id,
            reference,
            state=SEED_STATE[column],
            title="A small task",
            description="Add a line to README.md.",
            position=1,
            created=1720000000,
            project=None,
            metadata={"project": "ummanu", "task_type": "code", "slug": "run"},
        )
        return task_id

    def _journal(self, records: list[dict[str, Any]]) -> None:
        """Commit records to the card audit (`requests`), in order."""
        audit = task_audit_for(self.board)
        for record in records:
            audit.append(record["request_id"], record)

    def _event(self, ref: str, ordinal: int) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "record_type": "board.protocol_event",
            "request_id": f"req-{ref}-{ordinal}",
            "event_id": f"evt-{ref}-{ordinal}",
            "kind": "card.started",
            "subject": {"kind": "card", "ref": ref},
            "ref": ref,
            "occurred_at": "2026-09-06T00:00:00Z",
            "actor": {"role": "dispatcher", "id": "ummanu-production"},
            "reason": f"event {ordinal}",
            "related_refs": [],
            "data": {},
        }

    # -- the application under test --------------------------------------------------------

    def reads(self, **kwargs) -> ReadLayer:
        options = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "status_reader": lambda: {"schema_version": 1, "summary": {"state": "ok"}},
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return ReadLayer(self.instance, **options)

    def ops(self, **kwargs) -> OperationLayer:
        options = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "registry": _registry(),
            "runtime_factory": lambda _root: self.runtime,
            "clock": lambda: self.clock,
            "settle_seconds": 0.0,
        }
        options.update(kwargs)
        return OperationLayer(self.instance, **options)

    def app(self, **kwargs) -> WebApp:
        return WebApp(
            self.reads(),
            self.ops(**kwargs),
            self.sprint_reads(),
            self.sprint_ops(),
            *self.operator_layers(),
        )

    def operator_layers(self, **kwargs) -> tuple[Any, Any, Any, Any]:
        """The pause, command and card layers, over the same instance, board and clock."""
        options = {"data_dir": self.data_dir, "board_client": self.board, "clock": lambda: self.clock}
        options.update(kwargs)
        return (
            PauseReadLayer(self.instance, **options),
            PauseOperationLayer(self.instance, **options),
            CommandReadLayer(self.instance, **options),
            CardOperationLayer(self.instance, **options),
        )

    def sprint_reads(self, **kwargs) -> SprintReadLayer:
        options = {"data_dir": self.data_dir, "board_client": self.board, "clock": lambda: self.clock}
        options.update(kwargs)
        return SprintReadLayer(self.instance, **options)

    def sprint_ops(self, **kwargs) -> SprintOperationLayer:
        options = {"data_dir": self.data_dir, "board_client": self.board, "clock": lambda: self.clock}
        options.update(kwargs)
        return SprintOperationLayer(self.instance, **options)

    def get(self, path: str, *, query: str = "", app: WebApp | None = None):
        return (app or self.app()).handle("GET", path, query=query)

    def post(self, path: str, payload: dict[str, Any], *, app: WebApp | None = None):
        return (app or self.app()).handle("POST", path, body=json.dumps(payload).encode("utf-8"))

    def json_of(self, response) -> dict[str, Any]:
        return json.loads(response.body.decode("utf-8"))

    def text_of(self, response) -> str:
        return response.body.decode("utf-8")


# -- criterion 1: one table, and only named operations -------------------------------------------


class RaisingLayer:
    """A layer whose every call refuses with the error it was built with."""

    def __init__(self, exc: ReadError) -> None:
        self.exc = exc

    def __getattr__(self, _name: str):
        def refuse(*_args: Any, **_kwargs: Any):
            raise self.exc

        return refuse


class StatusMappingTests(unittest.TestCase):
    """Criterion 1: a protocol code becomes a status in one place, for every route."""

    CODES: ClassVar[dict[ReadError, int]] = {
        TaskNotFound("x"): 404,
        RunNotFound("x"): 404,
        InvalidCursor("x"): 400,
        ValidationRefused("x"): 400,
        OwnerConflict("x"): 409,
        InstallationUnavailable("x"): 503,
        RuntimeUnavailable("x"): 503,
    }

    def test_every_code_the_layer_can_raise_has_exactly_one_status(self) -> None:
        codes = {
            value.code
            for value in vars(error_module).values()
            if isinstance(value, type) and issubclass(value, ReadError) and value is not ReadError
        }
        self.assertEqual(codes - set(HTTP_STATUS_BY_CODE), set())

    def test_a_code_this_transport_does_not_know_is_a_bug_and_not_a_guess(self) -> None:
        self.assertEqual(status_for("a-code-from-the-future"), UNMAPPED_CODE_STATUS)
        self.assertEqual(UNMAPPED_CODE_STATUS, 500)

    #: A body each POST route would be answered on, so that a refusal is the layer's and not this
    #: test's. Both encodings are here because both are published: a program sends the JSON object,
    #: and a browser sends the form the sprint page serves.
    #: Per JSON route, because each POST holds its body to its own closed field list and a body
    #: another route's fields would be refused by the transport before the layer saw it.
    JSON_BODIES: ClassVar[dict[str, dict[str, Any]]] = {
        "/api/runs/start": {"ref": "ummanu-1", "request_id": "r", "profile": "p"},
        "/api/runs/review": {"ref": "ummanu-1", "request_id": "r", "profile": "p"},
        "/api/pause/drain": {"reason": "why"},
        "/api/pause/resume": {},
        "/api/sprints/{ref}/comment": {"request_id": "r", "body": "a comment"},
        "/api/sprints/{ref}/close": {"request_id": "r", "reason": "why", "closeout": "what became"},
        "/api/tasks/{ref}/comment": {"request_id": "r", "body": "a comment"},
        "/api/tasks/{ref}/move": {"request_id": "r", "target": "ready", "reason": "why"},
        "/api/providers/codex/reset-limit": {"request_id": "r"},
    }
    BODIES: ClassVar[dict[str, bytes]] = {
        "json": json.dumps({"ref": "ummanu-1", "request_id": "r", "profile": "p"}).encode("utf-8"),
        "form": urlencode(
            [
                ("request_id", "r"),
                ("product", "ummanu"),
                ("goal", "a goal"),
                ("definition_of_done", "a definition of done"),
                ("issues", "issue:1"),
                ("projects", "ummanu"),
                ("observer", "claude-observer"),
                ("worker", ""),
                ("reviewer", ""),
            ]
        ).encode("utf-8"),
    }

    def _body(self, route) -> bytes:
        if route.pattern == "/po/login":
            return urlencode([("token", "t")]).encode("utf-8")
        if route.pattern.startswith("/owner-events/"):
            # The owner event forms carry only the view to return to; none is the unread default.
            return b""
        if route.body == "form":
            return self.BODIES["form"]
        if route.method != "POST":
            return b""
        return json.dumps(self.JSON_BODIES[route.pattern]).encode("utf-8")

    def test_every_published_post_route_has_a_body_this_test_knows(self) -> None:
        """A route added without a body here would be answered 400 by the transport and read as a pass."""
        posted = {route.pattern for route in ROUTES if route.method == "POST" and route.body == "json"}
        self.assertEqual(posted, set(self.JSON_BODIES))

    #: The one route whose own read refusing is its content rather than its status. The doctor page
    #: exists to say that this installation's health could not be read, with the reason and with the
    #: lamp red; answering 503 there would replace the only page that can say why health is unknown
    #: with a refusal that cannot. Its refusal path is asserted in `tests/test_web_doctor.py`.
    ANSWERS_ITS_OWN_REFUSAL: ClassVar[frozenset[str]] = frozenset({"/doctor"})

    def test_every_route_answers_a_refusal_with_the_status_of_its_code(self) -> None:
        for error, status in self.CODES.items():
            # The PO layers refuse too: the token check itself answers a refusing token layer with
            # the status of its code, so every /po route is held to the same table.
            app = WebApp(
                *(RaisingLayer(error) for _ in range(LAYERS)),
                po_auth=RaisingLayer(error),
                po=RaisingLayer(error),
                owner_events=RaisingLayer(error),
                provider_ops=RaisingLayer(error),
            )
            for route in ROUTES:
                if route.pattern in self.ANSWERS_ITS_OWN_REFUSAL:
                    continue
                path = (
                    route.pattern.replace("{ref}", "ummanu-1")
                    .replace("{run_id}", "pr-1")
                    .replace("{request_id}", "r-1")
                    .replace("{session}", "s-1")
                    .replace("{event_id}", "1")
                )
                with self.subTest(code=error.code, route=route.pattern):
                    response = app.handle(route.method, path, body=self._body(route))
                    self.assertEqual(response.status, status)

    def test_a_refused_json_route_answers_the_protocol_code_itself(self) -> None:
        app = WebApp(*(RaisingLayer(OwnerConflict("somebody else has this card")) for _ in range(LAYERS)))
        response = app.handle("GET", "/api/tasks/ummanu-1")
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.body)["error"]["code"], "owner_conflict")

    def test_a_refused_page_stays_a_page_and_carries_the_same_status(self) -> None:
        app = WebApp(*(RaisingLayer(TaskNotFound("no such card")) for _ in range(LAYERS)))
        response = app.handle("GET", "/tasks/ummanu-1")
        self.assertEqual(response.status, 404)
        self.assertIn("text/html", response.content_type)
        self.assertIn("no such card", response.body.decode("utf-8"))


class RouteTableTests(TransportFixture):
    """Criterion 1 again: the published routes are the whole surface, and none of them runs a shell."""

    PUBLISHED: ClassVar[set[tuple[str, str]]] = {
        ("GET", "/"),
        ("GET", "/tasks/{ref}"),
        ("GET", "/tasks/{ref}/heads/{run_id}"),
        ("GET", "/sprints"),
        ("GET", "/projects"),
        ("GET", "/projects/{project}"),
        ("GET", "/sprints/new"),
        ("POST", "/sprints"),
        ("GET", "/sprints/{ref}"),
        ("GET", "/api/system"),
        ("GET", "/api/tasks/{ref}"),
        ("GET", "/api/tasks/{ref}/events"),
        ("GET", "/api/tasks/{ref}/runs"),
        ("GET", "/api/tasks/{ref}/heads/{run_id}"),
        ("GET", "/api/runs/{run_id}"),
        ("POST", "/api/runs/start"),
        ("POST", "/api/runs/review"),
        ("GET", "/history"),
        ("GET", "/doctor"),
        ("GET", "/api/pause"),
        ("GET", "/api/pause/scope"),
        ("POST", "/api/pause/drain"),
        ("POST", "/api/pause/resume"),
        ("GET", "/api/sprints"),
        ("POST", "/api/sprints/{ref}/comment"),
        ("POST", "/api/sprints/{ref}/close"),
        ("GET", "/api/history"),
        ("GET", "/api/history/{request_id}"),
        ("POST", "/api/tasks/{ref}/comment"),
        ("POST", "/api/tasks/{ref}/move"),
        ("POST", "/api/providers/codex/reset-limit"),
        ("POST", "/po/login"),
        ("GET", "/po"),
        ("POST", "/po/sessions"),
        ("GET", "/po/sessions/{session}"),
        ("POST", "/po/sessions/{session}/messages"),
        ("POST", "/po/sessions/{session}/stop"),
        ("POST", "/po/sessions/{session}/close"),
        ("POST", "/po/sessions/{session}/title"),
        ("GET", "/po/api/sessions/{session}"),
        ("GET", "/owner-events"),
        ("POST", "/owner-events/read-all"),
        ("POST", "/owner-events/{event_id}/read"),
    }

    def test_the_route_table_is_exactly_what_is_documented(self) -> None:
        self.assertEqual({(route.method, route.pattern) for route in ROUTES}, self.PUBLISHED)
        self.assertEqual(len(ROUTES), len(self.PUBLISHED))

    def test_every_route_is_one_operation_of_the_layer_below(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route.pattern):
                self.assertRegex(
                    route.operation,
                    r"^(reads|ops|sprint_reads|sprint_ops|pause_reads|pause_ops|command_reads|card_ops"
                    r"|doctor|po_auth|po|owner_events|provider_ops)\.[a-z_]+$",
                )

    def test_there_is_no_endpoint_that_runs_something_it_was_given(self) -> None:
        """A route that took a command, a script or a path to execute would be the whole hole."""
        for route in ROUTES:
            for word in ("shell", "exec", "eval", "command", "run-command", "cmd", "spawn", "proxy"):
                self.assertNotIn(word, route.pattern, msg=route.pattern)
        source = (REPO_ROOT / "src" / "ummanu" / "web").rglob("*.py")
        forbidden = {"subprocess", "os.system", "shutil", "pty"}
        offenders: list[str] = []
        for path in sorted(source):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                offenders += [f"{path.name}: {name}" for name in names if name.split(".")[0] in forbidden]
        self.assertEqual(offenders, [])

    def test_an_unrouted_path_is_a_refusal_and_reaches_no_handler(self) -> None:
        for path in ("/api/shell", "/../etc/passwd", "/api/tasks", "/api/runs/pr-1/stop"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status, 404)
                self.assertEqual(self.post(path, {}).status, 404)

    def test_an_unrouted_method_on_a_routed_path_is_405(self) -> None:
        self.assertEqual(self.post("/api/system", {}).status, 405)
        self.assertEqual(self.get("/api/runs/start").status, 405)

    def test_a_post_body_carrying_an_unknown_field_is_refused(self) -> None:
        response = self.post(
            "/api/runs/start",
            {"ref": "ummanu-run-1", "request_id": "r", "profile": WORKER_PROFILE, "command": "rm -rf /"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.json_of(response)["error"]["code"], "validation")
        self.assertIn("command", self.json_of(response)["error"]["message"])

    def test_a_body_that_is_not_a_json_object_is_a_validation_refusal(self) -> None:
        app = self.app()
        for body in (b"", b"[]", b"not json"):
            with self.subTest(body=body):
                response = app.handle("POST", "/api/runs/start", body=body)
                self.assertEqual(response.status, 400)


# -- criterion 3: cursors survive a reload and a reconnection ------------------------------------


class CursorOverHttpTests(TransportFixture):
    def test_reading_resumes_from_the_cursor_a_client_kept(self) -> None:
        self._card()
        self._journal([self._event("ummanu-run-1", index) for index in range(3)])
        app = self.app()
        first = self.json_of(self.get("/api/tasks/ummanu-run-1/events", query="limit=2", app=app))
        self.assertEqual(
            [item["event_id"] for item in first["items"]], ["evt-ummanu-run-1-0", "evt-ummanu-run-1-1"]
        )

        # "A reconnection": a brand new application object, as a restarted browser or a second tab
        # would reach. The server holds nothing, so only the cursor decides where reading resumes.
        self._journal([self._event("ummanu-run-1", 3)])
        resumed = self.json_of(
            self.get(
                "/api/tasks/ummanu-run-1/events",
                query=f"cursor={first['next_cursor']}&limit=10",
                app=self.app(),
            )
        )
        self.assertEqual(
            [item["event_id"] for item in resumed["items"]],
            ["evt-ummanu-run-1-2", "evt-ummanu-run-1-3"],
        )
        # And nothing was lost or repeated: the two pages together are the whole journal, once.
        self.assertEqual(
            [item["event_id"] for item in first["items"] + resumed["items"]],
            [f"evt-ummanu-run-1-{index}" for index in range(4)],
        )

    def test_the_same_cursor_read_twice_is_the_same_page(self) -> None:
        self._card()
        self._journal([self._event("ummanu-run-1", index) for index in range(4)])
        app = self.app()
        page = self.json_of(self.get("/api/tasks/ummanu-run-1/events", query="limit=2", app=app))
        again = self.json_of(
            self.get("/api/tasks/ummanu-run-1/events", query=f"cursor={page['next_cursor']}", app=app)
        )
        repeated = self.json_of(
            self.get("/api/tasks/ummanu-run-1/events", query=f"cursor={page['next_cursor']}", app=app)
        )
        self.assertEqual(again["items"], repeated["items"])

    def test_a_broken_cursor_is_a_validation_refusal_and_not_a_reset(self) -> None:
        self._card()
        self._journal([self._event("ummanu-run-1", 0)])
        for cursor in ("nonsense", "eyJyZWYiOiAibm90LXRoaXMtY2FyZCJ9"):
            with self.subTest(cursor=cursor):
                response = self.get("/api/tasks/ummanu-run-1/events", query=f"cursor={cursor}")
                self.assertEqual(response.status, 400)
                document = self.json_of(response)
                self.assertEqual(document["error"]["code"], "validation")
                # The refusal carries no page: a client is told, and never handed the beginning.
                self.assertNotIn("items", document)

    def test_a_cursor_belonging_to_another_card_is_refused_by_name(self) -> None:
        self._card()
        self._card("ummanu-run-2", column=1)
        self._journal([self._event("ummanu-run-1", 0), self._event("ummanu-run-2", 0)])
        foreign = self.json_of(self.get("/api/tasks/ummanu-run-1/events"))["next_cursor"]
        response = self.get("/api/tasks/ummanu-run-2/events", query=f"cursor={foreign}")
        self.assertEqual(response.status, 400)
        self.assertIn("ummanu-run-1", self.json_of(response)["error"]["message"])

    def test_a_limit_that_is_not_a_page_size_is_refused_rather_than_clamped(self) -> None:
        self._card()
        for query in ("limit=0", "limit=-3", "limit=nine", "limit=100000"):
            with self.subTest(query=query):
                self.assertEqual(self.get("/api/tasks/ummanu-run-1/events", query=query).status, 400)


# -- criteria 2 and 3: the pages, and a source that could not answer ------------------------------


class PageTests(TransportFixture):
    def test_a_refused_dispatcher_record_keeps_pages_and_system_readable(self) -> None:
        self._production({"ummanu-9": {"worker_retained_at": 1}})
        app = self.app()
        for path in ("/", "/projects", "/projects/ummanu", "/api/system"):
            with self.subTest(path=path):
                response = app.handle("GET", path)
                self.assertEqual(response.status, 200)
                if path == "/api/system":
                    snapshot = json.loads(response.body)
                    self.assertEqual(snapshot["agents"]["source"]["state"], "unavailable")
                    self.assertEqual(snapshot["projects"]["source"]["state"], "available")
                    self.assertEqual(snapshot["tasks"]["source"]["state"], "available")
                else:
                    self.assertIn("<!doctype html>", response.body.decode())

    def test_the_dashboard_omits_card_and_agent_lists(self) -> None:
        page = self.text_of(self.get("/"))
        self.assertNotIn("no agent is running.", page)
        self.assertNotIn("no card is in flight.", page)
        self.assertIn("no sprint is open.", page)

    def test_an_unreadable_board_does_not_take_the_dashboard_down(self) -> None:
        class SilentBoard:
            def __getattr__(self, _name):
                def refuse(*_args, **_kwargs):
                    raise OSError("the board is not answering")

                return refuse

        response = self.get(
            "/",
            app=WebApp(
                self.reads(board_client=SilentBoard()),
                self.ops(),
                self.sprint_reads(board_client=SilentBoard()),
                self.sprint_ops(board_client=SilentBoard()),
                *self.operator_layers(board_client=SilentBoard()),
            ),
        )
        self.assertEqual(response.status, 200)
        self.assertIn("could not find out which sprints are open", self.text_of(response))

    def test_the_dashboard_stays_compact_when_cards_and_agents_are_running(self) -> None:
        self._card()
        self._production(
            {
                "ummanu-run-1": {
                    "attempt_id": "attempt-1",
                    "state": "in_progress",
                    "worker_pid_file": str(self.tmp / "worker.pid"),
                    "worker_head_run": {"run_id": "run-1", "lifecycle": "working"},
                }
            }
        )
        markup = self.text_of(self.get("/"))
        self.assertNotIn('href="/tasks/ummanu-run-1"', markup)
        self.assertNotIn("In flight", markup)
        self.assertIn("Open sprints", markup)

    def test_the_card_page_shows_state_events_output_and_result(self) -> None:
        task_id = self._card()
        self.board.replace_comments(task_id, [
            {"date_creation": 1, "comment": "[report:done]\nthe worker's own words"},
            {"date_creation": 2, "comment": "[review:green]\nthe reviewer's own words"},
        ])
        self._journal([self._event("ummanu-run-1", 0)])
        markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("the worker&#x27;s own words", markup)
        self.assertIn("the reviewer&#x27;s own words", markup)
        self.assertIn("review:green", markup)
        self.assertIn("event 0", markup)
        self.assertIn("the card is not finished", markup)

    def test_an_open_run_whose_identity_does_not_match_is_not_drawn_as_a_running_one(self) -> None:
        """Criterion 3, on the runs this transport itself starts.

        A run whose heartbeat no longer names it -- a reused PID, a heartbeat from another head --
        is `unknown` and *not over*, which is a different thing from running and a different thing
        from finished. The page has to say which, so the listing carries the state rather than the
        record alone.
        """
        self._card()
        started = self.json_of(
            self.post(
                "/api/runs/start",
                {"ref": "ummanu-run-1", "request_id": "web-1", "profile": WORKER_PROFILE},
            )
        )
        pid_file = Path(started["run"]["pid_file"])
        heartbeat = json.loads(pid_file.read_text(encoding="utf-8"))
        heartbeat["run_id"] = "pr-somebody-else"
        pid_file.write_text(json.dumps(heartbeat, sort_keys=True), encoding="utf-8")

        read = self.json_of(self.get(f"/api/runs/{started['run']['run_id']}"))
        self.assertEqual(read["state"]["value"], "unknown")
        self.assertFalse(read["state"]["ended"])

        markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("state-unknown", markup)
        self.assertIn("belongs to another process", markup)
        self.assertIn("(open)", markup)

    def test_a_settled_run_and_an_open_one_read_differently_on_the_page(self) -> None:
        self._card()
        started = self.json_of(
            self.post(
                "/api/runs/start",
                {"ref": "ummanu-run-1", "request_id": "web-1", "profile": WORKER_PROFILE},
            )
        )
        open_markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("(open)", open_markup)
        self.assertNotIn("(over)", open_markup)

        result_path = Path(started["run"]["result_path"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({"status": "done"}), encoding="utf-8")
        self.assertTrue(self.json_of(self.get(f"/api/runs/{started['run']['run_id']}"))["state"]["ended"])
        settled_markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("(over)", settled_markup)

    def test_a_reviewer_verdict_is_on_the_card_page_and_not_only_in_the_json(self) -> None:
        """The one thing a card page is opened to find out about a review.

        Two reviews that both ended normally read identically in the state column -- `finished`,
        over -- and differ in exactly one place: the word the reviewer wrote. A page that stopped at
        the state would answer "did the reviewer run" while being asked "what did it say".
        """
        self._card()
        worker = self._settled_run("web-worker", {"status": "done", "summary": "a line was added"})
        review = self.json_of(
            self.post(
                "/api/runs/review",
                {
                    "ref": "ummanu-run-1",
                    "request_id": "web-review",
                    "profile": REVIEWER_PROFILE,
                    "worker_run_id": worker,
                },
            )
        )
        self._publish(review["review"]["run"], {"verdict": "red", "summary": "it does not stand"})

        markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("verdict", markup)
        self.assertIn("red", markup)
        self.assertIn("it does not stand", markup)
        self.assertIn("a line was added", markup)

    def test_a_failed_run_reads_as_a_failure_and_not_as_a_run_with_nothing_to_show(self) -> None:
        """Criterion 4 of secretary-1566, as the page renders it.

        A head that exited non-zero produced no result, and "produced no result" is what an open
        run that has not got there yet also has. Those are not the same thing, so the page says the
        failure, its exit status, and that the head published nothing -- and never the empty words
        an unstarted run gets.
        """
        self._card()
        started = self.json_of(
            self.post(
                "/api/runs/start",
                {"ref": "ummanu-run-1", "request_id": "web-1", "profile": WORKER_PROFILE},
            )
        )
        open_markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("this run has produced nothing yet.", open_markup)

        self._exited(started["run"], exit_code=7)
        read = self.json_of(self.get(f"/api/runs/{started['run']['run_id']}"))
        self.assertEqual(read["state"]["value"], "process_failed")

        markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("state-process_failed", markup)
        self.assertIn("exit status 7", markup)
        self.assertIn("the head published no result", markup)
        self.assertNotIn("this run has produced nothing yet.", markup)

    # -- the pieces those two are made of --------------------------------------------------------

    def _settled_run(self, request_id: str, result: dict[str, Any]) -> str:
        """A worker run that published `result` and has therefore been settled by a read."""
        started = self.json_of(
            self.post(
                "/api/runs/start",
                {"ref": "ummanu-run-1", "request_id": request_id, "profile": WORKER_PROFILE},
            )
        )
        self._publish(started["run"], result)
        return started["run"]["run_id"]

    def _publish(self, run: dict[str, Any], document: dict[str, Any]) -> None:
        """Write a head's own result where it was told to, and let the layer settle the run."""
        path = Path(run["result_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertTrue(self.json_of(self.get(f"/api/runs/{run['run_id']}"))["state"]["ended"])

    def _exited(self, run: dict[str, Any], *, exit_code: int) -> None:
        """The supervisor's own record of a head that is gone, and a heartbeat that says so."""
        with Path(run["journal_path"]).open("a", encoding="utf-8") as journal:
            journal.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "seq": 2,
                        "kind": RUN_EXITED,
                        "run_id": run["run_id"],
                        "at": 2.0,
                        "exit_code": exit_code,
                        "signal": None,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        heartbeat = json.loads(Path(run["pid_file"]).read_text(encoding="utf-8"))
        heartbeat["pid"] = _dead_pid()
        Path(run["pid_file"]).write_text(json.dumps(heartbeat, sort_keys=True), encoding="utf-8")

    def test_a_card_with_no_history_says_so_rather_than_showing_nothing(self) -> None:
        self._card()
        markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertIn("no event has been recorded for this card.", markup)

    def test_an_unreadable_run_record_marks_one_section_and_leaves_the_page_standing(self) -> None:
        """Criteria 3 and 7: a dead source is shown on the page, it does not take the page down.

        The run store is one source among several. An unreadable record under
        `<data>/webproto/runs/` says nothing about the card, its history, its output or its result,
        so those are still rendered and only the product-runs section is marked unavailable, with
        the reason the layer gave.
        """
        task_id = self._card()
        self.board.replace_comments(task_id, [
            {"date_creation": 1, "comment": "[report:done]\nwhat the worker said"}
        ])
        self._journal([self._event("ummanu-run-1", 0)])
        runs = self.data_dir / "webproto" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "pr-broken.json").write_text("{not json", encoding="utf-8")

        response = self.get("/tasks/ummanu-run-1")
        self.assertEqual(response.status, 200)
        markup = self.text_of(response)
        self.assertIn("could not find out this card's product runs:", markup)
        self.assertIn("pr-broken.json", markup)
        # The rest of the page is read from other sources and is still there.
        self.assertIn("A small task", markup)
        self.assertIn("what the worker said", markup)
        self.assertIn("event 0", markup)

    def test_an_unreadable_run_record_is_the_published_backend_unavailable_code(self) -> None:
        self._card()
        runs = self.data_dir / "webproto" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "pr-broken.json").write_text("{not json", encoding="utf-8")
        response = self.get("/api/tasks/ummanu-run-1/runs")
        self.assertEqual(response.status, 503)
        self.assertEqual(self.json_of(response)["error"]["code"], "backend_unavailable")

    def test_a_card_the_board_does_not_hold_is_a_404_page(self) -> None:
        response = self.get("/tasks/ummanu-absent")
        self.assertEqual(response.status, 404)
        self.assertIn("text/html", response.content_type)

    def test_a_page_escapes_what_the_board_gave_it(self) -> None:
        self._card(column=2)
        self.board.update(40, title="<script>alert(1)</script>")
        markup = self.text_of(self.get("/tasks/ummanu-run-1"))
        self.assertNotIn("<script>alert(1)</script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_every_page_says_this_service_is_local_only(self) -> None:
        self._card()
        for path in ("/", "/tasks/ummanu-run-1"):
            with self.subTest(path=path):
                self.assertIn("local only", self.text_of(self.get(path)))
        self.assertIn("no password", pages.LOOPBACK_NOTICE)


# -- criterion 4: a repeated POST is one run ------------------------------------------------------


class IdempotentPostTests(TransportFixture):
    def test_the_same_post_twice_is_one_run_and_one_process(self) -> None:
        self._card()
        app = self.app()
        payload = {"ref": "ummanu-run-1", "request_id": "web-1", "profile": WORKER_PROFILE}
        first = self.post("/api/runs/start", payload, app=app)
        self.assertEqual(first.status, 200)
        # A reconnected client: a new application object, the same request id the browser kept.
        second = self.post("/api/runs/start", payload, app=self.app())
        self.assertEqual(second.status, 200)
        self.assertEqual(self.json_of(first)["run"]["run_id"], self.json_of(second)["run"]["run_id"])
        self.assertEqual(len(self.runtime.starts), 1)
        listing = self.json_of(self.get("/api/tasks/ummanu-run-1/runs"))
        self.assertEqual(
            [item["run"]["run_id"] for item in listing["items"]],
            [self.json_of(first)["run"]["run_id"]],
        )

    def test_a_repeat_naming_other_inputs_is_refused_rather_than_answered(self) -> None:
        self._card()
        self._card("ummanu-run-2")
        app = self.app()
        self.post(
            "/api/runs/start",
            {"ref": "ummanu-run-1", "request_id": "web-1", "profile": WORKER_PROFILE},
            app=app,
        )
        response = self.post(
            "/api/runs/start",
            {"ref": "ummanu-run-2", "request_id": "web-1", "profile": WORKER_PROFILE},
            app=self.app(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.runtime.starts), 1)

    def test_a_start_missing_a_required_field_never_reaches_the_operation(self) -> None:
        self._card()
        for payload in (
            {"request_id": "web-1", "profile": WORKER_PROFILE},
            {"ref": "ummanu-run-1", "profile": WORKER_PROFILE},
            {"ref": "ummanu-run-1", "request_id": "web-1"},
        ):
            with self.subTest(payload=sorted(payload)):
                response = self.post("/api/runs/start", payload)
                self.assertEqual(response.status, 400)
        self.assertEqual(self.runtime.starts, [])

    def test_a_review_of_a_worker_that_is_still_running_is_a_refusal_not_a_second_head(self) -> None:
        self._card()
        app = self.app()
        started = self.json_of(
            self.post(
                "/api/runs/start",
                {"ref": "ummanu-run-1", "request_id": "web-1", "profile": WORKER_PROFILE},
                app=app,
            )
        )
        response = self.post(
            "/api/runs/review",
            {"request_id": "web-2", "profile": WORKER_PROFILE, "worker_run_id": started["run"]["run_id"]},
            app=self.app(),
        )
        self.assertIn(response.status, {400, 409})
        self.assertEqual(len(self.runtime.starts), 1)

    def test_a_run_is_read_back_by_id_through_the_same_operation(self) -> None:
        self._card()
        started = self.json_of(
            self.post(
                "/api/runs/start",
                {"ref": "ummanu-run-1", "request_id": "web-1", "profile": WORKER_PROFILE},
            )
        )
        run_id = started["run"]["run_id"]
        read = self.json_of(self.get(f"/api/runs/{run_id}"))
        self.assertEqual(read["run"]["run_id"], run_id)
        self.assertEqual(read["kind"], "product_run")


# -- criterion 5: loopback, and nothing else -----------------------------------------------------


class LoopbackTests(TransportFixture):
    def test_a_non_loopback_address_is_refused_before_a_socket_exists(self) -> None:
        for host in ("0.0.0.0", "::", "192.168.1.10", "example.invalid", "10.0.0.1"):
            with self.subTest(host=host):
                with self.assertRaises(LoopbackOnly) as refused:
                    check_bind(host)
                self.assertIn("DoD 5", str(refused.exception))

    def test_the_loopback_addresses_are_accepted(self) -> None:
        for host in ("127.0.0.1", "127.0.0.5", "::1", "localhost", ""):
            with self.subTest(host=host):
                self.assertTrue(check_bind(host))
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")

    def test_a_name_that_resolves_off_loopback_is_refused_rather_than_bound(self) -> None:
        """The hole a spelling check leaves: the name is fine, the address it names is not.

        `--host` is a name until something resolves it, and what resolves it is the host's own
        mappings rather than this program. So the refusal has to be about the addresses, and a
        name mapped to a routable one must be refused exactly like the literal would be.
        """
        routable = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.7.7", 0))]
        with mock.patch.object(socket, "getaddrinfo", return_value=routable):
            for host in ("localhost", "localhost.localdomain", "ip6-localhost"):
                with self.subTest(host=host):
                    with self.assertRaises(LoopbackOnly) as refused:
                        check_bind(host)
                    self.assertIn("192.168.7.7", str(refused.exception))
                    self.assertIn("DoD 5", str(refused.exception))

    def test_a_name_is_bound_as_the_literal_address_it_resolved_to(self) -> None:
        """Resolved once, here, and the socket is handed that answer — not the name again."""
        family, address = resolve_bind("localhost")
        self.assertIn(family, (socket.AF_INET, socket.AF_INET6))
        self.assertTrue(ipaddress.ip_address(address).is_loopback)
        self.assertEqual(check_bind("127.0.0.1"), "127.0.0.1")

    def test_a_name_that_resolves_to_both_loopback_and_a_routable_address_is_refused(self) -> None:
        mixed = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0)),
        ]
        with (
            mock.patch.object(socket, "getaddrinfo", return_value=mixed),
            self.assertRaises(LoopbackOnly),
        ):
            check_bind("localhost")

    def test_the_command_defaults_to_loopback_and_refuses_anything_else(self) -> None:
        from ummanu.cli import build_parser

        args = build_parser().parse_args(["web-serve", "--instance", str(self.instance)])
        self.assertEqual(args.host, "127.0.0.1")

    def test_a_bound_server_answers_a_real_request_on_loopback(self) -> None:
        """One real socket, so the handler between `http.server` and the app is exercised too."""
        self._card()
        server = build_server(self.app(), host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[0], server.server_address[1]
        self.assertEqual(host, "127.0.0.1")

        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse()
        markup = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("local only", markup)

        connection.request("GET", "/api/tasks/ummanu-absent")
        refused = connection.getresponse()
        body = refused.read().decode("utf-8")
        self.assertEqual(refused.status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

        connection.request(
            "POST",
            "/api/runs/start",
            body=json.dumps({"ref": "ummanu-run-1", "request_id": "sock-1", "profile": WORKER_PROFILE}),
            headers={"Content-Type": "application/json"},
        )
        started = connection.getresponse()
        document = json.loads(started.read().decode("utf-8"))
        self.assertEqual(started.status, 200)
        self.assertEqual(len(self.runtime.starts), 1)
        self.assertTrue(document["run"]["run_id"])


# -- criterion 5: an unexpected failure is contained, not dropped --------------------------------


class ExplodingApp:
    """The real application with one path made to raise, as the live process raised on every one.

    A double rather than a broken layer, because what is being tested is the socket adapter: the
    application has to be the real one for the *other* request in each test — the healthy one after
    the failure — to prove the server is still serving.
    """

    def __init__(self, app: WebApp, path: str, exc: BaseException) -> None:
        self.app = app
        self.path = path
        self.exc = exc
        self.raised = 0

    def handle(self, method: str, path: str, **kwargs: Any):
        if path == self.path:
            self.raised += 1
            raise self.exc
        return self.app.handle(method, path, **kwargs)


#: An exception whose text is exactly what must not be published: a credential in a DSN. Nothing has
#: audited the message of a failure nobody expected, so neither the body nor the log may quote it.
SECRET_MESSAGE = "connecting to postgresql://ummanu_app:hunter2@127.0.0.1:5432/board failed"


class FailureContainmentTests(TransportFixture):
    """`http.server` answers an escaped exception by closing the connection with no response.

    That is what `ummanu-web.service` did for nineteen hours on 2026-09-11 (secretary-1624):
    `jsonschema.exceptions._WrappedReferencingError: Unresolvable: adapter.schema.json` escaped
    `_Handler._answer`, and `GET /`, `GET /sprints/new` and `GET /api/system` all returned an empty
    reply. The transport now answers a bounded 500 instead and stays able to answer the next
    request. These tests drive a real socket, because the defect lived between `http.server` and the
    application and cannot be seen through `WebApp.handle`.
    """

    def serve(self, app: Any) -> tuple[str, int]:
        server = build_server(app, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server.server_address[0], server.server_address[1]

    def exploding(self, exc: BaseException, *, path: str = "/api/system") -> ExplodingApp:
        return ExplodingApp(self.app(), path, exc)

    def test_an_escaped_exception_is_a_complete_bounded_500_and_the_server_answers_again(self) -> None:
        app = self.exploding(RuntimeError(SECRET_MESSAGE))
        host, port = self.serve(app)
        log = io.StringIO()

        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        with mock.patch("sys.stderr", log):
            connection.request("GET", "/api/system")
            response = connection.getresponse()
            body = response.read()
            # The same connection, after the failure: a complete response left the socket usable.
            connection.request("GET", "/")
            healthy = connection.getresponse()
            markup = healthy.read().decode("utf-8")

        self.assertEqual(response.status, 500)
        self.assertEqual(int(response.getheader("Content-Length")), len(body))
        self.assertTrue(body)
        self.assertLess(len(body), 1024, "the containment body is a fixed sentence, not a dump")
        self.assertEqual(healthy.status, 200)
        self.assertIn("local only", markup)
        self.assertEqual(app.raised, 1)

    def test_the_contained_body_names_the_class_and_a_reference_and_quotes_no_message(self) -> None:
        app = self.exploding(RuntimeError(SECRET_MESSAGE))
        host, port = self.serve(app)
        log = io.StringIO()

        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        with mock.patch("sys.stderr", log):
            connection.request("GET", "/api/system")
            response = connection.getresponse()
            text = response.read().decode("utf-8")

        self.assertIn("RuntimeError", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("postgresql", text)
        reference = re.search(r"reference: ([0-9a-f]{12})", text)
        self.assertIsNotNone(reference, text)
        # The log is where the call site is, and it joins to the body by that one reference.
        recorded = log.getvalue()
        self.assertIn(f"ref={reference.group(1)}", recorded)
        self.assertIn("RuntimeError", recorded)
        self.assertIn("test_web_transport.py:", recorded)
        self.assertNotIn("hunter2", recorded)
        self.assertNotIn("postgresql", recorded)

    def test_the_contained_response_carries_the_same_security_headers_as_every_other_answer(self) -> None:
        host, port = self.serve(self.exploding(RuntimeError("boom")))
        log = io.StringIO()

        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        with mock.patch("sys.stderr", log):
            connection.request("GET", "/api/system")
            response = connection.getresponse()
            response.read()

        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("Content-Type"), "text/plain; charset=utf-8")

    def test_every_kind_of_escaped_failure_is_contained_the_same_way(self) -> None:
        """Application, backend read, config and schema validation: one boundary, not four."""
        failures = [
            RuntimeError("an application invariant broke"),
            OSError("the backend read failed"),
            ConfigError("cannot parse config"),
            _schema_drift_failure(),
        ]
        for exc in failures:
            with self.subTest(failure=type(exc).__name__):
                host, port = self.serve(self.exploding(exc))
                connection = HTTPConnection(host, port, timeout=10)
                self.addCleanup(connection.close)
                with mock.patch("sys.stderr", io.StringIO()) as log:
                    connection.request("GET", "/api/system")
                    response = connection.getresponse()
                    text = response.read().decode("utf-8")
                self.assertEqual(response.status, 500)
                self.assertIn(type(exc).__name__, text)
                self.assertIn(type(exc).__name__, log.getvalue())

    def test_a_deliberate_refusal_is_untouched_by_the_containment_boundary(self) -> None:
        """The application's own 4xx and 5xx still come from the application's one status table."""
        host, port = self.serve(self.app())

        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/tasks/ummanu-absent")
        refused = connection.getresponse()
        document = json.loads(refused.read().decode("utf-8"))

        self.assertEqual(refused.status, 404)
        self.assertEqual(document["error"]["code"], "not_found")
        self.assertNotIn("reference", document["error"])

    def test_a_refusal_the_layer_raises_never_reaches_the_containment_boundary(self) -> None:
        refusal = InstallationUnavailable("the installation cannot be read")
        app = WebApp(
            RaisingLayer(refusal),
            self.ops(),
            self.sprint_reads(),
            self.sprint_ops(),
            *self.operator_layers(),
        )
        host, port = self.serve(app)
        log = io.StringIO()

        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        with mock.patch("sys.stderr", log):
            connection.request("GET", "/api/system")
            response = connection.getresponse()
            document = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, status_for(refusal.code))
        self.assertEqual(document["error"]["code"], refusal.code)
        self.assertNotIn("unhandled", log.getvalue())


# -- criterion 6: the installed routes, over a socket, after a coherent start ---------------------


def _schema_drift_failure() -> BaseException:
    """The live failure itself: a registry-less validator against today's bundled schemas.

    Reconstructed rather than described, so the containment boundary is shown to hold for the exact
    exception that escaped it. `tests/test_web_process_coherence.py` owns why this raises.
    """
    document = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "onboarding" / "happy-path.json").read_text(encoding="utf-8")
    )
    try:
        list(Draft202012Validator(load_schema("onboarding-contract")).iter_errors(document))
    except Exception as exc:  # noqa: BLE001 - the point is that it is not a declared protocol error
        return exc
    raise AssertionError("the bundled schemas no longer carry a cross-file $ref to resolve")


class InstalledRouteTests(TransportFixture):
    """The three GETs the live service answered with an empty reply, over a real socket."""

    ROUTES_UNDER_TEST: ClassVar[tuple[str, ...]] = ("/", "/sprints/new", "/api/system")

    def test_the_installed_get_routes_answer_200_after_a_coherent_start(self) -> None:
        self._card()
        server = build_server(self.app(), host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[0], server.server_address[1]

        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        for path in self.ROUTES_UNDER_TEST:
            with self.subTest(path=path):
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertTrue(body)
                self.assertEqual(int(response.getheader("Content-Length")), len(body))

    def test_the_routes_under_test_are_still_installed_routes(self) -> None:
        """The list above is not a second route table: every path in it is one of the real ones."""
        installed = {route.pattern for route in ROUTES if route.method == "GET"}
        self.assertTrue(set(self.ROUTES_UNDER_TEST) <= installed)


class RequestDurationLineTests(TransportFixture):
    """The one line per request `ummanu-web.service` leaves in its journal, with a duration.

    Before secretary-1649 that line was the request line `BaseHTTPRequestHandler` prints from
    `send_response`: method, target, status, and nothing about the cost. The sprint's whole subject
    is how long these pages take, and the operator's own record of it said nothing — so the line
    now carries the milliseconds the application spent, and there is still exactly one of it.
    """

    #: Every field of the line, in order. A test that matched loosely would not notice the target
    #: or the status quietly leaving it.
    LINE = re.compile(
        r"^(?P<client>\S+) (?P<method>[A-Z]+) (?P<target>\S+) (?P<status>\d{3}) (?P<ms>\d+\.\d)ms$"
    )

    def serve(self, app: Any) -> tuple[str, int]:
        server = build_server(app, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server.server_address[0], server.server_address[1]

    def request_lines(self, log: io.StringIO) -> list[re.Match[str]]:
        return [
            match
            for match in (self.LINE.match(line) for line in log.getvalue().splitlines())
            if match is not None
        ]

    def settle(self, log: io.StringIO, expected: int) -> list[re.Match[str]]:
        """Wait for the server thread to write its lines, which it does after it answers.

        The line is written in a `finally`, on the handler thread, after the response has gone out:
        a client that has already read the body may well get there first. The base class's own
        request line, if one ever came back, is printed *before* ours by `send_response`, so a wait
        that sees ours has seen any duplicate too.
        """
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            lines = self.request_lines(log)
            if len(lines) >= expected:
                return lines
            time.sleep(0.01)
        return self.request_lines(log)

    def drive(self, app: Any, calls: list[tuple[str, str]]) -> list[re.Match[str]]:
        """Make each request over one real socket and return the per-request lines it produced."""
        host, port = self.serve(app)
        log = io.StringIO()
        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        with mock.patch("sys.stderr", log):
            for method, path in calls:
                connection.request(method, path)
                connection.getresponse().read()
            return self.settle(log, len(calls))

    def test_a_normal_200_leaves_one_line_with_the_method_path_status_and_duration(self) -> None:
        self._card()
        lines = self.drive(self.app(), [("GET", "/sprints")])

        self.assertEqual(len(lines), 1, "one answered request is one line")
        line = lines[0]
        self.assertEqual(line["method"], "GET")
        self.assertEqual(line["target"], "/sprints")
        self.assertEqual(line["status"], "200")
        # A page this installation renders costs real work; a zero would mean the clock never ran.
        self.assertGreater(float(line["ms"]), 0.0)
        self.assertLess(float(line["ms"]), 10_000.0)

    def test_the_contained_500_leaves_its_own_line_with_a_duration(self) -> None:
        """`_contain` answers a request too, so it is a request line like any other.

        The frames line it also writes is not one: it carries the reference that joins the body to
        the journal and deliberately has no status and no duration, and the shape check above is
        what separates the two.
        """
        app = ExplodingApp(self.app(), "/api/system", RuntimeError(SECRET_MESSAGE))
        lines = self.drive(app, [("GET", "/api/system")])

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["method"], "GET")
        self.assertEqual(lines[0]["target"], "/api/system")
        self.assertEqual(lines[0]["status"], "500")
        self.assertGreater(float(lines[0]["ms"]), 0.0)

    def test_a_head_and_a_refusal_are_recorded_under_the_verb_and_status_they_had(self) -> None:
        self._card()
        lines = self.drive(
            self.app(),
            [("HEAD", "/"), ("GET", "/api/tasks/ummanu-absent"), ("GET", "/nowhere")],
        )

        self.assertEqual(
            [(line["method"], line["target"], line["status"]) for line in lines],
            [("HEAD", "/", "200"), ("GET", "/api/tasks/ummanu-absent", "404"), ("GET", "/nowhere", "404")],
        )

    def test_a_verb_this_service_does_not_serve_is_recorded_too(self) -> None:
        """`http.server` answers a DELETE with 501 by itself, and that is still an answered request."""
        self._card()
        host, port = self.serve(self.app())
        log = io.StringIO()
        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        with mock.patch("sys.stderr", log):
            connection.request("DELETE", "/api/system")
            response = connection.getresponse()
            response.read()
            lines = self.settle(log, 1)

        self.assertEqual(response.status, 501)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["method"], "DELETE")
        self.assertEqual(lines[0]["target"], "/api/system")
        self.assertEqual(lines[0]["status"], "501")

    def test_the_query_string_stays_on_the_line_and_nothing_prints_a_second_one(self) -> None:
        """The base class's own request line is gone, not merely duplicated with a duration."""
        self._card()
        host, port = self.serve(self.app())
        log = io.StringIO()
        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        with mock.patch("sys.stderr", log):
            connection.request("GET", "/api/system?limit=1")
            connection.getresponse().read()
            self.settle(log, 1)

        recorded = [line for line in log.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(recorded), 1, recorded)
        self.assertEqual(self.request_lines(log)[0]["target"], "/api/system?limit=1")


# -- the transport is a transport ----------------------------------------------------------------


class ThinnessTests(unittest.TestCase):
    """Criterion 1's other half: this package reads the layer and nothing under it."""

    ALLOWED_PREFIXES = ("ummanu.web", "ummanu.webproto")

    def test_the_transport_reaches_the_installation_only_through_the_layer(self) -> None:
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "src" / "ummanu" / "web").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                offenders += [
                    f"{path.name}: {module}"
                    for module in modules
                    if module.startswith("ummanu.") and not module.startswith(self.ALLOWED_PREFIXES)
                ]
        self.assertEqual(offenders, [])

    def test_the_pages_render_from_documents_and_hold_no_state(self) -> None:
        """Two renders of the same document are the same page: nothing accumulates between them."""
        snapshot = {
            "observed_at": "2026-09-06T00:00:00Z",
            "installation": {
                "instance": "/i",
                "name": "n",
                "data_dir": "/d",
                "health": {"source": {"state": "available"}, "status": {}},
            },
            "projects": {"source": {"state": "available"}, "items": []},
            "tasks": {"source": {"state": "available"}, "items": []},
            "agents": {"source": {"state": "available"}, "items": []},
        }
        self.assertEqual(pages.dashboard(snapshot), pages.dashboard(snapshot))


if __name__ == "__main__":
    unittest.main()
