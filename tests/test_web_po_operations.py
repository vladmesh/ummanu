"""`/po` end to end: the real PO layer, PO service and runner, fake `claude`/`codex`, PostgreSQL 16.

Every request goes through `WebApp.handle` with a valid PO cookie; the layer writes through the PO
service's Unix socket, and the service owns the runner. The fakes are the ones `tests.test_po_runner`
drives the runner with (`SLEEP` keeps a turn running).
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from tests.po_cli_fakes import FAKE_CLAUDE, FAKE_CODEX, eventually, unscoped_test_launch
from tests.sql_backend_fixtures import PostgresBoard
from tests.web_fakes import Recording
from ummanu.po import store as po_store
from ummanu.po import token as po_token
from ummanu.po.models import DEFAULT_EFFORTS, DEFAULT_MODELS
from ummanu.po.queue import PoQueue
from ummanu.po.runner import PoRunner
from ummanu.po.service import PoService, listening
from ummanu.po.store import PoStore
from ummanu.web.app import WebApp
from ummanu.webproto.errors import PoRequestConflict, ValidationRefused
from ummanu.webproto.po_auth import PoTokenLayer
from ummanu.webproto.po_ops import PoLayer

BOARD: PostgresBoard
MODELS = {"claude": ("opus", "sonnet"), "codex": ("gpt-5.6-sol",)}


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


class PoWebOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executables = {}
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            executables[name] = str(path)
        self.log = self.root / "fake.log"
        config = BOARD.fresh_database()
        self.addCleanup(BOARD.drop_database, config.dbname)
        self.store = PoStore(config.for_role("app"))
        # The Codex home is the test's own, so no turn reads a rollout of this host's.
        env = {**os.environ, "FAKE_LOG": str(self.log), "CODEX_HOME": str(self.root / "codex-home")}
        self.runner = PoRunner(self.store, self.data, executables=executables, env=env,
                               turn_launcher=unscoped_test_launch)
        self.addCleanup(self.stop_everything)
        self.service = PoService(self.runner, data_dir=self.data)
        self.enterContext(listening(self.service))
        self.service.start()
        loop = threading.Thread(target=self.service.run, kwargs={"tick": 0.05, "say": lambda _line: None})
        loop.start()
        self.addCleanup(loop.join, 10)
        self.addCleanup(self.service.stop)
        self.layer = PoLayer(self.root, data_dir=self.data, store=self.store, models=MODELS)
        po_token.ensure_token(self.data)
        self.cookie = po_token.cookie_value(po_token.read_token(self.data))
        self.app = WebApp(
            *(Recording() for _ in range(8)),
            po_auth=PoTokenLayer(self.root, data_dir=self.data),
            po=self.layer,
        )

    def stop_everything(self) -> None:
        for turn in self.store.running_turns():
            self.runner.stop(turn.session_id)
        pids = self.log.with_name(self.log.name + ".pids")
        if pids.exists():
            for line in pids.read_text().splitlines():
                for pid in line.split():
                    try:
                        os.kill(int(pid), 9)
                    except (ProcessLookupError, PermissionError):
                        pass

    # --- requests ---------------------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        return {"Cookie": f"{po_token.COOKIE_NAME}={self.cookie}"}

    def post(self, path: str, fields: list[tuple[str, str]]):
        return self.app.handle("POST", path, body=urlencode(fields).encode(), headers=self.headers())

    def get(self, path: str):
        return self.app.handle("GET", path, headers=self.headers())

    def create(
        self, cli: str = "claude", model: str = "opus", request_id: str = "create-1", effort: str = "high"
    ) -> str:
        response = self.post(
            "/po/sessions", [("request_id", request_id), ("cli", cli), ("model", model), ("effort", effort)]
        )
        self.assertEqual(response.status, 303, response.body.decode())
        return response.headers["Location"].rsplit("/", 1)[1]

    def send(self, session_id: str, text: str, request_id: str):
        return self.post(f"/po/sessions/{session_id}/messages", [("request_id", request_id), ("text", text)])

    def document(self, session_id: str) -> dict:
        response = self.get(f"/po/api/sessions/{session_id}")
        self.assertEqual(response.status, 200)
        return json.loads(response.body)

    def page(self, session_id: str) -> str:
        response = self.get(f"/po/sessions/{session_id}")
        self.assertEqual(response.status, 200)
        return response.body.decode()

    def feed(self, session_id: str) -> list[tuple[int, str, str]]:
        return [
            (entry["turn_seq"], entry["role"], entry["text"]) for entry in self.document(session_id)["feed"]
        ]

    def settle(self, session_id: str) -> None:
        eventually(
            lambda: not self.document(session_id)["running"] and not self.document(session_id)["queued"],
            "the turn never settled",
        )

    def spawned(self, count: int) -> None:
        pids = self.log.with_name(self.log.name + ".pids")
        eventually(
            lambda: pids.exists() and len(pids.read_text().splitlines()) >= count,
            "the fake never reached its sleeping child",
        )

    def calls(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    # --- sessions ---------------------------------------------------------------------------

    def test_a_session_opens_with_a_listed_model_and_a_model_or_cli_outside_the_list_is_refused(self) -> None:
        session_id = self.create("claude", "sonnet")

        session = self.store.session(session_id)
        self.assertEqual((session.cli, session.model), ("claude", "sonnet"))
        self.assertIn(session_id[:8], self.get("/po").body.decode())

        for cli, model in (("claude", "gpt-5.6-sol"), ("codex", "opus"), ("gemini", "opus"), ("claude", "")):
            with self.subTest(cli=cli, model=model):
                response = self.post(
                    "/po/sessions", [("request_id", f"bad-{cli}-{model}"), ("cli", cli), ("model", model)]
                )
                self.assertEqual(response.status, 400)
                self.assertIn("refused (validation)", response.body.decode())
        with self.assertRaises(ValidationRefused):
            self.layer.po_create_session(request_id="direct", cli="codex", model="gpt-4")
        self.assertEqual([item.session_id for item in self.store.sessions()], [session_id])

    def test_the_default_list_opens_fable_and_gpt_6_astra_sessions_and_refuses_an_off_list_model(
        self,
    ) -> None:
        self.layer = PoLayer(self.root, data_dir=self.data, store=self.store, models=DEFAULT_MODELS)
        self.app = WebApp(
            *(Recording() for _ in range(8)),
            po_auth=PoTokenLayer(self.root, data_dir=self.data),
            po=self.layer,
        )
        form = self.get("/po").body.decode()
        self.assertIn('<option value="fable" data-cli="claude" selected>', form)
        self.assertIn('<option value="high" data-cli="claude" selected>high</option>', form)
        self.assertNotIn('value="default"', form)
        self.assertNotIn("CLI default", form)
        self.assertEqual(
            form.count(" selected>"),
            3,
            "only the CLI, its first model and the CLI's first offered effort are preselected",
        )

        created = []
        for cli, model in (("claude", "fable"), ("codex", "gpt-6-astra")):
            session_id = self.create(cli, model, request_id=f"create-{model}")
            session = self.store.session(session_id)
            self.assertEqual((session.cli, session.model), (cli, model))
            created.append(session_id)

        for cli, model in (("claude", "haiku"), ("codex", "gpt-5.6-astra")):
            with self.subTest(cli=cli, model=model):
                response = self.post(
                    "/po/sessions", [("request_id", f"bad-{cli}-{model}"), ("cli", cli), ("model", model)]
                )
                self.assertEqual(response.status, 400)
        self.assertEqual(sorted(item.session_id for item in self.store.sessions()), sorted(created))

    def test_the_installations_models_are_named_as_people_say_them_in_the_form_and_the_list(self) -> None:
        from ummanu.po.models import models_from_instance

        # The live installation's `po.models`, as instance.yaml lists them.
        instance = {
            "po": {
                "models": {
                    "codex": ["gpt-6-astra", "gpt-6-sol", "gpt-5.6-terra"],
                    "claude": ["fable", "claude-opus-5-5"],
                }
            }
        }
        models = models_from_instance(instance)
        self.assertEqual(models, DEFAULT_MODELS)
        self.layer = PoLayer(self.root, data_dir=self.data, store=self.store, models=models)
        self.app = WebApp(
            *(Recording() for _ in range(8)),
            po_auth=PoTokenLayer(self.root, data_dir=self.data),
            po=self.layer,
        )
        form = self.get("/po").body.decode()
        # The raw id stays the value the form posts; the text is the name.
        self.assertIn('<option value="claude-opus-5-5" data-cli="claude">Opus 5.5</option>', form)
        self.assertIn('<option value="gpt-6-sol" data-cli="codex">GPT-6 Sol</option>', form)
        self.assertNotIn(">claude-opus-5-5</option>", form)

        self.create("claude", "claude-opus-5-5", request_id="create-opus")
        self.create("codex", "gpt-6-sol", request_id="create-sol")
        listing = self.get("/po").body.decode()
        self.assertIn("<b>Opus 5.5</b>", listing)
        self.assertIn("<b>GPT-6 Sol</b>", listing)

    def test_a_session_opens_with_an_offered_effort_and_one_outside_the_list_is_refused(self) -> None:
        self.layer = PoLayer(
            self.root,
            data_dir=self.data,
            store=self.store,
            models=MODELS,
            efforts={"claude": ("high", "max"), "codex": ()},
        )
        self.assertEqual(self.layer.po_models()["efforts"], {"claude": ["high", "max"], "codex": []})

        created = self.layer.po_create_session(
            request_id="effort-1", cli="claude", model="opus", effort="max"
        )
        self.assertEqual(created["effort"], "max")
        self.assertEqual(self.store.session(created["session_id"]).effort, "max")
        # A new session's effort is always explicit: none, `default` or `none` is refused, listed or
        # not, and the refusal names what is offered.
        for effort in ("", "default", "none", "DEFAULT "):
            with self.subTest(effort=effort), self.assertRaises(ValidationRefused) as refused:
                self.layer.po_create_session(
                    request_id=f"unset-{effort}", cli="claude", model="opus", effort=effort
                )
            self.assertIn("explicit effort", str(refused.exception))
            self.assertIn("high, max", str(refused.exception))
        with self.assertRaises(ValidationRefused):
            self.layer.po_create_session(
                request_id="effort-2", cli="codex", model="gpt-5.6-sol", effort="default"
            )

        for cli, model, effort in (("claude", "opus", "low"), ("codex", "gpt-5.6-sol", "high")):
            with self.subTest(cli=cli, effort=effort), self.assertRaises(ValidationRefused):
                self.layer.po_create_session(request_id=f"bad-{effort}", cli=cli, model=model, effort=effort)
        # The effort is one of the inputs its request id is bound to.
        with self.assertRaises(PoRequestConflict):
            self.layer.po_create_session(request_id="effort-1", cli="claude", model="opus", effort="high")
        self.assertEqual(len(self.store.sessions()), 1)

    def test_the_session_documents_carry_the_effort_and_the_model_each_turn_resolved_to(self) -> None:
        session_id = self.create("claude", "sonnet")
        self.assertEqual(self.send(session_id, "hello", "send-1").status, 303)
        self.settle(session_id)

        document = self.document(session_id)
        self.assertEqual(document["session"]["effort"], "high")
        self.assertEqual(document["session"]["resolved_model"], "claude-sonnet-5")
        self.assertEqual(document["turns"][0]["resolved_model"], "claude-sonnet-5")
        (listed,) = self.layer.po_overview()["sessions"]
        self.assertEqual((listed["effort"], listed["resolved_model"]), ("high", "claude-sonnet-5"))

    def test_the_new_session_form_preselects_the_first_model_of_the_chosen_cli(self) -> None:
        from ummanu.web.pages import _PO_FORM_SCRIPT, _po_new_session_form

        models = {cli: list(values) for cli, values in DEFAULT_MODELS.items()}
        efforts = {cli: list(values) for cli, values in DEFAULT_EFFORTS.items()}
        for submitted, expected in (
            ({}, ("claude", "fable")),
            ({"cli": "claude"}, ("claude", "fable")),
            ({"cli": "codex"}, ("codex", "gpt-6-astra")),
            ({"cli": "codex", "model": "fable"}, ("codex", "gpt-6-astra")),
            ({"cli": "codex", "model": "gpt-6-sol"}, ("codex", "gpt-6-sol")),
            ({"cli": "codex", "effort": "default"}, ("codex", "gpt-6-astra")),
        ):
            with self.subTest(submitted=submitted):
                form = _po_new_session_form(models, efforts, request_id="r", submitted=submitted)
                cli, model = expected
                self.assertIn(f'<option value="{cli}" selected>', form)
                self.assertIn(f'<option value="{model}" data-cli="{cli}" selected>', form)
                # The effort starts at the CLI's first offered one; `default` is never an option.
                self.assertIn(f'<option value="high" data-cli="{cli}" selected>high</option>', form)
                self.assertNotIn('value="default"', form)
                self.assertNotIn("CLI default", form)
                self.assertEqual(form.count(" selected>"), 3)
                # One bar, no label column: each select says what it is to a screen reader instead.
                self.assertIn('<form class="po-bar" id="po-new"', form)
                self.assertIn('aria-label="reasoning effort"', form)
        # Changing the CLI in the browser lists only that CLI's options, flat, so no other CLI's
        # optgroup label is left showing, and falls back to its first listed model.
        self.assertIn("select.replaceChildren(...owned);", _PO_FORM_SCRIPT)
        self.assertIn("else if (owned.length) owned[0].selected = true;", _PO_FORM_SCRIPT)
        self.assertNotIn("option.hidden", _PO_FORM_SCRIPT)

    def test_a_session_row_says_model_effort_and_age_on_one_line_and_never_a_table(self) -> None:
        from datetime import UTC, datetime

        from ummanu.web.pages import po_page

        now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        base = {"cli": "claude", "model": "fable", "first_message": "hi", "state": "open"}
        sessions = [
            {
                **base,
                "session_id": "a" * 12,
                "effort": "default",
                "running": True,
                "last_activity_at": "2026-09-26T11:58:00+00:00",
            },
            {
                **base,
                "session_id": "b" * 12,
                "effort": "high",
                "running": False,
                "last_activity_at": "2026-09-26T09:00:00",
            },
            {
                **base,
                "session_id": "c" * 12,
                "effort": "extra",
                "last_activity_at": "2026-09-20T09:00:00+00:00",
            },
            {**base, "session_id": "d" * 12, "effort": None, "last_activity_at": "not a moment"},
            {**base, "session_id": "e" * 12, "effort": None, "last_activity_at": None},
        ]
        page = po_page({"sessions": sessions, "models": {"claude": ["fable"]}}, request_id="r", now=now)
        main = page.split("<main>", 1)[1]
        self.assertNotIn("<table", main)
        self.assertNotIn('class="grid"', main)
        self.assertIn('<span class="id">aaaaaaaa</span>', page)
        self.assertIn(">2m ago</time>", page)
        self.assertIn('title="2026-09-26T09:00:00">3h ago</time>', page)
        self.assertIn(">2026-09-20</time>", page)
        self.assertIn("<span>not a moment</span>", page)
        self.assertIn("<span>—</span>", page)
        # Every row says its effort: a chosen one as bars and its word (`extra` under the name people
        # use), a row stored with `default` or none as hollow bars and "not set", never "CLI default".
        self.assertEqual(page.count('<span class="effort">'), 5)
        self.assertEqual(page.count('<span class="segs unset" aria-hidden="true">'), 3)
        self.assertEqual(page.count("<span>not set</span>"), 3)
        self.assertIn("<span>high</span>", page)
        self.assertIn("<span>xhigh</span>", page)
        self.assertNotIn("CLI default", page)
        self.assertEqual(page.count("turn running</span>"), 1)
        self.assertEqual(page.count('class="po-close"'), 5)

        closed = po_page(
            {"closed": True, "sessions": [{**base, "session_id": "f" * 12, "closed_at": now}]},
            request_id="r",
            now=now,
        )
        self.assertIn("<span>closed <time", closed)
        self.assertIn(">0s ago</time>", closed)
        self.assertNotIn('class="po-close"', closed)
        self.assertIn("this installation offers no model for a PO session", closed)

    def test_enter_sends_the_message_once_and_shift_enter_keeps_a_newline(self) -> None:
        from ummanu.web.pages import _PO_SESSION_SCRIPT

        page = self.page(self.create())
        self.assertIn("Enter to send, Shift+Enter for a new line", page)
        # The key handler is installed outside the running-or-queued polling branch.
        self.assertLess(
            _PO_SESSION_SCRIPT.index("addEventListener('keydown'"),
            _PO_SESSION_SCRIPT.index("if (seen && (seen.running || seen.queued > 0)) {"),
        )
        self.assertIn("draft.addEventListener('keydown'", page)
        for line in (
            "if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;",
            "if (event.isComposing || event.keyCode === 229) return;",
            "if (submitted || !draft.value.trim()) return;",
            "form.requestSubmit();",
            "if (submitted) { event.preventDefault(); return; }",
            "if (button) button.disabled = true;",
        ):
            with self.subTest(line=line):
                self.assertIn(line, _PO_SESSION_SCRIPT)

    def test_the_same_session_form_submitted_twice_is_one_session(self) -> None:
        first = self.create(request_id="create-twice")
        second = self.create(request_id="create-twice")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.sessions()), 1)

    # --- turns ------------------------------------------------------------------------------

    def test_a_message_runs_a_turn_and_the_feed_holds_the_owner_message_then_the_answer(self) -> None:
        session_id = self.create()
        sid = self.store.session(session_id).cli_session_id

        response = self.send(session_id, "remember 42", "message-1")

        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], f"/po/sessions/{session_id}")
        self.settle(session_id)
        self.assertEqual(
            self.feed(session_id),
            [(1, "owner", "remember 42"), (1, "agent", f"claude --session-id {sid}: remember 42")],
        )
        self.assertEqual(self.document(session_id)["last_turn"]["state"], po_store.COMPLETED)
        page = self.page(session_id)
        self.assertIn("remember 42", page)
        self.assertIn('data-state="completed"', page)
        self.assertNotIn("stop turn", page)

    def test_while_a_turn_runs_the_page_says_so_and_a_second_message_waits_in_the_queue(self) -> None:
        session_id = self.create("codex", "gpt-5.6-sol")
        self.assertEqual(self.send(session_id, "SLEEP please", "message-1").status, 303)
        self.spawned(1)

        document = self.document(session_id)
        self.assertTrue(document["running"])
        self.assertEqual(document["running_seq"], 1)
        self.assertEqual(self.feed(session_id), [(1, "owner", "SLEEP please")])
        self.assertEqual(self.layer.po_running_count()["running"], 1)
        page = self.page(session_id)
        self.assertIn('data-state="running"', page)
        self.assertIn("stop turn", page)

        waiting = self.send(session_id, "hurry up", "message-2")

        self.assertEqual(waiting.status, 303)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual([item["text"] for item in self.document(session_id)["queued"]], ["hurry up"])
        self.assertIn('data-state="queued"', self.page(session_id))
        self.assertEqual(self.calls(), 1)

        self.assertEqual(self.post(f"/po/sessions/{session_id}/stop", [("seq", "1")]).status, 303)
        self.settle(session_id)
        feed = self.feed(session_id)
        self.assertEqual(feed[1], (2, "owner", "hurry up"))
        self.assertEqual(feed[2][:2], (2, "agent"))
        self.assertIn("hurry up", feed[2][2])
        self.assertEqual(self.store.request("message-2").seq, 2)

    def test_stop_interrupts_the_running_turn_and_the_next_message_goes_through(self) -> None:
        session_id = self.create()
        sid = self.store.session(session_id).cli_session_id
        self.send(session_id, "SLEEP please", "message-1")
        self.spawned(1)

        stopped = self.post(f"/po/sessions/{session_id}/stop", [("seq", "1")])

        self.assertEqual(stopped.status, 303)
        turn = self.store.turn(session_id, 1)
        self.assertEqual((turn.state, turn.reason), (po_store.INTERRUPTED, "stopped by the owner"))
        page = self.page(session_id)
        self.assertIn('data-state="interrupted"', page)
        self.assertIn("stopped by the owner", page)

        self.assertEqual(self.send(session_id, "again", "message-2").status, 303)
        self.settle(session_id)
        self.assertEqual(self.store.turn(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.feed(session_id)[-1], (2, "agent", f"claude --resume {sid}: again"))

    def test_a_stop_form_for_a_turn_that_is_no_longer_running_stops_nothing(self) -> None:
        session_id = self.create()
        self.send(session_id, "hello", "message-1")
        self.settle(session_id)
        self.send(session_id, "SLEEP now", "message-2")
        self.spawned(1)

        self.assertEqual(self.post(f"/po/sessions/{session_id}/stop", [("seq", "1")]).status, 303)

        self.assertEqual(self.store.turn(session_id, 2).state, po_store.RUNNING)

    def test_the_same_message_form_submitted_twice_is_one_turn(self) -> None:
        running = self.create(request_id="create-running")
        for _ in range(2):
            self.assertEqual(self.send(running, "SLEEP once", "message-running").status, 303)
        self.spawned(1)
        self.assertEqual([turn.seq for turn in self.store.turns(running)], [1])

        finished = self.create("codex", "gpt-5.6-sol", request_id="create-finished")
        self.assertEqual(self.send(finished, "hello", "message-finished").status, 303)
        self.settle(finished)
        self.assertEqual(self.send(finished, "hello", "message-finished").status, 303)

        self.assertEqual([turn.seq for turn in self.store.turns(finished)], [1])
        self.assertEqual([role for _seq, role, _text in self.feed(finished)], ["owner", "agent"])
        self.assertEqual(self.calls(), 2)

    def test_a_form_sent_again_after_its_turn_failed_to_launch_is_that_turn_and_no_second_launch(
        self,
    ) -> None:
        session_id = self.create()
        self.runner.executables["claude"] = str(self.root / "no-such-claude")

        with mock.patch.object(self.runner, "_launch", wraps=self.runner._launch) as launch:
            first = self.send(session_id, "hello", "message-broken")
            second = self.send(session_id, "hello", "message-broken")
            replay = self.layer.po_send(request_id="message-broken", session_id=session_id, text="hello")

        # The service accepted the message; its turn then failed to launch.
        self.assertEqual(first.status, 303)
        self.assertEqual(second.status, 303)
        self.assertEqual(second.headers["Location"], f"/po/sessions/{session_id}")
        self.assertEqual(launch.call_count, 1)
        [turn] = self.store.turns(session_id)
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("could not start", turn.reason)
        request = self.store.request("message-broken")
        self.assertEqual((request.operation, request.session_id, request.seq), (po_store.SEND, session_id, 1))
        self.assertEqual((replay["seq"], replay["state"], replay["repeated"]), (1, po_store.FAILED, True))
        self.assertEqual(self.feed(session_id), [(1, "owner", "hello")])
        page = self.page(session_id)
        self.assertIn('data-state="failed"', page)
        self.assertIn("could not start", page)

    # --- one request id, one operation with fixed inputs ---------------------------------------

    def assert_request_conflict(self, response) -> None:
        self.assertEqual(response.status, 409)
        self.assertIn("refused (request_conflict)", response.body.decode())

    def test_a_request_id_that_created_a_session_is_refused_for_a_message(self) -> None:
        session_id = self.create(request_id="shared-form")

        with mock.patch.object(self.runner, "_launch", wraps=self.runner._launch) as launch:
            refused = self.send(session_id, "hello", "shared-form")

        self.assert_request_conflict(refused)
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(self.store.turns(session_id), [])
        self.assertEqual(self.feed(session_id), [])
        request = self.store.request("shared-form")
        self.assertEqual(
            (request.operation, request.session_id, request.seq), (po_store.SESSION_CREATE, session_id, None)
        )

    def test_a_request_id_that_sent_in_one_session_is_refused_in_another(self) -> None:
        first = self.create(request_id="create-first")
        second = self.create(request_id="create-second")
        self.assertEqual(self.send(first, "hello", "one-form").status, 303)
        self.settle(first)

        self.assert_request_conflict(self.send(second, "hello", "one-form"))

        self.assertEqual(self.store.turns(second), [])
        self.assertEqual(self.feed(second), [])
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.store.request("one-form").session_id, first)

    def test_the_same_request_id_with_other_text_is_refused_and_the_feed_is_unchanged(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "hello", "text-form").status, 303)
        self.settle(session_id)
        before = self.feed(session_id)

        self.assert_request_conflict(self.send(session_id, "hello, again", "text-form"))

        self.assertEqual(self.feed(session_id), before)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual(self.calls(), 1)

    def test_an_id_sent_while_another_turn_runs_waits_and_becomes_the_next_turn(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "SLEEP please", "running-form").status, 303)
        self.spawned(1)

        waiting = self.send(session_id, "later", "waiting-form")

        self.assertEqual(waiting.status, 303)
        self.assertIsNone(self.store.request("waiting-form"), "no turn is claimed while the session is busy")
        self.assertEqual([item.request_id for item in PoQueue(self.data).pending()], ["waiting-form"])
        self.assertEqual(
            self.send(session_id, "later", "waiting-form").status, 303, "a repeat is the same message"
        )
        self.assertEqual(self.post(f"/po/sessions/{session_id}/stop", [("seq", "1")]).status, 303)

        self.settle(session_id)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1, 2])
        self.assertEqual(self.store.request("waiting-form").seq, 2)
        self.assertEqual(PoQueue(self.data).pending(), [])

    def test_concurrent_claims_of_one_new_request_id_have_exactly_one_winner(self) -> None:
        workers = 8

        def claim_session(_index: int):
            barrier.wait()
            return self.store.claim_session(
                session_id=str(uuid.uuid4()),
                cli="claude",
                model="opus",
                cwd=str(self.data / "po"),
                cli_session_id=None,
                request_id="race-create",
            )

        barrier = threading.Barrier(workers)
        with ThreadPoolExecutor(workers) as pool:
            sessions = list(pool.map(claim_session, range(workers)))
        self.assertEqual(sum(created for _session, created in sessions), 1)
        self.assertEqual(len({session.session_id for session, _created in sessions}), 1)
        self.assertEqual(len(self.store.sessions()), 1)
        session_id = sessions[0][0].session_id

        def path(seq: int) -> Path:
            return self.root / f"{seq}.out"

        def claim_turn(index: int):
            barrier.wait()
            try:
                return self.store.claim_turn(
                    session_id, "same" if index % 2 else "other", path, request_id="race-send"
                )
            except po_store.RequestConflict:
                return None

        barrier = threading.Barrier(workers)
        with ThreadPoolExecutor(workers) as pool:
            turns = list(pool.map(claim_turn, range(workers)))
        winners = [turn for turn in turns if turn is not None and turn[1]]
        self.assertEqual(len(winners), 1)
        [entry] = self.store.feed(session_id)
        self.assertEqual(len(self.store.turns(session_id)), 1)
        self.assertEqual(turns.count(None), workers // 2)
        self.assertTrue(all(turn[0].seq == 1 for turn in turns if turn is not None))
        self.assertEqual(
            [index % 2 for index, turn in enumerate(turns) if turn is not None],
            [1 if entry.text == "same" else 0] * (workers // 2),
        )

    # --- the session list -------------------------------------------------------------------

    def seed(self, session_id: str, created: str, turns=(), feed=()) -> None:
        """A session at fixed times: `turns` are (seq, started, finished), `feed` is (seq, role, text, at)."""
        import psycopg

        self.store.create_session(
            session_id=session_id, cli="claude", model="opus", cwd="/", cli_session_id=None
        )
        with psycopg.connect(self.store.credentials.conninfo()) as connection:
            connection.execute(
                "UPDATE po_sessions SET created_at = %s WHERE session_id = %s", (created, session_id)
            )
            for seq, started, finished in turns:
                connection.execute(
                    "INSERT INTO po_turns (session_id, seq, started_at, finished_at, state, stdout_path) "
                    "VALUES (%s, %s, %s, %s, 'completed', '/dev/null')",
                    (session_id, seq, started, finished),
                )
            for seq, role, text, at in feed:
                connection.execute(
                    "INSERT INTO po_feed (session_id, turn_seq, role, text, created_at) VALUES (%s, %s, %s, %s, %s)",
                    (session_id, seq, role, text, at),
                )

    def test_the_session_list_carries_the_first_owner_message_and_is_newest_activity_first(self) -> None:
        # a: created first, but its turn finished last, after its newest feed entry.
        self.seed(
            "a",
            "2026-09-01T10:00:00Z",
            turns=[(1, "2026-09-01T10:01:00Z", "2026-09-05T10:00:00Z")],
            feed=[
                (1, "agent", "an agent spoke first", "2026-09-01T10:00:30Z"),
                (1, "owner", "the owner's first", "2026-09-01T12:00:00Z"),
                (1, "owner", "earlier time, later entry", "2026-09-01T10:00:10Z"),
            ],
        )
        # b: its feed entry is newer than its turn.
        self.seed(
            "b",
            "2026-09-02T10:00:00Z",
            turns=[(1, "2026-09-02T10:01:00Z", "2026-09-02T10:02:00Z")],
            feed=[(1, "owner", "hello b", "2026-09-03T10:00:00Z")],
        )
        # c: no turns, no feed, created after b's last activity.
        self.seed("c", "2026-09-04T10:00:00Z")
        # d: only an agent entry and the same last activity as c, created earlier; ties go to newer creation.
        self.seed(
            "d",
            "2026-09-01T09:00:00Z",
            turns=[(1, "2026-09-01T09:01:00Z", "2026-09-04T10:00:00Z")],
            feed=[(1, "agent", "only the agent", "2026-09-01T09:02:00Z")],
        )

        sessions = self.store.sessions()
        self.assertEqual([item.session_id for item in sessions], ["a", "c", "d", "b"])
        by_id = {item.session_id: item for item in sessions}
        self.assertEqual(by_id["a"].first_message, "the owner's first")
        self.assertEqual(by_id["b"].first_message, "hello b")
        self.assertIsNone(by_id["c"].first_message)
        self.assertIsNone(by_id["d"].first_message)
        self.assertEqual(
            {key: item.last_activity_at.isoformat() for key, item in by_id.items()},
            {
                "a": "2026-09-05T10:00:00+00:00",
                "b": "2026-09-03T10:00:00+00:00",
                "c": "2026-09-04T10:00:00+00:00",
                "d": "2026-09-04T10:00:00+00:00",
            },
        )

        document = self.layer.po_overview()
        self.assertEqual([item["session_id"] for item in document["sessions"]], ["a", "c", "d", "b"])
        first = document["sessions"][0]
        self.assertEqual(first["first_message"], "the owner's first")
        self.assertIsNone(first["first_message_metadata"], "historical inputs carry no service metadata")
        self.assertEqual(first["last_activity_at"], by_id["a"].last_activity_at.isoformat())
        self.assertEqual(first["created_at"], by_id["a"].created_at.isoformat())
        self.assertEqual(
            set(first),
            {
                "session_id",
                "title",
                "cli",
                "model",
                "created_at",
                "state",
                "closed_at",
                "closed_by",
                "effort",
                "resolved_model",
                "running",
                "first_message",
                "first_message_metadata",
                "last_activity_at",
            },
        )

    def test_a_row_shows_the_escaped_collapsed_start_of_the_first_message_or_no_message_yet(self) -> None:
        cyrillic = "Глянь  по обоим\n\tспринтам что происходит и какие действия требуются, " + "я" * 40
        self.seed(
            "long",
            "2026-09-04T10:00:00Z",
            turns=[(1, "2026-09-04T10:00:00Z", "2026-09-04T10:00:00Z")],
            feed=[(1, "owner", cyrillic, "2026-09-04T10:00:00Z")],
        )
        self.seed(
            "html",
            "2026-09-03T10:00:00Z",
            turns=[(1, "2026-09-03T10:00:00Z", "2026-09-03T10:00:00Z")],
            feed=[(1, "owner", "<b>bold</b> & **not markdown**", "2026-09-03T10:00:00Z")],
        )
        self.seed(
            "short",
            "2026-09-02T10:00:00Z",
            turns=[(1, "2026-09-02T10:00:00Z", "2026-09-02T10:00:00Z")],
            feed=[(1, "owner", "exactly  fits", "2026-09-02T10:00:00Z")],
        )
        self.seed("none", "2026-09-01T10:00:00Z")

        response = self.get("/po")
        self.assertEqual(response.status, 200)
        page = response.body.decode()
        collapsed = " ".join(cyrillic.split())
        shown = collapsed[:79] + "…"
        self.assertEqual(len(shown), 80)
        self.assertIn(f'<a class="title" href="/po/sessions/long">{shown}</a>', page)
        self.assertNotIn(collapsed[:80], page)
        self.assertIn(
            '<a class="title" href="/po/sessions/html">&lt;b&gt;bold&lt;/b&gt; &amp; **not markdown**</a>',
            page,
        )
        self.assertNotIn("<b>bold</b>", page)
        self.assertIn('<a class="title" href="/po/sessions/short">exactly fits</a>', page)
        self.assertIn(
            '<a class="title" href="/po/sessions/none"><span class="empty">no message yet</span></a>', page
        )
        self.assertIn("2026-09-04T10:00:00+00:00", page)
        self.assertIn('<span class="id">long</span>', page)
        order = [page.index(f"/po/sessions/{key}") for key in ("long", "html", "short", "none")]
        self.assertEqual(order, sorted(order))

    # --- closing a session ------------------------------------------------------------------

    def close(self, session_id: str):
        return self.post(f"/po/sessions/{session_id}/close", [])

    def requests_rows(self) -> int:
        import psycopg

        with psycopg.connect(self.store.credentials.conninfo()) as connection:
            return connection.execute("SELECT count(*) FROM po_requests").fetchone()[0]

    def test_closing_an_open_session_moves_it_from_the_default_list_to_the_closed_one(self) -> None:
        kept = self.create(request_id="create-kept")
        session_id = self.create(request_id="create-closed")
        self.assertEqual(self.send(session_id, "hello", "message-1").status, 303)
        self.settle(session_id)
        overview = self.get("/po").body.decode()
        self.assertIn(f'action="/po/sessions/{session_id}/close"', overview)
        self.assertIn(f'action="/po/sessions/{kept}/close"', overview)
        self.assertIn('href="/po?closed=1">closed sessions (0)</a>', overview)
        self.assertIn(f'action="/po/sessions/{session_id}/close"', self.page(session_id))

        response = self.close(session_id)

        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/po")
        session = self.store.session(session_id)
        self.assertEqual((session.state, session.closed_by), (po_store.SESSION_CLOSED, "owner"))
        self.assertIsNotNone(session.closed_at)
        self.assertEqual([item.session_id for item in self.store.sessions()], [kept])
        closed = self.store.sessions(po_store.SESSION_CLOSED)
        self.assertEqual([item.session_id for item in closed], [session_id])
        self.assertEqual(closed[0].first_message, "hello")
        self.assertEqual(closed[0].closed_at, session.closed_at)

        overview = self.get("/po").body.decode()
        self.assertNotIn(f"/po/sessions/{session_id}", overview)
        self.assertIn(f"/po/sessions/{kept}", overview)
        self.assertIn('href="/po?closed=1">closed sessions (1)</a>', overview)
        self.assertIn('id="po-new"', overview)
        listed = self.app.handle("GET", "/po", query="closed=1", headers=self.headers())
        self.assertEqual(listed.status, 200)
        listed_page = listed.body.decode()
        self.assertIn(f'<a class="title" href="/po/sessions/{session_id}">hello</a>', listed_page)
        self.assertNotIn(f"/po/sessions/{kept}", listed_page)
        self.assertIn(session.closed_at.isoformat(), listed_page)
        self.assertIn('<a class="more" href="/po">open sessions</a>', listed_page)
        self.assertNotIn("/close", listed_page)
        self.assertEqual(self.layer.po_overview(closed=True)["closed_count"], 1)

        page = self.page(session_id)
        self.assertIn("hello", page)
        self.assertIn(f"closed {session.closed_at.isoformat()} by owner", page)
        self.assertNotIn('id="po-send"', page)
        self.assertNotIn("/close", page)
        self.assertEqual(self.document(session_id)["session"]["closed_by"], "owner")

    def test_closing_while_a_turn_runs_is_refused_and_writes_nothing(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "SLEEP please", "message-1").status, 303)
        self.spawned(1)
        self.assertNotIn("/close", self.page(session_id))

        refused = self.close(session_id)

        self.assertEqual(refused.status, 409)
        body = refused.body.decode()
        self.assertIn("not closed: a turn is still running in this session", body)
        self.assertIn('id="po-send"', body)
        session = self.store.session(session_id)
        self.assertEqual(
            (session.state, session.closed_at, session.closed_by), (po_store.SESSION_OPEN, None, None)
        )
        with self.assertRaises(po_store.TurnInProgress):
            self.store.close_session(session_id, "owner")
        self.assertEqual(self.store.session(session_id), session)
        self.assertEqual(self.store.turn(session_id, 1).state, po_store.RUNNING)
        self.assertEqual(self.layer.po_running_count()["running"], 1)
        self.assertIn("1 PO turn running", self.app.handle("GET", "/").body.decode())

    def test_closing_twice_keeps_the_first_close(self) -> None:
        session_id = self.create()
        first = self.store.close_session(session_id, "owner")

        self.assertEqual(self.close(session_id).status, 303)
        again = self.store.close_session(session_id, "someone-else")

        self.assertEqual(again, first)
        self.assertEqual(self.store.session(session_id), first)
        self.assertEqual(self.store.session_count(po_store.SESSION_CLOSED), 1)

    def test_closing_an_unknown_session_is_404(self) -> None:
        response = self.close("no-such-session")

        self.assertEqual(response.status, 404)
        with self.assertRaises(po_store.SessionNotFound):
            self.store.close_session("no-such-session", "owner")
        self.assertEqual(self.store.session_count(po_store.SESSION_CLOSED), 0)

    def test_a_close_form_with_a_field_is_refused(self) -> None:
        session_id = self.create()
        self.assertEqual(self.post(f"/po/sessions/{session_id}/close", [("seq", "1")]).status, 400)
        self.assertEqual(self.store.session(session_id).state, po_store.SESSION_OPEN)

    def test_a_message_into_a_closed_session_is_refused_with_nothing_written(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "hello", "message-1").status, 303)
        self.settle(session_id)
        self.assertEqual(self.close(session_id).status, 303)
        feed, calls, requests = self.feed(session_id), self.calls(), self.requests_rows()

        refused = self.send(session_id, "again", "message-2")

        self.assertEqual(refused.status, 409)
        body = refused.body.decode()
        self.assertIn("not sent: this session is closed", body)
        self.assertNotIn('id="po-send"', body)
        with self.assertRaises(po_store.SessionClosed):
            self.store.claim_turn(session_id, "again", lambda seq: self.root / f"turn-{seq}")
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual(self.feed(session_id), feed)
        self.assertEqual(self.requests_rows(), requests)
        self.assertIsNone(self.store.request("message-2"))
        self.assertEqual(self.calls(), calls)

        # The send made before the close is still answered by its recorded turn, and launches nothing.
        replay = self.send(session_id, "hello", "message-1")
        self.assertEqual(replay.status, 303)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual(self.calls(), calls)

    def test_the_audit_check_holds_closed_exactly_when_who_and_when_are_set(self) -> None:
        import psycopg

        session_id = self.create()
        statements = (
            "UPDATE po_sessions SET state = 'closed' WHERE session_id = %s",
            "UPDATE po_sessions SET state = 'closed', closed_at = now() WHERE session_id = %s",
            "UPDATE po_sessions SET state = 'closed', closed_by = 'owner' WHERE session_id = %s",
            "UPDATE po_sessions SET closed_at = now(), closed_by = 'owner' WHERE session_id = %s",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with (
                    psycopg.connect(self.store.credentials.conninfo()) as connection,
                    self.assertRaises(psycopg.errors.CheckViolation) as raised,
                ):
                    connection.execute(statement, (session_id,))
                self.assertEqual(raised.exception.diag.constraint_name, "po_session_closed_iff_audited")
        self.assertEqual(self.store.session(session_id).state, po_store.SESSION_OPEN)

    # --- the composer's control row -----------------------------------------------------------

    def control_row(self, page: str) -> str:
        """The one `po-controls` row of a session page; a second one would be a duplicated button."""
        self.assertEqual(page.count('<div class="po-controls">'), 1, "one control row, not two")
        start = page.index('<div class="po-controls">')
        return page[start : page.index("</div></div>", start) + len("</div></div>")]

    def new_session_form(self, page: str) -> list[tuple[str, str]]:
        """The fields the `new session` form on a session page would post, in the order it lists them."""
        form = page[
            page.index('<form class="po-new"') : page.index("</form>", page.index('<form class="po-new"'))
        ]
        self.assertIn('action="/po/sessions"', form)
        return re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)

    def test_close_and_new_session_sit_by_send_and_no_longer_under_the_feed(self) -> None:
        """The feed runs newest first, so a control at its end is a control behind the whole scroll."""
        session_id = self.create()
        self.assertEqual(self.send(session_id, "hello", "message-1").status, 303)
        self.settle(session_id)

        page = self.page(session_id)
        row = self.control_row(page)
        self.assertIn('<button type="submit" form="po-send">send</button>', row)
        self.assertIn("new session", row)
        self.assertIn(f'action="/po/sessions/{session_id}/close"', row)
        # Everything that is not `send` is at the far end of the row, behind `send` in the markup.
        self.assertLess(row.index(">send<"), row.index('<div class="aside">'))
        # The close is the page's only one and it is above the feed, not after it.
        self.assertEqual(page.count("/close"), 1)
        self.assertLess(page.index("/close"), page.index('<ol class="po-feed"'))
        self.assertLess(page.index('<div class="po-controls">'), page.index('<ol class="po-feed"'))
        # The feed panel is the feed and nothing else: no form trails it.
        self.assertNotIn("<form", page[page.index('<ol class="po-feed"') :])

    def test_stop_turn_is_in_the_same_row_while_a_turn_runs_and_close_is_not_offered(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "SLEEP please", "message-1").status, 303)
        self.spawned(1)

        row = self.control_row(self.page(session_id))

        self.assertIn(f'action="/po/sessions/{session_id}/stop"', row)
        self.assertIn("stop turn", row)
        self.assertNotIn("/close", row)
        self.assertIn("new session", row)

    def test_the_send_button_reaches_its_form_from_outside_it_because_a_form_holds_no_form(self) -> None:
        """`close` and `new session` are forms of their own, so `send` is bound by `form=` instead."""
        from ummanu.web.pages import _PO_SESSION_SCRIPT

        page = self.page(self.create())
        send = page[page.index('<form class="sprint" id="po-send"') : page.index("</form>")]
        self.assertNotIn("<button", send, "the submit button is in the control row, not in the form")
        # The message form is closed before the row begins: the other forms are its siblings.
        self.assertLess(page.index("</form>"), page.index('<div class="po-controls">'))
        self.assertIn('<button type="submit" form="po-send">send</button>', page)
        # The script still finds the button to disable it, now by that association.
        self.assertIn(
            "const button = document.querySelector('button[form=\"po-send\"]');", _PO_SESSION_SCRIPT
        )

    def test_the_form_opens_a_session_with_the_chosen_effort_and_every_page_names_it(self) -> None:
        response = self.post(
            "/po/sessions",
            [("request_id", "create-high"), ("cli", "claude"), ("model", "opus"), ("effort", "high")],
        )
        self.assertEqual(response.status, 303, response.body.decode())
        session_id = response.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(self.document(session_id)["session"]["effort"], "high")

        listing = self.get("/po").body.decode()
        self.assertIn('aria-label="reasoning effort"', listing)
        self.assertIn('<span class="effort">', listing)
        self.assertIn("<span>high</span>", listing)
        page = self.page(session_id)
        self.assertIn('<span class="head-chip"', page)
        self.assertEqual(dict(self.new_session_form(page))["effort"], "high")

    def test_an_effort_the_installation_does_not_offer_is_refused_on_the_form(self) -> None:
        response = self.post(
            "/po/sessions",
            [("request_id", "create-odd"), ("cli", "claude"), ("model", "opus"), ("effort", "turbo")],
        )
        self.assertEqual(response.status, 400)
        self.assertIn("turbo", response.body.decode())

    def test_a_create_without_an_effort_or_with_default_is_refused_on_the_form_and_opens_nothing(
        self,
    ) -> None:
        for fields in (
            [("request_id", "no-effort"), ("cli", "claude"), ("model", "opus")],
            [("request_id", "empty-effort"), ("cli", "claude"), ("model", "opus"), ("effort", "")],
            [("request_id", "default-effort"), ("cli", "claude"), ("model", "opus"), ("effort", "default")],
        ):
            with self.subTest(fields=fields):
                response = self.post("/po/sessions", fields)
                body = response.body.decode()
                self.assertEqual(response.status, 400)
                self.assertIn("refused (validation)", body)
                self.assertIn(
                    "a new PO session needs an explicit effort; this installation offers for claude: "
                    "high, low, medium, xhigh, max",
                    body,
                )
                # The form is drawn again with a real effort preselected, never `default`.
                self.assertIn('id="po-new"', body)
                self.assertIn('<option value="high" data-cli="claude" selected>high</option>', body)
                self.assertNotIn('value="default"', body)
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.calls(), 0)

    def legacy_session(self, cli: str = "codex", model: str = "gpt-5.6-sol") -> str:
        """A session row as a release before migration 0015 left it: no effort written, the column's `default`."""
        import psycopg

        session_id = str(uuid.uuid4())
        with psycopg.connect(self.store.credentials.conninfo()) as connection:
            connection.execute(
                "INSERT INTO po_sessions (session_id, cli, model, cwd, created_at, state, cli_session_id) "
                "VALUES (%s, %s, %s, %s, now(), %s, %s)",
                (session_id, cli, model, str(self.runner.workspace), po_store.SESSION_OPEN, None),
            )
        return session_id

    def test_a_session_stored_with_default_reads_not_set_and_its_new_session_opens_at_the_first_effort(
        self,
    ) -> None:
        session_id = self.legacy_session()
        # The API keeps the stored value; only the pages word it.
        self.assertEqual(self.document(session_id)["session"]["effort"], po_store.DEFAULT_EFFORT)

        listing = self.get("/po").body.decode()
        row = listing[listing.index(f"/po/sessions/{session_id}") :]
        row = row[: row.index("</li>")]
        self.assertIn('<span class="segs unset" aria-hidden="true">', row)
        self.assertIn("<span>not set</span>", row)
        page = self.page(session_id)
        chip = page[page.index('<span class="head-chip"') :]
        self.assertIn("<span>not set</span>", chip[: chip.index("</span></span>") + 14])
        for shown in (listing, page):
            self.assertNotIn("CLI default", shown)

        # `new session` never sends `default`: it opens at the CLI's first offered effort and says so.
        self.assertIn("effort not set on this session; the new one opens at high", page)
        fields = self.new_session_form(page)
        self.assertEqual(dict(fields)["effort"], "high")
        opened = self.post("/po/sessions", fields)
        self.assertEqual(opened.status, 303, opened.body.decode())
        fresh = self.store.session(opened.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual((fresh.cli, fresh.model, fresh.effort), ("codex", "gpt-5.6-sol", "high"))

        # The old session still takes a message, and still closes.
        self.assertEqual(self.send(session_id, "hello", "legacy-1").status, 303)
        self.settle(session_id)
        self.assertEqual(self.close(session_id).status, 303)
        self.assertEqual(self.store.session(session_id).state, po_store.SESSION_CLOSED)

    def test_a_new_session_from_a_session_page_reuses_its_cli_and_model_and_leaves_it_open(self) -> None:
        """`new session` posts the `/po` form's own route; the session being read is not touched."""
        session_id = self.create("codex", "gpt-5.6-sol", request_id="create-codex", effort="xhigh")
        self.assertEqual(self.send(session_id, "hello", "message-1").status, 303)
        self.settle(session_id)
        fields = self.new_session_form(self.page(session_id))
        self.assertEqual([name for name, _ in fields], ["request_id", "cli", "model", "effort"])
        self.assertEqual(dict(fields)["effort"], "xhigh")
        self.assertEqual(dict(fields)["cli"], "codex")
        self.assertEqual(dict(fields)["model"], "gpt-5.6-sol")

        opened = self.post("/po/sessions", fields)

        self.assertEqual(opened.status, 303)
        other = opened.headers["Location"].rsplit("/", 1)[1]
        self.assertNotEqual(other, session_id)
        fresh = self.store.session(other)
        self.assertEqual((fresh.cli, fresh.model, fresh.effort), ("codex", "gpt-5.6-sol", "xhigh"))
        # Nothing happened to the session the owner was reading: both are open and both are listed.
        self.assertEqual(self.store.session(session_id).state, po_store.SESSION_OPEN)
        self.assertEqual(fresh.state, po_store.SESSION_OPEN)
        self.assertEqual(self.store.session_count(po_store.SESSION_CLOSED), 0)
        overview = self.get("/po").body.decode()
        for listed in (session_id, other):
            self.assertIn(listed[:8], overview)
        # Pressed twice, the same page opens one session: the create carries a request id like any other.
        self.assertEqual(self.post("/po/sessions", fields).headers["Location"].rsplit("/", 1)[1], other)
        self.assertEqual(len(self.store.sessions()), 2)

    def test_the_new_session_id_is_not_the_id_the_message_box_already_spent(self) -> None:
        """One page mints one id, and an id belongs to one operation: the create takes its own."""
        session_id = self.create()
        page = self.page(session_id)
        sent = re.search(r'id="po-send".*?name="request_id" value="([^"]+)"', page, re.DOTALL).group(1)
        fields = dict(self.new_session_form(page))
        self.assertEqual(fields["request_id"], f"{sent}-new-session")

        self.assertEqual(self.send(session_id, "hello", sent).status, 303)
        self.settle(session_id)
        opened = self.post("/po/sessions", list(fields.items()))

        self.assertEqual(opened.status, 303, opened.body.decode())
        self.assertEqual(len(self.store.sessions()), 2)

    def test_a_closed_session_still_offers_a_new_one_and_never_a_close(self) -> None:
        session_id = self.create()
        self.assertEqual(self.close(session_id).status, 303)

        page = self.page(session_id)
        row = self.control_row(page)

        self.assertNotIn("/close", page)
        self.assertNotIn(">send<", row)
        self.assertNotIn('id="po-send"', page)
        self.assertIn("new session", row)
        opened = self.post("/po/sessions", self.new_session_form(page))
        self.assertEqual(opened.status, 303)
        self.assertNotEqual(opened.headers["Location"].rsplit("/", 1)[1], session_id)

    # --- the session title (secretary-1782) ---------------------------------------------------

    def rename(self, session_id: str, title: str):
        return self.post(f"/po/sessions/{session_id}/title", [("title", title)])

    def test_the_store_sets_a_title_by_one_rule(self) -> None:
        session_id = self.create()
        for given, stored in (("  Roadmap  ", "Roadmap"), ("Roadmap", "Roadmap"), ("   ", None)):
            with self.subTest(given=given):
                self.assertEqual(self.store.set_title(session_id, given).title, stored)
                self.assertEqual(self.store.session(session_id).title, stored)
        self.store.set_title(session_id, "kept")
        for given in ("two\nlines", "x" * 121):
            with self.subTest(given=given), self.assertRaises(po_store.TitleRefused):
                self.store.set_title(session_id, given)
        self.assertEqual(self.store.session(session_id).title, "kept")
        with self.assertRaises(po_store.SessionNotFound):
            self.store.set_title("no-such", "t")
        self.assertEqual(self.close(session_id).status, 303)
        self.assertEqual(self.store.set_title(session_id, "after close").title, "after close")
        self.assertEqual(self.store.sessions(po_store.SESSION_CLOSED)[0].title, "after close")

    def test_the_title_form_renames_the_session_and_every_read_shows_it(self) -> None:
        titled = self.create(request_id="create-titled")
        untitled = self.create(request_id="create-untitled")
        self.assertEqual(self.send(titled, "the first thing I said", "m-1").status, 303)
        self.settle(titled)
        page = self.page(titled)
        self.assertIn(f'action="/po/sessions/{titled}/title"', page)
        self.assertIn('name="title"', page)

        response = self.rename(titled, "  Sprint <planning>  ")

        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], f"/po/sessions/{titled}")
        self.assertEqual(self.document(titled)["session"]["title"], "Sprint <planning>")
        self.assertIsNone(self.document(untitled)["session"]["title"])
        page = self.page(titled)
        self.assertIn(f'<h1>Sprint &lt;planning&gt; <span class="id">{titled[:8]}</span></h1>', page)
        self.assertIn('value="Sprint &lt;planning&gt;"', page)
        self.assertIn(f"<h1>PO session {untitled[:8]}</h1>", self.page(untitled))
        listing = self.get("/po").body.decode()
        self.assertIn(
            f'<li class="titled"><a class="title" href="/po/sessions/{titled}">Sprint &lt;planning&gt;</a>'
            '<div class="first">the first thing I said</div><div class="meta">',
            listing,
        )
        # An untitled row is as it was: the first message is its link.
        self.assertIn(
            f'<li><a class="title" href="/po/sessions/{untitled}"><span class="empty">no message yet</span></a>',
            listing,
        )
        self.assertEqual(self.layer.po_overview()["sessions"][0]["title"], "Sprint <planning>")

        # A repeat is the same answer; an empty title clears it.
        self.assertEqual(self.rename(titled, "Sprint <planning>").status, 303)
        self.assertEqual(self.rename(titled, "").status, 303)
        self.assertIsNone(self.document(titled)["session"]["title"])
        self.assertIn(f"<h1>PO session {titled[:8]}</h1>", self.page(titled))

    def test_a_refused_title_renders_the_session_with_the_reason_and_the_text_kept(self) -> None:
        session_id = self.create()
        self.rename(session_id, "kept")

        response = self.rename(session_id, "z" * 121)

        self.assertEqual(response.status, 400)
        page = response.body.decode()
        self.assertIn("title not saved (validation).", page)
        self.assertIn("at most 120 characters", page)
        self.assertIn(f'value="{"z" * 121}"', page)
        self.assertEqual(self.store.session(session_id).title, "kept")
        self.assertEqual(self.rename("no-such", "t").status, 404)
        self.assertEqual(
            self.post(f"/po/sessions/{session_id}/title", [("title", "t"), ("seq", "1")]).status, 400
        )
        self.assertEqual(self.store.session(session_id).title, "kept")

    def test_a_closed_session_is_renamed_from_its_page(self) -> None:
        session_id = self.create()
        self.assertEqual(self.close(session_id).status, 303)
        self.assertIn(f'action="/po/sessions/{session_id}/title"', self.page(session_id))

        self.assertEqual(self.rename(session_id, "archived").status, 303)

        self.assertEqual(self.store.session(session_id).title, "archived")
        listed = self.app.handle("GET", "/po", query="closed=1", headers=self.headers())
        self.assertIn(f'href="/po/sessions/{session_id}">archived</a>', listed.body.decode())

    def po_rename_cli(self, *arguments: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        import contextlib
        import io

        from ummanu.cli import main

        out, err = io.StringIO(), io.StringIO()
        environment = {key: value for key, value in os.environ.items() if key != "UMMANU_PO_SESSION"}
        with (
            mock.patch.dict(os.environ, {**environment, **(env or {})}, clear=True),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            status = main(
                ["po", "rename", "--instance", str(self.root), "--data-dir", str(self.data), *arguments]
            )
        return status, out.getvalue(), err.getvalue()

    def test_the_cli_renames_the_named_session_or_the_turns_own(self) -> None:
        named = self.create(request_id="create-named")
        own = self.create(request_id="create-own")

        status, out, _ = self.po_rename_cli("--title", "Named", "--session", named)
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(out), {"kind": "po_session_renamed", "session_id": named, "title": "Named"}
        )
        # Inside a PO turn the session comes from the turn's environment; a repeat changes nothing.
        for _ in range(2):
            status, out, _ = self.po_rename_cli("--title", "Own", env={"UMMANU_PO_SESSION": own})
            self.assertEqual((status, json.loads(out)["session_id"]), (0, own))
        self.assertEqual((self.store.session(named).title, self.store.session(own).title), ("Named", "Own"))

    def test_the_cli_refuses_with_the_reason_and_web_runs_statuses(self) -> None:
        session_id = self.create()
        self.store.set_title(session_id, "kept")
        for arguments, status, code in (
            (("--title", "t"), 2, "validation"),
            (("--title", "a\nb", "--session", session_id), 2, "validation"),
            (("--title", "t", "--session", "no-such"), 2, "not_found"),
        ):
            with self.subTest(arguments=arguments):
                answered, out, err = self.po_rename_cli(*arguments)
                self.assertEqual((answered, out), (status, ""))
                self.assertEqual(json.loads(err)["error"]["code"], code)
        self.assertEqual(self.store.session(session_id).title, "kept")
        # No PO service listening under this data dir: the backend status, nothing written.
        elsewhere = self.root / "no-service"
        elsewhere.mkdir()
        answered, _, err = self.po_rename_cli(
            "--title", "t", "--session", session_id, "--data-dir", str(elsewhere)
        )
        self.assertEqual((answered, json.loads(err)["error"]["code"]), (1, "backend_unavailable"))
        self.assertEqual(self.store.session(session_id).title, "kept")


if __name__ == "__main__":
    unittest.main()
