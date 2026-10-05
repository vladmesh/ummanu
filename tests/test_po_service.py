"""The PO service (`ummanu.po.service`): the durable queue, one turn per session, restarts, the socket.

Unit-level: the fake `claude`/`codex` of `tests.po_cli_fakes` run as real processes, the board store is
the in-memory `tests.po_fake_store`, and a "restart" is a new `PoService` over the same board and data
directory after the old one's store connection was cut (`FakePoStore.crash`) and, where the scenario
says so, its turn processes killed. No PostgreSQL, no Docker, no systemd.
"""

from __future__ import annotations

import ast
import getpass
import inspect
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock
from urllib.parse import urlencode

from ummanu import upgrade
from ummanu.backup_policy import FULL_POLICY, should_skip_data_entry
from ummanu.host import (
    SHIPPED_PACKAGING_ROOT,
    SystemdLayout,
    build_doctor_expectations,
    build_plan,
    load_packaged_units,
    render_systemd_unit,
)
from ummanu.host_apply import UnitProcessIdentity
from ummanu.po import client as po_client
from ummanu.po import runner as po_runner
from ummanu.po import service as po_service
from ummanu.po import store as po_store
from ummanu.po import token as po_token
from ummanu.po.client import PoServiceClient, ServiceUnavailable
from ummanu.po.queue import PoQueue, QueueError, queue_dir
from ummanu.po.runner import (
    RERUN_INTERRUPTED_REASON,
    RERUN_REASON,
    STOPPED_REASON,
    PoRunner,
    RunnerError,
    still_running,
)
from ummanu.po.service import PoService, ServiceStartError, listening
from ummanu.po.sprints import SprintRecord, WhyDocument, find_why_documents, why_document_label
from ummanu.web.app import WebApp
from ummanu.webproto.errors import (
    NOTHING_WRITTEN,
    PoOutcomeUnknown,
    PoRequestConflict,
    PoSessionClosed,
    PoSessionNotFound,
    PoTurnInProgress,
    RuntimeUnavailable,
    ValidationRefused,
)
from ummanu.webproto.po_auth import PoTokenLayer
from ummanu.webproto.po_ops import PoLayer
from tests.fakes.upgrade import FakeUnitInstaller
from tests.po_cli_fakes import FAKE_CLAUDE, FAKE_CODEX, eventually, unscoped_test_launch
from tests.po_fake_store import FakeBoard, FakePoStore, FakeSprints
from tests.web_fakes import Recording
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.local_pty.client import LocalPtySpawnError
from ummanu.runtime.head.memory import MemoryScopeError

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"claude": ("opus",), "codex": ("gpt-5.6-sol",)}


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


class ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        # A short root: the service's Unix socket lives under it.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="po-")))
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.executables = {}
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            self.executables[name] = str(path)
        self.log = self.root / "fake.log"
        self.gate = self.log.with_name(self.log.name + ".gate")
        self.board = FakeBoard()
        self.services: list[PoService] = []
        self.addCleanup(self.kill_everything)

    # --- one "process" of the service ------------------------------------------------------

    def service(self, *, run: bool = True, **options) -> PoService:
        """A fresh service process over the shared board and data dir, started (and its loop running).

        `options` go to `PoService` (its sprints and models, for the sprint-session resolver).
        """
        codex_home = str(self.root / "codex-home")
        runner = PoRunner(
            FakePoStore(self.board),
            self.data,
            executables=self.executables,
            turn_launcher=unscoped_test_launch,
            env={
                **os.environ,
                "FAKE_LOG": str(self.log),
                "CODEX_HOME": codex_home,
                "FAKE_CODEX_HOME": codex_home,
            },
        )
        service = PoService(runner, data_dir=self.data, **options)
        self.services.append(service)
        self.start_lines = service.start()
        if run:
            thread = threading.Thread(target=service.run, kwargs={"tick": 0.05, "say": lambda _line: None})
            thread.start()
            service.thread = thread  # type: ignore[attr-defined]
            self.addCleanup(thread.join, 10)
            self.addCleanup(service.stop)
        return service

    def crash(self, service: PoService, *, kill: bool = True) -> None:
        """The service process dies: its store connection is gone and, under systemd, its turns with it."""
        running = service.store.running_turns()
        service.store.crash()
        service.stop()
        if kill:
            # As systemd does before it starts the unit again: the control group is gone.
            for turn in running:
                if turn.pid:
                    self.kill_group(turn.pid)
                    eventually(
                        lambda turn=turn: not still_running(turn.pid, turn.process_identity),
                        "a killed turn process kept running",
                    )

    @staticmethod
    def kill_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_everything(self) -> None:
        self.gate.touch()
        for turn in self.store().running_turns():
            if turn.pid:
                self.kill_group(turn.pid)
        for service in self.services:
            service.stop()
            for live in list(service.runner._live.values()):
                self.kill_group(live.process.pid)

    def store(self) -> FakePoStore:
        return FakePoStore(self.board)

    # --- reading -----------------------------------------------------------------------------

    def session(
        self, service: PoService, cli: str = "claude", model: str = "opus", request_id: str | None = None
    ) -> str:
        created = service.create_session(
            cli=cli,
            model=model,
            effort="high",
            request_id=request_id or f"c-{cli}-{len(self.board.sessions)}",
        )
        return created["session_id"]

    def turns(self, session_id: str) -> list[po_store.Turn]:
        return self.store().turns(session_id)

    def feed(self, session_id: str) -> list[tuple[int, str, str]]:
        return [(entry.turn_seq, entry.role, entry.text) for entry in self.store().feed(session_id)]

    def reached_gate(self, session_id: str, seq: int, attempts: int = 1) -> None:
        """The fake of turn `seq` printed its stream up to the gate (on its `attempts`-th launch)."""
        stdout = self.data / "po-runs" / session_id / f"turn-{seq:04d}.stdout"
        eventually(
            lambda: stdout.exists() and stdout.read_text().count("TOOL-CALL-SECRET") >= attempts,
            f"turn {seq} never reached its gate",
        )

    def settled(self, session_id: str, seq: int) -> po_store.Turn:
        eventually(
            lambda: (
                len(self.turns(session_id)) >= seq
                and self.turns(session_id)[seq - 1].state != po_store.RUNNING
            ),
            f"turn {seq} never settled",
        )
        return self.turns(session_id)[seq - 1]

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def queued(self) -> list[str]:
        return [item.text for item in PoQueue(self.data).pending()]


class QueueOrderTests(ServiceFixture):
    def test_two_inputs_for_one_session_run_one_after_the_other_never_together(self) -> None:
        service = self.service()
        session_id = self.session(service)

        first = service.submit(session_id=session_id, text="GATE first", request_id="m-1")
        second = service.submit(session_id=session_id, text="second", request_id="m-2")

        self.assertEqual((first["queued"], first["seq"], first["state"]), (False, 1, po_store.RUNNING))
        self.assertEqual((second["queued"], second["seq"]), (True, None))
        self.reached_gate(session_id, 1)
        self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
        self.assertEqual(self.queued(), ["second"])
        self.assertEqual(len(self.calls()), 1)

        self.gate.touch()
        one, two = self.settled(session_id, 1), self.settled(session_id, 2)

        self.assertEqual((one.state, two.state), (po_store.COMPLETED, po_store.COMPLETED))
        self.assertLess(one.finished_at, two.started_at)
        self.assertEqual(self.queued(), [])
        self.assertEqual(
            [role for _seq, role, _text in self.feed(session_id)], ["owner", "agent", "owner", "agent"]
        )

    def test_inputs_for_two_sessions_run_at_the_same_time(self) -> None:
        service = self.service()
        claude = self.session(service, "claude", "opus")
        codex = self.session(service, "codex", "gpt-5.6-sol")

        service.submit(session_id=claude, text="GATE a", request_id="a")
        service.submit(session_id=codex, text="GATE b", request_id="b")

        self.reached_gate(claude, 1)
        self.reached_gate(codex, 1)
        self.assertEqual(
            sorted(turn.session_id for turn in self.store().running_turns()), sorted([claude, codex])
        )
        self.gate.touch()
        self.assertEqual(self.settled(claude, 1).state, po_store.COMPLETED)
        self.assertEqual(self.settled(codex, 1).state, po_store.COMPLETED)

    def test_a_request_id_answers_what_it_made_and_is_refused_for_anything_else(self) -> None:
        service = self.service()
        session_id = self.session(service)
        other = self.session(service, request_id="other-session")
        service.submit(session_id=session_id, text="GATE hold", request_id="m-1")
        service.submit(session_id=session_id, text="later", request_id="m-2")

        running = service.submit(session_id=session_id, text="GATE hold", request_id="m-1")
        waiting = service.submit(session_id=session_id, text="later", request_id="m-2")

        self.assertEqual((running["repeated"], running["seq"], running["queued"]), (True, 1, False))
        self.assertEqual((waiting["repeated"], waiting["queued"]), (True, True))
        for session, text, request_id in (
            (session_id, "other text", "m-1"),
            (other, "later", "m-2"),
            (session_id, "x", "other-session"),
        ):
            with self.subTest(request_id=request_id, text=text), self.assertRaises(po_store.RequestConflict):
                service.submit(session_id=session, text=text, request_id=request_id)
        with self.assertRaises(po_store.SessionNotFound):
            service.submit(session_id="no-such-session", text="x", request_id="m-9")
        self.assertEqual(self.queued(), ["later"])

        self.gate.touch()
        self.settled(session_id, 2)
        self.assertEqual(service.submit(session_id=session_id, text="later", request_id="m-2")["seq"], 2)

    def test_a_closed_session_takes_nothing_and_a_session_with_queued_messages_does_not_close(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="m-1")
        service.submit(session_id=session_id, text="waiting", request_id="m-2")

        with self.assertRaisesRegex(po_store.TurnInProgress, "1 message\\(s\\) queued"):
            service.close_session(session_id=session_id, actor="owner")
        self.assertEqual(self.store().session(session_id).state, po_store.SESSION_OPEN)

        self.gate.touch()
        self.settled(session_id, 2)
        service.close_session(session_id=session_id, actor="owner")
        with self.assertRaises(po_store.SessionClosed):
            service.submit(session_id=session_id, text="after", request_id="m-3")
        self.assertEqual(self.queued(), [])


class ServiceRestartTests(ServiceFixture):
    def test_a_turn_whose_process_died_with_the_service_is_rerun_once_over_the_same_conversation(
        self,
    ) -> None:
        for cli, model, resumed in (
            ("codex", "gpt-5.6-sol", "codex resume 019a-fake-thread"),
            ("claude", "opus", "--resume"),
        ):
            with self.subTest(cli=cli):
                self.gate.unlink(missing_ok=True)
                first = self.service()
                session_id = self.session(first, cli, model)
                first.submit(session_id=session_id, text="GATE remember 42", request_id=f"m-{cli}")
                self.reached_gate(session_id, 1)
                self.crash(first)

                second = self.service()

                turn = self.turns(session_id)[0]
                self.assertEqual((turn.state, turn.reason), (po_store.RUNNING, RERUN_REASON))
                self.gate.touch()
                done = self.settled(session_id, 1)
                self.assertEqual(done.state, po_store.COMPLETED)
                self.assertEqual(
                    done.reason, RERUN_REASON, "the completed turn keeps the record of its re-run"
                )
                self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
                [(_, owner, asked), (_, agent, answer)] = self.feed(session_id)
                self.assertEqual((owner, asked, agent), ("owner", "GATE remember 42", "agent"))
                self.assertIn(resumed, answer)
                second.stop()

    def test_a_rerun_interrupted_again_is_settled_and_the_queued_input_behind_it_runs(self) -> None:
        first = self.service()
        session_id = self.session(first, "codex", "gpt-5.6-sol")
        first.submit(session_id=session_id, text="GATE long", request_id="m-1")
        first.submit(session_id=session_id, text="behind it", request_id="m-2")
        self.reached_gate(session_id, 1)
        self.crash(first)

        second = self.service()
        self.reached_gate(session_id, 1, attempts=2)
        self.assertEqual(self.queued(), ["behind it"], "the queued input survives both restarts")
        self.crash(second)

        self.service()

        interrupted = self.turns(session_id)[0]
        self.assertEqual(interrupted.state, po_store.INTERRUPTED)
        self.assertTrue(interrupted.reason.startswith(RERUN_INTERRUPTED_REASON), interrupted.reason)
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.feed(session_id)[-2:][0], (2, "owner", "behind it"))
        launches = [call for call in self.calls() if call["prompt"] == "GATE long"]
        self.assertEqual(len(launches), 2, "one launch and exactly one re-run")
        self.assertEqual(self.queued(), [])

    def test_a_turn_process_still_alive_at_start_is_killed_then_rerun(self) -> None:
        first = self.service()
        session_id = self.session(first, "codex", "gpt-5.6-sol")
        first.submit(session_id=session_id, text="GATE alive", request_id="m-1")
        self.reached_gate(session_id, 1)
        old_pid = self.turns(session_id)[0].pid
        self.crash(first, kill=False)
        self.assertTrue(alive(old_pid))

        self.service()

        eventually(lambda: not alive(old_pid), "the previous run's process survived")
        turn = self.turns(session_id)[0]
        self.assertEqual(turn.reason, RERUN_REASON + "; its process was killed first")
        self.assertNotEqual(turn.pid, old_pid)
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)

    def test_a_turn_the_owner_stopped_is_never_rerun(self) -> None:
        first = self.service()
        session_id = self.session(first)
        first.submit(session_id=session_id, text="GATE stop me", request_id="m-1")
        self.reached_gate(session_id, 1)
        self.assertTrue(first.stop_turn(session_id=session_id, seq=1)["stopped"])
        self.crash(first)

        self.service()

        turn = self.turns(session_id)[0]
        self.assertEqual((turn.state, turn.reason), (po_store.INTERRUPTED, STOPPED_REASON))
        self.assertEqual(len(self.calls()), 1)

    def test_a_crash_between_the_claim_and_the_dequeue_makes_no_second_turn(self) -> None:
        first = self.service(run=False)
        session_id = self.session(first)
        real_remove = first.queue.remove

        class Crash(BaseException):
            pass

        def crash_before_removal(item) -> None:
            first.store.crash()
            raise Crash

        first.queue.remove = crash_before_removal  # type: ignore[method-assign]
        with self.assertRaises(Crash):
            first.submit(session_id=session_id, text="hello once", request_id="m-1")
        self.assertEqual(self.queued(), ["hello once"])
        self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
        first.queue.remove = real_remove  # type: ignore[method-assign]
        first.stop()

        self.service()

        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        eventually(lambda: self.queued() == [], "the handed-over input stayed queued")
        self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
        self.assertEqual([role for _seq, role, _text in self.feed(session_id)], ["owner", "agent"])


class RestartRuleTests(ServiceFixture):
    def test_an_idle_service_exits_now_for_a_restart(self) -> None:
        service = self.service()

        answer = po_client.request_restart(self.data, "code changed", client=_Direct(service))

        self.assertEqual((answer.outcome, answer.running), ("now", 0))
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the idle service did not exit")  # type: ignore[attr-defined]
        self.service(run=False)
        self.assertFalse(
            po_client.restart_marker_path(self.data).exists(), "the new process fulfils the request"
        )

    def test_a_busy_service_defers_the_restart_holds_the_queue_and_exits_once_idle(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE running upgrade", request_id="m-1")
        self.reached_gate(session_id, 1)

        answer = po_client.request_restart(self.data, "code changed", client=_Direct(service))

        self.assertEqual((answer.outcome, answer.running), ("deferred", 1))
        self.assertEqual(answer.detail, "PO service restart deferred: 1 turn(s) running")
        held = service.submit(
            session_id=self.session(service, request_id="c-2"), text="held", request_id="m-2"
        )
        self.assertTrue(held["queued"], "no new turn starts while a restart is pending")
        self.assertTrue(service.thread.is_alive())  # type: ignore[attr-defined]

        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the service did not exit at idle")  # type: ignore[attr-defined]
        self.assertEqual(self.queued(), ["held"])

        self.service()
        eventually(lambda: self.queued() == [], "the new process never took the held input")


class _Direct:
    """A client that calls the service in-process: the rule, without the socket."""

    def __init__(self, service: PoService) -> None:
        self.service = service

    def request_restart(self, *, reason: str) -> dict:
        return self.service.request_restart(reason=reason)


class EndpointTests(ServiceFixture):
    def layer(self, client: PoServiceClient | None = None) -> PoLayer:
        return PoLayer(self.root, data_dir=self.data, store=self.store(), client=client, models=MODELS)

    def app(self, layer: PoLayer) -> tuple[WebApp, dict[str, str]]:
        po_token.ensure_token(self.data)
        cookie = po_token.cookie_value(po_token.read_token(self.data))
        app = WebApp(
            *(Recording() for _ in range(8)), po_auth=PoTokenLayer(self.root, data_dir=self.data), po=layer
        )
        return app, {"Cookie": f"{po_token.COOKIE_NAME}={cookie}"}

    def test_the_socket_is_private_and_one_service_serves_an_installation(self) -> None:
        service = self.service()
        with listening(service) as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertTrue(stat.S_ISSOCK(path.stat().st_mode))
            with self.assertRaisesRegex(ServiceStartError, "another PO service"), listening(self.service()):
                pass
            self.assertEqual(PoServiceClient(self.data).status()["running"], 0)
        self.assertFalse(path.exists())

    def test_a_web_restart_leaves_the_running_turn_alone_and_its_answer_reaches_the_feed(self) -> None:
        service = self.service()
        with listening(service):
            first_web = self.layer()
            created = first_web.po_create_session(request_id="c-1", cli="claude", model="opus", effort="high")
            session_id = created["session_id"]
            sent = first_web.po_send(
                request_id="m-1", session_id=session_id, text="GATE across a web restart"
            )
            self.assertEqual((sent["kind"], sent["seq"]), ("po_turn_started", 1))
            self.reached_gate(session_id, 1)
            del first_web

            second_web = self.layer()
            self.assertTrue(second_web.po_session(session_id)["running"])
            self.gate.touch()
            eventually(lambda: not second_web.po_session(session_id)["running"], "the turn never settled")

            document = second_web.po_session(session_id)
            self.assertEqual(document["last_turn"]["state"], po_store.COMPLETED)
            self.assertEqual([entry["role"] for entry in document["feed"]], ["owner", "agent"])
            self.assertIn("GATE across a web restart", document["feed"][1]["text"])

    def test_the_layer_keeps_the_outcomes_it_had_and_shows_queued_messages(self) -> None:
        service = self.service()
        with listening(service):
            layer = self.layer()
            session_id = layer.po_create_session(
                request_id="c-1", cli="codex", model="gpt-5.6-sol", effort="high"
            )["session_id"]
            layer.po_send(request_id="m-1", session_id=session_id, text="GATE first")
            queued = layer.po_send(request_id="m-2", session_id=session_id, text="second")
            self.assertEqual(
                (queued["kind"], queued["queued"], queued["seq"]), ("po_turn_queued", True, None)
            )
            self.assertEqual([item["text"] for item in layer.po_session(session_id)["queued"]], ["second"])
            self.assertTrue(layer.po_send(request_id="m-2", session_id=session_id, text="second")["repeated"])
            with self.assertRaises(PoRequestConflict):
                layer.po_send(request_id="m-2", session_id=session_id, text="changed")
            with self.assertRaises(PoTurnInProgress):
                layer.po_close(session_id=session_id)
            self.reached_gate(session_id, 1)
            self.assertFalse(layer.po_stop(session_id=session_id, seq=7)["stopped"])
            stopped = layer.po_stop(session_id=session_id, seq=1)
            self.assertEqual((stopped["stopped"], stopped["turn"]["reason"]), (True, STOPPED_REASON))
            eventually(
                lambda: (
                    self.store().turns(session_id)[-1].state == po_store.COMPLETED
                    and len(self.store().turns(session_id)) == 2
                ),
                "the queued message never ran",
            )
            self.assertEqual(layer.po_session(session_id)["queued"], [])
            self.assertEqual(
                layer.po_close(session_id=session_id)["session"]["state"], po_store.SESSION_CLOSED
            )
            with self.assertRaises(PoSessionClosed):
                layer.po_send(request_id="m-3", session_id=session_id, text="after close")

    def test_every_write_is_refused_with_nothing_written_when_the_service_is_not_running(self) -> None:
        store = self.store()
        session, _ = store.claim_session(
            session_id="s-1",
            cli="claude",
            model="opus",
            cwd=str(self.data / "po"),
            cli_session_id="u",
            request_id="c-0",
        )
        layer = self.layer()
        for call in (
            lambda: layer.po_create_session(request_id="c-1", cli="claude", model="opus", effort="high"),
            lambda: layer.po_send(request_id="m-1", session_id="s-1", text="hello"),
            lambda: layer.po_stop(session_id="s-1", seq=1),
            lambda: layer.po_close(session_id="s-1"),
        ):
            with self.subTest(), self.assertRaisesRegex(RuntimeUnavailable, "PO service is not running"):
                call()
        with self.assertRaises(ServiceUnavailable):
            PoServiceClient(self.data).status()

        app, headers = self.app(layer)
        response = app.handle(
            "POST",
            "/po/sessions/s-1/messages",
            body=urlencode([("request_id", "m-2"), ("text", "hello")]).encode(),
            headers=headers,
        )
        self.assertEqual(response.status, 503)
        self.assertIn("PO service is not running", response.body.decode())
        self.assertIn("hello", response.body.decode(), "the draft is kept")

        self.assertEqual(store.turns("s-1"), [])
        self.assertEqual(store.feed("s-1"), [])
        self.assertEqual(len(store.sessions()), 1)
        self.assertFalse(queue_dir(self.data).exists() and any(queue_dir(self.data).glob("*.json")))
        self.assertEqual(layer.po_session("s-1")["session"]["session_id"], session.session_id)


class RequestIdReservationTests(ServiceFixture):
    """Review 5: one reservation check (`PoService._reserve`) owns every request id from its acknowledgement."""

    def test_a_queued_message_keeps_its_id_against_a_session_create_and_later_becomes_its_turn(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        queued = service.submit(session_id=session_id, text="accepted message", request_id="reserved")
        self.assertTrue(queued["queued"])

        with self.assertRaisesRegex(po_store.RequestConflict, "queued for PO session"):
            service.create_session(cli="claude", model="opus", effort="high", request_id="reserved")

        self.assertEqual(len(self.board.sessions), 1)
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.store().request("reserved").seq, 2)
        self.assertEqual(self.feed(session_id)[2], (2, "owner", "accepted message"))
        self.assertEqual(list((queue_dir(self.data) / "refused").glob("*.json")), [])

    def test_a_set_aside_message_keeps_its_id_too(self) -> None:
        service = self.service()
        session_id = self.session(service)
        item = service.queue.put(session_id="gone", text="lost", request_id="aside", source="web")
        service.queue.refuse(item, "there is no PO session gone")

        for call in (
            lambda: service.create_session(cli="claude", model="opus", effort="high", request_id="aside"),
            lambda: service.submit(session_id=session_id, text="lost", request_id="aside"),
        ):
            with self.subTest(), self.assertRaisesRegex(po_store.RequestConflict, "set aside"):
                call()
        self.assertIsNone(self.store().request("aside"))

    def test_every_operation_that_takes_a_request_id_goes_through_the_one_reservation(self) -> None:
        from ummanu.po import service as service_module

        takers = sorted(
            name
            for name in service_module._OPERATIONS.values()
            if "request_id" in inspect.signature(getattr(PoService, name)).parameters
        )
        self.assertEqual(takers, ["create_session", "sprint_session", "submit"])
        self.assertEqual(set(takers), service_module.ID_OPERATIONS)
        service = self.service(sprints=FakeSprints({"sprint:1": None}), models=MODELS)
        with mock.patch.object(service, "_reserve", wraps=service._reserve) as reserve:
            session_id = self.session(service, request_id="c-9")
            service.submit(session_id=session_id, text="hello", request_id="m-9")
            service.sprint_session(sprint_ref="sprint:1", request_id="s-9")
        self.assertEqual(
            [call.args[:2] for call in reserve.call_args_list],
            [("c-9", po_store.SESSION_CREATE), ("m-9", po_store.SEND), ("s-9", po_store.SPRINT_SESSION)],
        )


class OutcomeUnknownTests(EndpointTests):
    """Review 5: a request written to the service whose answer is lost is not "nothing was written"."""

    def lose_the_reply(self):
        """The service does the operation, then the connection drops before any answer."""
        from ummanu.po import service as service_module

        def no_reply(handler) -> None:
            request = json.loads(handler.rfile.readline())
            self.assertTrue(handler.server.service.handle(request)["ok"])
            handler.connection.shutdown(socket.SHUT_RDWR)

        return mock.patch.object(service_module._Handler, "handle", no_reply)

    def test_a_lost_reply_keeps_the_request_id_and_the_resend_is_the_same_message(self) -> None:
        service = self.service(run=False)
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        app, headers = self.app(self.layer())
        route = f"/po/sessions/{session_id}/messages"
        with listening(service):
            with self.lose_the_reply():
                response = app.handle(
                    "POST",
                    route,
                    body=urlencode({"request_id": "original", "text": "do this once"}).encode(),
                    headers=headers,
                )
            body = response.body.decode()
            self.assertEqual(response.status, 503)
            self.assertIn(
                "the PO service may have accepted this message; sending again with the same form is safe",
                body,
            )
            self.assertNotIn("nothing was sent or written", body)
            form = re.search(
                r'<form[^>]+action="' + re.escape(route) + r'"[^>]*>(.*?)</form>', body, re.DOTALL
            ).group(1)
            self.assertEqual(re.findall(r'name="request_id" value="([^"]+)"', form), ["original"])
            self.assertIn("do this once", form)

            retry = app.handle(
                "POST",
                route,
                body=urlencode({"request_id": "original", "text": "do this once"}).encode(),
                headers=headers,
            )

        self.assertEqual(retry.status, 303)
        self.assertEqual(self.queued(), ["do this once"])

    def test_create_stop_and_close_say_the_same_and_repeating_them_is_safe(self) -> None:
        service = self.service()
        layer = self.layer()
        with listening(service):
            with (
                self.lose_the_reply(),
                self.assertRaisesRegex(PoOutcomeUnknown, "may have opened this session"),
            ):
                layer.po_create_session(request_id="c-1", cli="claude", model="opus", effort="high")
            again = layer.po_create_session(request_id="c-1", cli="claude", model="opus", effort="high")
            self.assertTrue(again["repeated"])
            self.assertEqual(len(self.board.sessions), 1)
            session_id = again["session_id"]
            layer.po_send(request_id="m-1", session_id=session_id, text="GATE hold")
            self.reached_gate(session_id, 1)
            with (
                self.lose_the_reply(),
                self.assertRaisesRegex(PoOutcomeUnknown, "stopping it again is safe") as stop,
            ):
                layer.po_stop(session_id=session_id, seq=1)
            self.assertEqual(stop.exception.data["action"], "repeat_same_request")
            self.assertFalse(layer.po_stop(session_id=session_id, seq=1)["stopped"])
            with self.lose_the_reply(), self.assertRaisesRegex(PoOutcomeUnknown, "closing it again is safe"):
                layer.po_close(session_id=session_id)
            self.assertEqual(
                layer.po_close(session_id=session_id)["session"]["state"], po_store.SESSION_CLOSED
            )

    def test_the_service_runs_no_request_whose_line_never_ended(self) -> None:
        service = self.service()
        session_id = self.session(service)
        with listening(service) as path, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(path))
            line = {"op": "submit", "session_id": session_id, "text": "half", "request_id": "half"}
            connection.sendall(json.dumps(line).encode())
            connection.shutdown(socket.SHUT_WR)
            answer = json.loads(connection.makefile("rb").readline())
        self.assertEqual(answer["error"]["code"], "validation")
        self.assertEqual(self.queued(), [])
        self.assertIsNone(self.store().request("half"))


def form_request_ids(body: str, action: str) -> list[str]:
    form = re.search(r'<form[^>]+action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', body, re.DOTALL)
    assert form is not None, f"no form posting to {action}"
    return re.findall(r'name="request_id" value="([^"]+)"', form.group(1))


def fail_once(target, name: str, when):
    """Patch `target.name` so the first call for which `when(*args)` holds raises `PoStoreError`."""
    real = getattr(target, name)
    failed: list[tuple] = []

    def wrapper(*args, **kwargs):
        if not failed and when(*args, **kwargs):
            failed.append(args)
            raise po_store.PoStoreError(f"injected failure in {name}")
        return real(*args, **kwargs)

    return mock.patch.object(target, name, wrapper), failed


class AcceptanceAnswerTests(EndpointTests):
    """Review 17: after acceptance a request is answered as accepted, and a refused form keeps its id."""

    def post(self, app, headers, route: str, fields: dict[str, str]):
        return app.handle("POST", route, body=urlencode(fields).encode(), headers=headers)

    def test_a_failed_lookup_after_the_queue_write_answers_accepted_and_a_resend_is_the_same_message(
        self,
    ) -> None:
        service = self.service(run=False)
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        app, headers = self.app(self.layer())
        route = f"/po/sessions/{session_id}/messages"
        lookups: list[str] = []

        def second_lookup(_store, request_id):
            lookups.append(request_id)
            return request_id == "original" and lookups.count("original") == 2

        patch, failed = fail_once(FakePoStore, "request", second_lookup)
        with listening(service):
            with patch:
                first = self.post(app, headers, route, {"request_id": "original", "text": "do this once"})
            self.assertEqual(failed, [(mock.ANY, "original")], "the post-enqueue lookup failed once")
            self.assertEqual(first.status, 303, first.body.decode())
            again = self.post(app, headers, route, {"request_id": "original", "text": "do this once"})

        self.assertEqual(again.status, 303)
        self.assertEqual(self.queued(), ["do this once"])

    def test_a_queue_write_that_failed_after_landing_is_outcome_unknown_and_keeps_the_id(self) -> None:
        service = self.service(run=False)
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        app, headers = self.app(self.layer())
        route = f"/po/sessions/{session_id}/messages"
        real_put = service.queue.put

        def put_then_fail(**kwargs):
            real_put(**kwargs)
            raise QueueError("the directory fsync failed after the rename")

        with listening(service):
            with mock.patch.object(service.queue, "put", side_effect=put_then_fail):
                first = self.post(app, headers, route, {"request_id": "original", "text": "do this once"})
            body = first.body.decode()
            self.assertEqual(first.status, 503)
            self.assertIn("may have accepted this message", body)
            self.assertEqual(form_request_ids(body, route), ["original"])
            self.assertEqual(
                self.post(app, headers, route, {"request_id": "original", "text": "do this once"}).status, 303
            )

        self.assertEqual(self.queued(), ["do this once"])

    def test_every_post_acceptance_failure_of_a_session_create_ends_with_one_session(self) -> None:
        service = self.service(run=False)
        app, headers = self.app(self.layer())
        form = {"request_id": "create-once", "cli": "claude", "model": "opus", "effort": "high"}
        real_claim = FakePoStore.claim_session

        def commit_then_fail(store, **kwargs):
            real_claim(store, **kwargs)
            raise po_store.PoStoreError("the connection dropped after the commit")

        with listening(service):
            with mock.patch.object(FakePoStore, "claim_session", commit_then_fail):
                first = self.post(app, headers, "/po/sessions", form)
            body = first.body.decode()
            self.assertEqual(first.status, 503)
            self.assertIn("may have opened this session", body)
            self.assertEqual(form_request_ids(body, "/po/sessions"), ["create-once"])
            self.assertEqual(len(self.board.sessions), 1)
            [session_id] = self.board.sessions

            # The resend is the replay; its one enrichment read fails, and it is still the session.
            patch, failed = fail_once(FakePoStore, "session", lambda _store, sid: sid == session_id)
            with patch:
                again = self.post(app, headers, "/po/sessions", form)
            self.assertEqual(len(failed), 1)
            self.assertEqual((again.status, again.headers["Location"]), (303, f"/po/sessions/{session_id}"))
            third = self.post(app, headers, "/po/sessions", form)

        self.assertEqual(third.headers["Location"], f"/po/sessions/{session_id}")
        self.assertEqual(len(self.board.sessions), 1)

    def test_each_exit_of_submit_and_create_says_whether_it_wrote_nothing(self) -> None:
        service = self.service(run=False)
        session_id = self.session(service, request_id="made")
        closed = self.session(service, request_id="made-closed")
        service.close_session(session_id=closed, actor="owner")
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        service.submit(session_id=session_id, text="waiting", request_id="waiting")

        def error(**request):
            answer = service.handle(request)
            self.assertFalse(answer["ok"], answer)
            return answer["error"]["code"], answer["error"].get("nothing_written", False)

        definite = [
            ({"op": "submit", "session_id": session_id, "text": " ", "request_id": "e"}, "validation"),
            ({"op": "submit", "session_id": session_id, "text": "x"}, "validation"),
            (
                {"op": "submit", "session_id": session_id, "text": "x", "request_id": "made"},
                "request_conflict",
            ),
            (
                {"op": "submit", "session_id": session_id, "text": "other", "request_id": "waiting"},
                "request_conflict",
            ),
            ({"op": "submit", "session_id": "nope", "text": "x", "request_id": "n"}, "session_not_found"),
            ({"op": "submit", "session_id": closed, "text": "x", "request_id": "c"}, "session_closed"),
            ({"op": "create_session", "cli": "gemini", "model": "m", "request_id": "g"}, "validation"),
            ({"op": "create_session", "cli": "claude", "model": " ", "request_id": "g"}, "validation"),
            (
                {
                    "op": "create_session",
                    "cli": "claude",
                    "model": "opus",
                    "effort": "high",
                    "request_id": "waiting",
                },
                "request_conflict",
            ),
            (
                {
                    "op": "create_session",
                    "cli": "codex",
                    "model": "m",
                    "effort": "high",
                    "request_id": "made",
                },
                "request_conflict",
            ),
        ]
        for request, code in definite:
            with self.subTest(request=request):
                self.assertEqual(error(**request), (code, True))

        # A store that fails before acceptance wrote nothing either, but nobody marked it: the id is kept.
        with mock.patch.object(FakePoStore, "request", side_effect=po_store.PoStoreError("away")):
            self.assertEqual(
                error(op="submit", session_id=session_id, text="x", request_id="k"), ("unavailable", False)
            )
            self.assertEqual(
                error(op="create_session", cli="claude", model="opus", effort="high", request_id="k"),
                ("unavailable", False),
            )
        self.assertEqual(self.queued(), ["waiting"])


class KeptRequestIdTests(ServiceFixture):
    """The web keeps a refused form's request id unless the refusal is marked as having written nothing."""

    SESSION: ClassVar[dict] = {
        "kind": "po_session",
        "session": {"session_id": "s-1", "state": "open", "cli": "claude", "model": "opus"},
        "turns": [],
        "feed": [],
        "running": False,
        "queued": [],
    }
    OVERVIEW: ClassVar[dict] = {
        "kind": "po_overview",
        "closed": False,
        "closed_count": 0,
        "sessions": [],
        "running": 0,
        "models": {"claude": ["opus"]},
        "efforts": {"claude": ["high"]},
    }

    def forms(self, failure: Exception) -> tuple[list[str], list[str]]:
        po_token.ensure_token(self.data)
        cookie = po_token.cookie_value(po_token.read_token(self.data))
        po = Recording(
            po_send=failure, po_create_session=failure, po_session=self.SESSION, po_overview=self.OVERVIEW
        )
        app = WebApp(
            *(Recording() for _ in range(8)), po_auth=PoTokenLayer(self.root, data_dir=self.data), po=po
        )
        headers = {"Cookie": f"{po_token.COOKIE_NAME}={cookie}"}
        sent = app.handle(
            "POST",
            "/po/sessions/s-1/messages",
            body=urlencode({"request_id": "form-1", "text": "hello"}).encode(),
            headers=headers,
        )
        created = app.handle(
            "POST",
            "/po/sessions",
            body=urlencode({"request_id": "form-2", "cli": "claude", "model": "opus"}).encode(),
            headers=headers,
        )
        return (
            form_request_ids(sent.body.decode(), "/po/sessions/s-1/messages"),
            form_request_ids(created.body.decode(), "/po/sessions"),
        )

    def test_an_unexpected_exception_from_the_layer_keeps_the_id(self) -> None:
        self.assertEqual(self.forms(RuntimeError("boom")), (["form-1"], ["form-2"]))

    def test_an_unmarked_refusal_keeps_the_id_and_only_a_marked_one_gets_a_fresh_one(self) -> None:
        for failure in (
            RuntimeUnavailable("the store answered badly"),
            PoOutcomeUnknown("no answer"),
            PoRequestConflict("taken"),
            PoTurnInProgress("busy"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(self.forms(failure), (["form-1"], ["form-2"]))
        for failure in (
            RuntimeUnavailable("the PO service is not running", data=NOTHING_WRITTEN),
            PoRequestConflict("taken", data=NOTHING_WRITTEN),
            PoSessionClosed("closed", data=NOTHING_WRITTEN),
        ):
            with self.subTest(failure=type(failure).__name__, marked=True):
                sent, created = self.forms(failure)
                self.assertNotEqual(sent, ["form-1"])
                self.assertNotEqual(created, ["form-2"])
                self.assertTrue(sent[0].startswith("web-po-") and created[0].startswith("web-po-"))


class RecoveryProgressTests(ServiceFixture):
    """Review 5: recovery is complete only when no `running` row is left without a process."""

    def test_new_cleanup_incomplete_launch_recovers_without_restart_and_other_session_continues(self) -> None:
        service = self.service()
        session_id = self.session(service)
        other = self.session(service, request_id="other-session")
        original = service.runner._turn_launcher
        failed = []
        def launch(session, seq, *args):
            if session.session_id == session_id and not failed:
                failed.append(seq)
                directory = service.runner._scope_dir(session_id, seq)
                directory.mkdir(parents=True)
                ScopedHeadLifecycle("new-orphan", 96).persist(directory)
                raise LocalPtySpawnError("cleanup_failed", "scope still populated", cleanup_complete=False)
            return original(session, seq, *args)
        service.runner._turn_launcher = launch
        clean = threading.Event()
        attempts = []
        def cleanup(owner, _record):
            attempts.append(owner.run_id)
            if not clean.is_set():
                raise MemoryScopeError("temporary cleanup failure")
            _record["launch_allowed"] = False
            owner._record_empty(_record)
        with mock.patch.object(ScopedHeadLifecycle, "stop_owned", cleanup):
            self.assertTrue(service._recovered)
            service.submit(session_id=session_id, text="first", request_id="first")
            service.submit(session_id=session_id, text="second", request_id="second")
            self.assertFalse(service._recovered)
            self.assertEqual(self.turns(session_id)[0].state, po_store.RUNNING)
            self.assertEqual(self.queued(), ["second"])
            service.submit(session_id=other, text="independent", request_id="independent")
            self.assertEqual(self.settled(other, 1).state, po_store.COMPLETED)
            eventually(lambda: bool(attempts), "steady-state recovery did not retry cleanup")
            clean.set()
            self.assertEqual(self.settled(session_id, 1).state, po_store.FAILED)
            self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(failed, [1])
        self.assertEqual(self.queued(), [])

    def test_waiter_start_failure_rearms_recovery_after_startup(self) -> None:
        service = self.service()
        session_id = self.session(service)
        original = service.runner._turn_launcher
        processes = []
        def launch(session, seq, *args):
            process = original(session, seq, *args)
            if seq == 1:
                processes.append(process)
                directory = service.runner._scope_dir(session_id, seq)
                directory.mkdir()
                ScopedHeadLifecycle("waiter-orphan", 96).persist(directory)
            return process
        service.runner._turn_launcher = launch
        clean = threading.Event()
        def cleanup(_owner, _record):
            if not clean.is_set():
                raise MemoryScopeError("temporary cleanup failure")
            for process in processes:
                po_runner._kill_group(process.pid)
            _record["launch_allowed"] = False
            _owner._record_empty(_record)
        with mock.patch.object(ScopedHeadLifecycle, "stop_owned", cleanup):
            with mock.patch("ummanu.po.runner.threading.Thread.start", side_effect=RuntimeError("waiter refused")):
                service.submit(session_id=session_id, text="GATE waiter", request_id="waiter")
            self.assertFalse(service._recovered)
            self.assertEqual(self.turns(session_id)[0].state, po_store.RUNNING)
            service.submit(session_id=session_id, text="after waiter", request_id="after-waiter")
            self.assertEqual(self.queued(), ["after waiter"])
            clean.set()
            self.assertEqual(self.settled(session_id, 1).state, po_store.FAILED)
            self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
            for process in processes:
                process.wait(timeout=5)

    def test_completion_and_owner_stop_cleanup_failures_keep_their_outcome_until_recovery(self) -> None:
        for action in ("completion", "stop"):
            with self.subTest(action=action):
                service = self.service()
                session_id = self.session(service, request_id=f"session-{action}")
                original = service.runner._turn_launcher
                processes = []
                def launch(session, seq, *args):
                    process = original(session, seq, *args)
                    if seq == 1:
                        processes.append(process)
                        directory = service.runner._scope_dir(session_id, seq)
                        directory.mkdir()
                        ScopedHeadLifecycle(f"pending-{action}", 96).persist(directory)
                    return process
                service.runner._turn_launcher = launch
                clean = threading.Event()
                attempted = threading.Event()
                def cleanup(_owner, _record):
                    attempted.set()
                    if not clean.is_set():
                        raise MemoryScopeError("temporary cleanup failure")
                    for process in processes:
                        po_runner._kill_group(process.pid)
                    _record["launch_allowed"] = False
                    _owner._record_empty(_record)
                with mock.patch.object(ScopedHeadLifecycle, "stop_owned", cleanup):
                    text = "complete" if action == "completion" else "GATE stop"
                    service.submit(session_id=session_id, text=text, request_id=f"input-{action}")
                    if action == "stop":
                        # Submission can still be queued while the service's drain holds its lock.
                        # Stop the launched, scoped process so this case reaches the injected
                        # cleanup failure rather than the legitimate no-running-turn result.
                        self.reached_gate(session_id, 1)
                        eventually(lambda: ScopedHeadLifecycle.from_run_dir(
                            service.runner._scope_dir(session_id, 1)) is not None,
                            "the running turn's cleanup scope was not persisted")
                        with self.assertRaises(MemoryScopeError):
                            service.stop_turn(session_id=session_id, seq=1)
                    eventually(attempted.is_set, "cleanup was not attempted")
                    eventually(lambda: not service._recovered, "cleanup failure did not schedule recovery")
                    service.submit(session_id=session_id, text="next", request_id=f"next-{action}")
                    self.assertEqual(self.turns(session_id)[0].state, po_store.RUNNING)
                    clean.set()
                    expected = po_store.COMPLETED if action == "completion" else po_store.INTERRUPTED
                    self.assertEqual(self.settled(session_id, 1).state, expected)
                    self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
    def interrupted(self, *, queued: str = "waiting") -> str:
        first = self.service(run=False)
        session_id = self.session(first)
        first.submit(session_id=session_id, text="GATE interrupted", request_id="original")
        first.submit(session_id=session_id, text=queued, request_id="waiting")
        self.reached_gate(session_id, 1)
        self.crash(first)
        return session_id

    def test_one_failed_feed_read_spends_no_rerun_and_recovery_retries_until_the_queue_moves(self) -> None:
        session_id = self.interrupted()
        real_feed = FakePoStore.feed
        failures = []

        def feed(store, sid):
            if not failures:
                failures.append(sid)
                raise po_store.PoStoreError("temporary feed read failure")
            return real_feed(store, sid)

        with mock.patch.object(FakePoStore, "feed", feed):
            second = self.service(run=False)
            turn = self.turns(session_id)[0]
            self.assertFalse(second._recovered)
            self.assertEqual(
                (turn.state, turn.reason), (po_store.RUNNING, None), "the re-run allowance is unspent"
            )
            second.pump()
            self.assertEqual(self.queued(), ["waiting"], "the session of an unrecovered row takes nothing")
            thread = threading.Thread(target=second.run, kwargs={"tick": 0.05, "say": lambda _line: None})
            thread.start()
            self.addCleanup(thread.join, 10)
            self.addCleanup(second.stop)
            eventually(lambda: second._recovered, "recovery never completed")

        self.assertEqual(failures, [session_id])
        self.assertEqual(self.turns(session_id)[0].reason, RERUN_REASON)
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.queued(), [])

    def test_a_rerun_that_cannot_launch_is_settled_failed_and_the_queue_goes_on(self) -> None:
        session_id = self.interrupted(queued="GATE waiting")
        self.executables = {**self.executables, "claude": str(self.root / "no-such-claude")}

        second = self.service()

        turn = self.turns(session_id)[0]
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("could not start", turn.reason)
        self.assertTrue(second._recovered)
        self.assertEqual(self.settled(session_id, 2).state, po_store.FAILED, "the queued input was taken")

    def test_a_rerun_that_can_never_be_prepared_is_settled_failed_without_spending_it(self) -> None:
        session_id = self.interrupted()
        with mock.patch.object(PoRunner, "_owner_text", side_effect=RunnerError("no owner message")):
            second = self.service()

        turn = self.turns(session_id)[0]
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("could not be prepared", turn.reason)
        self.assertTrue(second._recovered)
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)

    def test_a_launch_failure_the_store_could_not_record_is_settled_by_the_next_pass(self) -> None:
        session_id = self.interrupted()
        runner = PoRunner(
            FakePoStore(self.board), self.data, executables={"claude": str(self.root / "missing")},
            turn_launcher=unscoped_test_launch,
        )
        real_finish = runner.store.finish_turn
        calls = []

        def finish(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise po_store.PoStoreError("the store is away")
            return real_finish(*args, **kwargs)

        # The crashed service's waiter may still attempt its own terminal write.
        # Only this recovery runner's launch-failure write loses its reply; the
        # old store must not consume the injected failure by thread scheduling.
        with mock.patch.object(runner.store, "finish_turn", finish):
            runner.recover(rerun=True)
            self.assertEqual(
                [t.seq for t in runner.orphaned_turns()], [1], "left running, reported as orphaned"
            )
            runner.recover(rerun=True)

        self.assertEqual([(args[0], args[1], args[2]) for args in calls],
                         [(session_id, 1, po_store.FAILED)] * 2)
        turn = self.turns(session_id)[0]
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("the re-run did not start", turn.reason)
        self.assertEqual(runner.orphaned_turns(), [])


PO_UNIT = "ummanu-po.service"


class PoUpgradeFixture(ServiceFixture):
    """A Git product checkout, a data dir and a fake PO unit, for `step_po` and `step_verify`."""

    def setUp(self) -> None:
        super().setUp()
        self.product = self.root / "product"
        for relative, text in (
            ("pyproject.toml", '[project]\nname = "ummanu"\n'),
            ("src/ummanu/__init__.py", ""),
            ("src/ummanu/app.py", "VERSION = 'A'\n"),
            ("src/ummanu/schemas/card.json", "{}\n"),
        ):
            path = self.product / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("init", "--quiet", "--initial-branch=main")
        self.commit("A")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.product), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git(
            "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", message
        )

    def move_checkout(self) -> None:
        """A commit this upgrade did not pull: the dispatcher's release fast-forwarded the checkout."""
        (self.product / "src/ummanu/app.py").write_text("VERSION = 'B'\n", encoding="utf-8")
        self.commit("B")

    def bind(self, identity: UnitProcessIdentity) -> str:
        """What the PO service process `identity` writes when it starts on the checkout as it is now."""
        with (
            mock.patch.object(upgrade.os, "getpid", return_value=identity.pid),
            mock.patch.object(upgrade, "_process_start_ticks", return_value=identity.start_ticks),
        ):
            return upgrade.write_po_process_receipt(
                self.data, self.product, environ={"INVOCATION_ID": identity.invocation_id}
            )

    def receipt(self) -> Path:
        return po_client.process_receipt_path(self.data)

    def context(self, units: FakeUnitInstaller, **flags) -> upgrade.UpgradeContext:
        report = SimpleNamespace(
            host={"unit_prefix": "ummanu-"},
            instance={"host": {"unit_prefix": "ummanu-"}},
            data_dir=self.data,
            bindings=[],
        )
        return upgrade.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.product,
            base_branch="main",
            dry_run=flags.pop("dry_run", False),
            units=units,
            pull=False,
            report=report,
            **{"code_changed": True, **flags},
        )

    def units(self) -> FakeUnitInstaller:
        return FakeUnitInstaller(present={PO_UNIT: b"[Service]\n"}, active={PO_UNIT})


class UpgradeStepTests(PoUpgradeFixture):
    """`step_po`: an upgrade restarts the PO service now only while it is idle, else defers it."""

    def test_the_step_sits_before_the_web_restart(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]
        self.assertEqual(names.index("step_po"), names.index("step_web") - 1)

    def test_idle_the_service_is_restarted_now_and_the_new_process_is_waited_for(self) -> None:
        service = self.service()
        units = self.units()
        before = units.identities[PO_UNIT]

        def systemd_brings_it_back(_seconds: float) -> None:
            units.identities[PO_UNIT] = units._new_identity()

        with listening(service), mock.patch.object(upgrade, "_sleep", side_effect=systemd_brings_it_back):
            result = upgrade.step_po(self.context(units))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn(f"restarted {PO_UNIT} while idle", result.detail)
        self.assertIn("product code or dependencies changed", result.detail)
        self.assertNotEqual(units.identities[PO_UNIT], before)
        self.assertNotIn(
            ("restart", PO_UNIT), units.calls, "the service exits by itself; systemd restarts it"
        )
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive())  # type: ignore[attr-defined]

    def test_busy_the_restart_is_deferred_and_applied_by_the_service_at_idle(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE ummanu upgrade", request_id="m-1")
        self.reached_gate(session_id, 1)
        units = self.units()

        with listening(service), mock.patch.object(upgrade, "_sleep") as sleep:
            result = upgrade.step_po(self.context(units))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertTrue(
            result.detail.startswith("PO service restart deferred: 1 turn(s) running"), result.detail
        )
        sleep.assert_not_called()
        self.assertEqual(units.calls, [])
        self.assertTrue(po_client.restart_marker_path(self.data).exists())
        self.assertEqual(self.turns(session_id)[0].state, po_store.RUNNING, "the caller's turn is untouched")

        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the deferred restart was not applied at idle")  # type: ignore[attr-defined]

    def test_a_service_that_does_not_answer_keeps_the_request_on_disk(self) -> None:
        result = upgrade.step_po(self.context(self.units()))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("not acknowledged", result.detail)
        self.assertTrue(po_client.restart_marker_path(self.data).exists())

    def test_uninstalled_stopped_unchanged_and_dry_run(self) -> None:
        missing = upgrade.step_po(self.context(FakeUnitInstaller()))
        self.assertEqual(missing.status, "skipped", missing.detail)

        stopped = FakeUnitInstaller(present={PO_UNIT: b"[Service]\n"})
        started = upgrade.step_po(self.context(stopped))
        self.assertEqual((started.status, stopped.calls), ("changed", [("restart", PO_UNIT)]))

        bound = self.units()
        self.bind(bound.identities[PO_UNIT])
        current = upgrade.step_po(self.context(bound, code_changed=False))
        self.assertEqual(current.status, "unchanged", current.detail)

        dry = upgrade.step_po(self.context(self.units(), dry_run=True, po_unit_changed=True))
        self.assertEqual(dry.status, "changed")
        self.assertIn("would ask", dry.detail)
        self.assertIn("the PO unit file changed", dry.detail)
        self.assertFalse(po_client.restart_marker_path(self.data).exists())


class ProcessReceiptTests(PoUpgradeFixture):
    """The PO process receipt: `step_po` compares the running process with the checkout (secretary-1759)."""

    def test_a_checkout_moved_before_an_empty_pull_restarts_the_service_on_the_stale_receipt(self) -> None:
        # The live defect: the dispatcher's release fast-forwarded the checkout, so this upgrade's pull
        # saw nothing and `code_changed` is False, while the service still runs the old revision.
        service = self.service()
        units = self.units()
        self.bind(units.identities[PO_UNIT])
        old_revision = self.git("rev-parse", "HEAD")
        self.move_checkout()
        new_revision = self.git("rev-parse", "HEAD")

        def systemd_brings_it_back(_seconds: float) -> None:
            units.identities[PO_UNIT] = units._new_identity()

        with listening(service), mock.patch.object(upgrade, "_sleep", side_effect=systemd_brings_it_back):
            result = upgrade.step_po(self.context(units, code_changed=False))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn(f"restarted {PO_UNIT} while idle", result.detail)
        self.assertIn(
            f"the PO process receipt is stale: product revision {old_revision[:12]} -> {new_revision[:12]}",
            result.detail,
        )
        self.assertIn("product sha256", result.detail)
        self.assertNotIn("product code or dependencies changed", result.detail)
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the service did not exit for the restart")  # type: ignore[attr-defined]

    def test_no_receipt_is_a_restart_reason(self) -> None:
        result = upgrade.step_po(self.context(self.units(), code_changed=False))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("the PO process receipt is missing", result.detail)
        self.assertTrue(po_client.restart_marker_path(self.data).exists())

    def test_a_matching_receipt_answers_unchanged(self) -> None:
        units = self.units()
        identity = units.identities[PO_UNIT]
        self.bind(identity)

        result = upgrade.step_po(self.context(units, code_changed=False))

        self.assertEqual(result.status, "unchanged", result.detail)
        revision = self.git("rev-parse", "HEAD")
        self.assertTrue(
            result.detail.startswith(
                f"PO process receipt verified: pid {identity.pid}, revision {revision[:12]}"
            ),
            result.detail,
        )
        self.assertFalse(po_client.restart_marker_path(self.data).exists())

    def test_a_receipt_of_another_process_generation_is_a_restart_reason(self) -> None:
        units = self.units()
        self.bind(units._new_identity())

        result = upgrade.step_po(self.context(units, code_changed=False))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("the PO process receipt belongs to a different process generation", result.detail)

    def test_the_existing_change_flags_are_still_reasons_over_a_matching_receipt(self) -> None:
        units = self.units()
        self.bind(units.identities[PO_UNIT])
        for flag, reason in (
            ("code_changed", "product code or dependencies changed"),
            ("schemas_changed", "bundled schemas changed"),
            ("po_unit_changed", "the PO unit file changed"),
        ):
            with self.subTest(flag):
                flags = {"code_changed": False, flag: True}
                result = upgrade.step_po(self.context(units, dry_run=True, **flags))
                self.assertEqual(result.status, "changed", result.detail)
                self.assertIn(reason, result.detail)
                self.assertNotIn("receipt", result.detail)

    def test_busy_a_stale_receipt_defers_the_restart(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE ummanu upgrade", request_id="m-1")
        self.reached_gate(session_id, 1)
        units = self.units()
        self.bind(units.identities[PO_UNIT])
        self.move_checkout()

        with listening(service), mock.patch.object(upgrade, "_sleep") as sleep:
            result = upgrade.step_po(self.context(units, code_changed=False))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertTrue(
            result.detail.startswith("PO service restart deferred: 1 turn(s) running"), result.detail
        )
        self.assertIn("the PO process receipt is stale", result.detail)
        sleep.assert_not_called()
        self.assertEqual(units.calls, [])
        self.assertEqual(self.turns(session_id)[0].state, po_store.RUNNING, "the caller's turn is untouched")
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the deferred restart was not applied at idle")  # type: ignore[attr-defined]

    def test_dry_run_names_the_stale_receipt_and_writes_nothing(self) -> None:
        units = self.units()
        self.bind(units.identities[PO_UNIT])
        before = self.receipt().read_bytes()
        self.move_checkout()

        result = upgrade.step_po(self.context(units, code_changed=False, dry_run=True))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("would ask", result.detail)
        self.assertIn("the PO process receipt is stale", result.detail)
        self.assertFalse(po_client.restart_marker_path(self.data).exists())
        self.assertEqual(self.receipt().read_bytes(), before)
        self.assertEqual(units.calls, [])

    def test_hostile_receipts_are_restart_reasons_never_exceptions(self) -> None:
        units = self.units()
        self.bind(units.identities[PO_UNIT])
        valid = json.loads(self.receipt().read_text(encoding="utf-8"))
        cases = {
            "not json": "{",
            "a list": "[]",
            "extra key": json.dumps({**valid, "unit": PO_UNIT}),
            "another version": json.dumps({**valid, "version": 2}),
            "boolean version": json.dumps({**valid, "version": True}),
            "short revision": json.dumps({**valid, "inputs": {**valid["inputs"], "product_revision": "abc"}}),
            "missing input": json.dumps(
                {**valid, "inputs": {k: v for k, v in valid["inputs"].items() if k != "schemas_sha256"}}
            ),
            "negative pid": json.dumps({**valid, "process": {**valid["process"], "pid": -1}}),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.receipt().write_text(text, encoding="utf-8")
                result = upgrade.step_po(self.context(units, code_changed=False, dry_run=True))
                self.assertEqual(result.status, "changed", result.detail)
                self.assertIn("the PO process receipt is malformed", result.detail)
        self.receipt().unlink()
        self.receipt().mkdir()
        result = upgrade.step_po(self.context(units, code_changed=False, dry_run=True))
        self.assertIn("the PO process receipt is malformed", result.detail)

    # --- what the service writes ------------------------------------------------------------------

    def test_the_receipt_is_private_and_a_new_generation_replaces_the_previous_one(self) -> None:
        first = FakeUnitInstaller._new_identity()
        line = self.bind(first)
        self.assertIn(f"pid {first.pid}", line)
        self.assertEqual(stat.S_IMODE(os.stat(self.receipt()).st_mode), 0o600)
        written = json.loads(self.receipt().read_text(encoding="utf-8"))
        self.assertEqual(
            written["process"],
            {"pid": first.pid, "start_ticks": first.start_ticks, "invocation_id": first.invocation_id},
        )
        self.assertEqual(written["inputs"]["product_revision"], self.git("rev-parse", "HEAD"))

        second = FakeUnitInstaller._new_identity()
        self.move_checkout()
        self.bind(second)

        rewritten = json.loads(self.receipt().read_text(encoding="utf-8"))
        self.assertEqual(rewritten["process"]["pid"], second.pid)
        self.assertEqual(rewritten["inputs"]["product_revision"], self.git("rev-parse", "HEAD"))
        self.assertEqual(sorted(p.name for p in self.receipt().parent.iterdir()), ["process-receipt.json"])

    def test_a_failed_write_leaves_the_previous_receipt_whole(self) -> None:
        self.bind(FakeUnitInstaller._new_identity())
        before = self.receipt().read_bytes()

        with (
            mock.patch.object(upgrade.os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(upgrade.ReceiptError),
        ):
            self.bind(FakeUnitInstaller._new_identity())

        self.assertEqual(self.receipt().read_bytes(), before)
        self.assertEqual(list(self.receipt().parent.glob("*.tmp")), [])

    def test_a_process_systemd_did_not_start_writes_no_receipt(self) -> None:
        with self.assertRaisesRegex(upgrade.ReceiptError, "INVOCATION_ID"):
            upgrade.write_po_process_receipt(self.data, self.product, environ={})
        self.assertFalse(self.receipt().exists())

        with mock.patch.object(
            upgrade, "write_po_process_receipt", side_effect=upgrade.ReceiptError("no INVOCATION_ID")
        ):
            line = po_service._write_process_receipt(self.data)
        self.assertIn("no process receipt was written (no INVOCATION_ID)", line)

    def test_the_service_writes_its_receipt_before_it_fulfils_a_pending_restart(self) -> None:
        source = inspect.getsource(po_service.run_po_serve)
        self.assertLess(source.index("_write_process_receipt(data_dir)"), source.index("service.start()"))
        with mock.patch.object(upgrade, "write_po_process_receipt", return_value="wrote it") as write:
            self.assertEqual(po_service._write_process_receipt(self.data), "ummanu po: wrote it")
        root = write.call_args.args[1]
        self.assertTrue((root / "src" / "ummanu" / "po" / "service.py").is_file(), root)

    def test_the_receipt_is_excluded_from_backup(self) -> None:
        relative = self.receipt().relative_to(self.data)
        self.assertTrue(should_skip_data_entry(relative, policy=FULL_POLICY))

    # --- verify -----------------------------------------------------------------------------------

    def verify(self, units: FakeUnitInstaller) -> upgrade.StepResult:
        with (
            mock.patch.object(upgrade, "step_host", return_value=upgrade.StepResult("host", "unchanged")),
            mock.patch.object(upgrade.role_skills, "audit", return_value={"ok": True}),
            mock.patch.object(upgrade, "assert_snapshot_current"),
            mock.patch.object(upgrade, "installed_heads"),
            mock.patch.object(upgrade.state_repo, "status", return_value=""),
        ):
            return upgrade.step_verify(self.context(units, code_changed=False))

    def test_verify_reports_the_po_receipt(self) -> None:
        units = self.units()
        identity = units.identities[PO_UNIT]
        self.bind(identity)

        verified = self.verify(units)
        self.assertEqual(verified.status, "unchanged", verified.detail)
        self.assertIn(f"PO process receipt verified: pid {identity.pid}", verified.detail)

        self.move_checkout()
        stale = self.verify(units)
        self.assertEqual(stale.status, "failed", stale.detail)
        self.assertIn(
            "active PO process receipt is not current: the PO process receipt is stale", stale.detail
        )

        po_client.write_restart_marker(self.data, "deferred by step_po")
        pending = self.verify(units)
        self.assertEqual(pending.status, "unchanged", pending.detail)
        self.assertIn("PO service restart pending: the PO process receipt is stale", pending.detail)

    def test_verify_names_a_skipped_po_unit(self) -> None:
        missing = self.verify(FakeUnitInstaller())
        self.assertEqual(missing.status, "unchanged", missing.detail)
        self.assertIn(f"PO process receipt not checked: {PO_UNIT} is not installed", missing.detail)

        stopped = self.verify(FakeUnitInstaller(present={PO_UNIT: b"[Service]\n"}))
        self.assertEqual(stopped.status, "unchanged", stopped.detail)
        self.assertIn(f"PO process receipt not checked: {PO_UNIT} is not active", stopped.detail)


class TurnEnvironmentTests(ServiceFixture):
    """Every turn names its own PO session, so `sprint create` inside it records that session."""

    def test_a_new_turn_and_its_rerun_both_carry_their_session(self) -> None:
        first = self.service()
        session_id = self.session(first)
        other = self.session(first, "codex", "gpt-5.6-sol")
        first.submit(session_id=other, text="elsewhere", request_id="m-other")
        first.submit(session_id=session_id, text="GATE remember 42", request_id="m-1")
        self.reached_gate(session_id, 1)
        self.settled(other, 1)
        self.crash(first)

        second = self.service()
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).reason, RERUN_REASON)

        launches = [call for call in self.calls() if call["prompt"] == "GATE remember 42"]
        # The first launch, its re-run, and the re-run's relaunch over `--resume`.
        self.assertGreaterEqual(len(launches), 2)
        self.assertEqual({call["po_session"] for call in launches}, {session_id})
        # And the request id of the input it answers (secretary-1792), read back from the store on a re-run.
        self.assertEqual({call["po_request"] for call in launches}, {"m-1"})
        [elsewhere] = [call for call in self.calls() if call["prompt"] == "elsewhere"]
        self.assertEqual((elsewhere["po_session"], elsewhere["po_request"]), (other, "m-other"))
        self.assertNotIn(po_runner.PO_SESSION_ENV, second.runner.env)
        self.assertNotIn(po_runner.PO_REQUEST_ENV, second.runner.env)

    def test_a_turn_whose_input_carried_no_request_id_names_none(self) -> None:
        service = self.service()
        session_id = self.session(service)
        with mock.patch.dict(os.environ, {po_runner.PO_REQUEST_ENV: "inherited-from-the-service"}):
            runner = PoRunner(service.store, self.data, executables=self.executables, env=dict(os.environ))
            environment = runner.session_environment(service.store.session(session_id), 1)
        self.assertEqual(environment[po_runner.PO_SESSION_ENV], session_id)
        self.assertNotIn(po_runner.PO_REQUEST_ENV, environment)


def why(path: str, text: str) -> WhyDocument:
    return WhyDocument(path, text)


class SprintSessionTests(ServiceFixture):
    """`PoService.sprint_session`: the sprint's live PO session, re-seeded once when it is gone."""

    DOC = "state/knowledge/decisions/2026-09-26-sprint-1-why.md"

    def resolver(self, sprints: FakeSprints, **options) -> PoService:
        return self.service(sprints=sprints, models=MODELS, **options)

    def first_prompt(self, session_id: str) -> str:
        return self.feed(session_id)[0][2]

    def test_an_open_recorded_session_is_the_answer_and_nothing_is_written(self) -> None:
        sprints = FakeSprints({})
        service = self.resolver(sprints)
        session_id = self.session(service)
        sprints.records["sprint:1"] = SprintRecord("sprint:1", "open", session_id)

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        self.assertEqual(answer, {"session_id": session_id, "created": False, "repeated": False})
        self.assertEqual(list(self.board.sessions), [session_id])
        self.assertIsNone(self.store().request("r-1"))
        self.assertEqual((sprints.comments, sprints.recorded, self.queued()), ({}, {}, []))

    def test_a_sprint_with_no_session_gets_one_seeded_with_its_why_document_and_notes(self) -> None:
        sprints = FakeSprints(
            {"sprint:1": None},
            documents={"sprint:1": [why(self.DOC, "# Why\n\nBecause the owner said so.\n")]},
        )
        service = self.resolver(sprints)

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        self.assertEqual((answer["created"], answer["repeated"]), (True, False))
        session = self.store().session(answer["session_id"])
        # No recorded session: the new-session form's preselection, first CLI and first model, at the
        # first effort offered for that CLI, never `default`.
        self.assertEqual((session.cli, session.model, session.effort), ("claude", "opus", "high"))
        self.assertEqual(sprints.records["sprint:1"].po_session, session.session_id)
        self.assertEqual(
            list(sprints.comments.values()),
            [
                (
                    "sprint:1",
                    (
                        f"the PO session none no longer exists; opened {session.session_id} seeded with "
                        f"{self.DOC} and NOTES.md"
                    ),
                )
            ],
        )
        self.assertEqual(self.settled(session.session_id, 1).state, po_store.COMPLETED)
        seed = self.first_prompt(session.session_id)
        for expected in (
            "sprint:1",
            "recorded no PO session",
            "NOTES.md",
            self.DOC,
            "Because the owner said so.",
        ):
            self.assertIn(expected, seed)
        [launch] = self.calls()
        self.assertEqual((launch["prompt"], launch["po_session"]), (seed, session.session_id))

    def test_a_session_missing_from_the_store_is_replaced_and_no_why_document_is_said(self) -> None:
        sprints = FakeSprints({"sprint:1": "gone-session"})
        service = self.resolver(sprints)

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        new = answer["session_id"]
        self.assertTrue(answer["created"])
        self.assertEqual(self.store().session(new).cli, "claude")
        self.assertEqual(
            list(sprints.comments.values()),
            [
                (
                    "sprint:1",
                    (
                        f"the PO session gone-session no longer exists; opened {new} seeded with "
                        "no why-document found and NOTES.md"
                    ),
                )
            ],
        )
        self.settled(new, 1)
        seed = self.first_prompt(new)
        self.assertIn("gone-session no longer exists", seed)
        self.assertIn("No why-document under state/knowledge/decisions/ names sprint:1", seed)

    def test_a_closed_session_is_replaced_with_its_own_cli_model_and_effort(self) -> None:
        sprints = FakeSprints({})
        service = self.resolver(sprints)
        old = service.create_session(cli="codex", model="gpt-5.6-sol", effort="high", request_id="c-old")
        service.close_session(session_id=old["session_id"], actor="owner")
        sprints.records["sprint:1"] = SprintRecord("sprint:1", "open", old["session_id"])

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        session = self.store().session(answer["session_id"])
        self.assertNotEqual(session.session_id, old["session_id"])
        self.assertEqual((session.cli, session.model, session.effort), ("codex", "gpt-5.6-sol", "high"))
        self.settled(session.session_id, 1)
        self.assertIn(f"{old['session_id']} is closed", self.first_prompt(session.session_id))
        self.assertEqual(sprints.records["sprint:1"].po_session, session.session_id)

    def test_a_closed_session_stored_with_default_is_replaced_at_the_first_offered_effort(self) -> None:
        sprints = FakeSprints({})
        service = self.resolver(sprints, efforts={"claude": ("max", "low"), "codex": ("medium",)})
        # A session opened before an effort had to be chosen: the store keeps its legacy `default`.
        old, _ = self.store().claim_session(
            session_id="legacy",
            cli="codex",
            model="gpt-5.6-sol",
            cwd=str(self.data / "po"),
            cli_session_id=None,
        )
        self.assertEqual(old.effort, po_store.DEFAULT_EFFORT)
        service.close_session(session_id="legacy", actor="owner")
        sprints.records["sprint:1"] = SprintRecord("sprint:1", "open", "legacy")

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        session = self.store().session(answer["session_id"])
        self.assertEqual((session.cli, session.model, session.effort), ("codex", "gpt-5.6-sol", "medium"))
        self.settled(session.session_id, 1)
        self.assertEqual(sprints.records["sprint:1"].po_session, session.session_id)

    def test_a_sprint_session_with_no_offered_effort_is_refused_and_opens_nothing(self) -> None:
        sprints = FakeSprints({"sprint:1": None})
        service = self.resolver(sprints, efforts={"claude": (), "codex": ("high",)})

        refused = service.handle({"op": "sprint_session", "sprint_ref": "sprint:1", "request_id": "r-1"})

        self.assertEqual(refused["error"]["code"], "validation")
        self.assertIn("explicit effort", refused["error"]["message"])
        self.assertEqual(list(self.board.sessions), [])
        self.assertIsNone(sprints.records["sprint:1"].po_session)

    def test_a_create_without_an_explicit_offered_effort_is_refused_with_nothing_written(self) -> None:
        service = self.service(run=False)
        for effort in (None, "", "default", "none", "turbo"):
            request = {"op": "create_session", "cli": "claude", "model": "opus", "request_id": f"c-{effort}"}
            if effort is not None:
                request["effort"] = effort
            with self.subTest(effort=effort):
                answer = service.handle(request)
                self.assertEqual(answer["error"]["code"], "validation")
                self.assertTrue(answer["error"]["nothing_written"])
                self.assertIn("high, low, medium, xhigh, max", answer["error"]["message"])
        self.assertEqual(list(self.board.sessions), [])

    def test_several_why_documents_are_listed_and_none_is_quoted(self) -> None:
        documents = [
            why("state/knowledge/decisions/a.md", "sprint:1 first TEXT-A"),
            why("state/knowledge/decisions/b.md", "sprint:1 second TEXT-B"),
        ]
        sprints = FakeSprints({"sprint:1": None}, documents={"sprint:1": documents})
        service = self.resolver(sprints)

        new = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")["session_id"]

        self.settled(new, 1)
        seed = self.first_prompt(new)
        self.assertIn("- state/knowledge/decisions/a.md", seed)
        self.assertIn("- state/knowledge/decisions/b.md", seed)
        self.assertNotIn("TEXT-A", seed)
        [(_, comment)] = sprints.comments.values()
        self.assertIn(
            "no single why-document (state/knowledge/decisions/a.md, state/knowledge/decisions/b.md)", comment
        )

    def test_a_repeat_replays_and_finishes_a_resolve_that_failed_part_way(self) -> None:
        sprints = FakeSprints({"sprint:1": None})
        service = self.resolver(sprints)
        sprints.fail["record_po_session"] = 1
        request = {"op": "sprint_session", "sprint_ref": "sprint:1", "request_id": "r-1"}

        failed = service.handle(request)

        self.assertEqual(failed["error"]["code"], "outcome_unknown")
        [opened] = self.board.sessions
        self.assertIsNone(sprints.records["sprint:1"].po_session)

        again = service.handle(request)
        once_more = service.handle(request)

        self.assertEqual(again["result"], {"session_id": opened, "created": True, "repeated": True})
        self.assertEqual(once_more["result"], again["result"])
        self.assertEqual(list(self.board.sessions), [opened])
        self.assertEqual(sprints.records["sprint:1"].po_session, opened)
        self.assertEqual(len(sprints.comments), 1)
        self.settled(opened, 1)
        self.assertEqual([role for _seq, role, _text in self.feed(opened)], ["owner", "agent"])
        self.assertEqual(self.queued(), [])
        # A later resolve under another id finds the recorded session open.
        later = service.sprint_session(sprint_ref="sprint:1", request_id="r-2")
        self.assertEqual((later["session_id"], later["created"]), (opened, False))
        with self.assertRaises(po_store.RequestConflict):
            service.sprint_session(sprint_ref="sprint:2", request_id="r-1")

    def test_two_resolves_of_one_sprint_open_one_session(self) -> None:
        sprints = FakeSprints({"sprint:1": "gone-session"})
        service = self.resolver(sprints)
        start = threading.Barrier(4)
        answers: list[dict] = []

        def resolve(request_id: str) -> None:
            start.wait(5)
            answers.append(service.sprint_session(sprint_ref="sprint:1", request_id=request_id))

        threads = [threading.Thread(target=resolve, args=(f"r-{index}",)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual(len(answers), 4)
        self.assertEqual(len({answer["session_id"] for answer in answers}), 1)
        self.assertEqual(sorted(answer["created"] for answer in answers), [False, False, False, True])
        self.assertEqual(len(self.board.sessions), 1)
        self.assertEqual(len(sprints.comments), 1)

    def test_an_unknown_or_closed_sprint_is_refused_with_nothing_written(self) -> None:
        sprints = FakeSprints({"sprint:done": None}, status={"sprint:done": "closed"})
        service = self.resolver(sprints)
        for ref in ("sprint:none", "sprint:done"):
            with self.subTest(ref=ref):
                answer = service.handle({"op": "sprint_session", "sprint_ref": ref, "request_id": f"r-{ref}"})
                self.assertEqual(answer["error"]["code"], "validation")
                self.assertTrue(answer["error"]["nothing_written"])
        self.assertEqual((self.board.sessions, sprints.comments), ({}, {}))

    def test_the_client_resolves_through_the_socket(self) -> None:
        sprints = FakeSprints({"sprint:1": None})
        service = self.resolver(sprints, run=False)
        with listening(service):
            client = PoServiceClient(self.data)
            first = client.sprint_session(sprint_ref="sprint:1", request_id="r-1")
            second = client.sprint_session(sprint_ref="sprint:1", request_id="r-1")
        self.assertTrue(first["created"])
        self.assertEqual((second["session_id"], second["repeated"]), (first["session_id"], True))

    def test_a_sprints_new_session_is_titled_with_the_sprint_ref(self) -> None:
        """secretary-1782: the resolver's session reads as its sprint on `/po`."""
        sprints = FakeSprints({"sprint:1467": None})
        service = self.resolver(sprints)

        answer = service.sprint_session(sprint_ref="sprint:1467", request_id="r-1")

        self.assertEqual(self.store().session(answer["session_id"]).title, "sprint:1467")
        self.settled(answer["session_id"], 1)

    def test_a_replacement_for_a_closed_sprint_session_gets_the_same_title(self) -> None:
        sprints = FakeSprints({})
        service = self.resolver(sprints)
        old = service.create_session(cli="claude", model="opus", effort="high", request_id="c-old")
        service.rename_session(session_id=old["session_id"], title="the owner's name for it")
        service.close_session(session_id=old["session_id"], actor="owner")
        sprints.records["sprint:1467"] = SprintRecord("sprint:1467", "open", old["session_id"])

        answer = service.sprint_session(sprint_ref="sprint:1467", request_id="r-1")

        self.assertNotEqual(answer["session_id"], old["session_id"])
        self.assertEqual(self.store().session(answer["session_id"]).title, "sprint:1467")
        # The closed session keeps what it was called.
        self.assertEqual(self.store().session(old["session_id"]).title, "the owner's name for it")
        self.settled(answer["session_id"], 1)


class RenameSessionTests(ServiceFixture):
    """`rename_session`: the one write of a session's title, for the web and the CLI (secretary-1782)."""

    def test_a_title_is_set_trimmed_repeated_and_cleared_through_the_socket(self) -> None:
        service = self.service()
        session_id = self.session(service)
        with listening(service):
            client = PoServiceClient(self.data)
            self.assertEqual(
                client.rename_session(session_id=session_id, title="  Sprint planning  "),
                {"session_id": session_id, "title": "Sprint planning"},
            )
            # Idempotent by value: a repeat answers the same and changes nothing.
            self.assertEqual(
                client.rename_session(session_id=session_id, title="Sprint planning"),
                {"session_id": session_id, "title": "Sprint planning"},
            )
            self.assertEqual(self.store().session(session_id).title, "Sprint planning")
            self.assertEqual(
                client.rename_session(session_id=session_id, title="   "),
                {"session_id": session_id, "title": None},
            )
        self.assertIsNone(self.store().session(session_id).title)
        # No request id and no `po_requests` row: only the create's.
        self.assertEqual(len(self.board.requests), 1)

    def test_a_refused_title_and_an_unknown_session_answer_with_their_reason_and_write_nothing(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.rename_session(session_id=session_id, title="kept")
        for fields, code, fragment in (
            ({"session_id": session_id, "title": "two\nlines"}, "validation", "one line"),
            ({"session_id": session_id, "title": "x" * 121}, "validation", "at most 120"),
            ({"session_id": session_id, "title": 7}, "validation", "title is text"),
            ({"session_id": "no-such", "title": "t"}, "session_not_found", "no-such"),
            ({"session_id": session_id}, "validation", "title"),
        ):
            with self.subTest(fields=fields):
                answer = service.handle({"op": "rename_session", **fields})
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["error"]["code"], code)
                self.assertIn(fragment, answer["error"]["message"])
        self.assertEqual(self.store().session(session_id).title, "kept")

    def test_a_closed_session_may_be_renamed(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.close_session(session_id=session_id, actor="owner")

        answer = service.handle({"op": "rename_session", "session_id": session_id, "title": "done"})

        self.assertEqual(answer, {"ok": True, "result": {"session_id": session_id, "title": "done"}})
        self.assertEqual(self.store().session(session_id).state, po_store.SESSION_CLOSED)

    def test_the_layer_turns_a_refusal_into_a_validation_refusal_with_the_reason(self) -> None:
        service = self.service()
        session_id = self.session(service)
        with listening(service):
            layer = PoLayer(self.root, data_dir=self.data, store=self.store(), models=MODELS)
            self.assertEqual(
                layer.po_rename(session_id=session_id, title="Roadmap"),
                {"kind": "po_session_renamed", "session_id": session_id, "title": "Roadmap"},
            )
            with self.assertRaisesRegex(ValidationRefused, "at most 120"):
                layer.po_rename(session_id=session_id, title="y" * 121)
            with self.assertRaises(PoSessionNotFound):
                layer.po_rename(session_id="no-such", title="t")


class SessionTitleRuleTests(unittest.TestCase):
    """`session_title`: the one rule, whoever sets the title."""

    def test_the_rule(self) -> None:
        for given, stored in (
            ("  Roadmap  ", "Roadmap"),
            ("", None),
            ("   ", None),
            (None, None),
            ("Спринт 1467 · план", "Спринт 1467 · план"),
            ("x" * 120, "x" * 120),
            ("  " + "x" * 120 + "  ", "x" * 120),
        ):
            with self.subTest(given=given):
                self.assertEqual(po_store.session_title(given), stored)
        for given in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x7fb", "a\x85b", "a\x9fb", "x" * 121):
            with self.subTest(given=given), self.assertRaises(po_store.TitleRefused):
                po_store.session_title(given)


class FakeStoreVocabularyTests(unittest.TestCase):
    def test_an_operation_outside_the_check_writes_nothing_as_postgresql_would(self) -> None:
        store = FakePoStore()
        with self.assertRaisesRegex(po_store.PoStoreError, "po_request_operation_in_vocabulary"):
            store.claim_session(
                session_id="s-1",
                cli="claude",
                model="opus",
                cwd="/po",
                cli_session_id=None,
                request_id="r-1",
                operation="po_not_in_the_check",
                fingerprint="f",
            )
        self.assertEqual((store.board.sessions, store.board.requests), ({}, {}))
        for operation in po_store.REQUEST_OPERATIONS:
            if operation != po_store.SEND:
                store.claim_session(
                    session_id=f"s-{operation}",
                    cli="claude",
                    model="opus",
                    cwd="/po",
                    cli_session_id=None,
                    request_id=f"r-{operation}",
                    operation=operation,
                    fingerprint="f",
                )
        self.assertEqual(len(store.board.sessions), 2)


class WhyDocumentTests(unittest.TestCase):
    def test_only_a_decision_that_names_the_whole_reference_is_found(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertEqual(find_why_documents(root, "sprint:14"), [])
        decisions = root / "state" / "knowledge" / "decisions"
        decisions.mkdir(parents=True)
        (decisions / "a.md").write_text("Why sprint:14 exists.\n", encoding="utf-8")
        (decisions / "b.md").write_text("About sprint:1465 and xsprint:14.\n", encoding="utf-8")
        (decisions / "c.md").write_text("(sprint:14)\n", encoding="utf-8")
        (decisions / "notes.txt").write_text("sprint:14\n", encoding="utf-8")

        found = find_why_documents(root, "sprint:14")

        self.assertEqual(
            [document.path for document in found],
            ["state/knowledge/decisions/a.md", "state/knowledge/decisions/c.md"],
        )
        self.assertEqual(found[0].text, "Why sprint:14 exists.\n")
        self.assertEqual(why_document_label(found[:1]), "state/knowledge/decisions/a.md")
        self.assertEqual(why_document_label([]), "no why-document found")


class UnitTemplateTests(unittest.TestCase):
    LAYOUT = SystemdLayout(
        product_root=Path("/opt/product"),
        instance_path=Path("/opt/instance"),
        data_dir=Path("/opt/data"),
        runtime_user="runner",
        runtime_home=Path("/opt/home"),
    )

    def unit(self, name: str, layout: SystemdLayout | None = None) -> str:
        return render_systemd_unit(
            (SHIPPED_PACKAGING_ROOT / name).read_bytes(), layout or self.LAYOUT
        ).decode()

    def test_the_unit_is_a_simple_always_restarted_service_with_the_web_units_templating(self) -> None:
        text = self.unit(PO_UNIT)
        self.assertIn("Type=simple", text)
        self.assertIn("Restart=always", text)
        self.assertIn("[Install]", text)
        self.assertIn("ExecStart=/opt/product/.venv/bin/ummanu po-serve --instance /opt/instance", text)
        directives = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
        for coupling in ("PartOf=", "BindsTo=", "Requires=", "ummanu-web"):
            self.assertNotIn(coupling, directives)

        def shared(unit: str) -> list[str]:
            keys = ("User=", "Group=", "WorkingDirectory=", "EnvironmentFile=", "Environment=")
            return [line for line in self.unit(unit).splitlines() if line.startswith(keys)]

        self.assertEqual(shared(PO_UNIT), shared("ummanu-web.service"))

    def test_it_is_planned_listed_by_doctor_and_can_be_opted_out(self) -> None:
        packaged = load_packaged_units(SHIPPED_PACKAGING_ROOT, "ummanu-", self.LAYOUT)
        instance = {"host": {"unit_prefix": "ummanu-"}}
        planned = {r.name: r for r in build_plan(instance, [], packaged=packaged) if r.kind == "unit"}
        self.assertIn(PO_UNIT, planned)
        self.assertEqual(json.loads(planned[PO_UNIT].spec)["installable"], "yes")
        expected = build_doctor_expectations(instance, [], packaged=packaged)
        self.assertIn(PO_UNIT, expected.units)
        self.assertEqual(expected.unit_runtime[PO_UNIT], (True, True))

        opted_out = {"host": {"unit_prefix": "ummanu-", "components": {"po": {"enabled": False}}}}
        names = {r.name for r in build_plan(opted_out, [], packaged=packaged)}
        self.assertNotIn(PO_UNIT, names)
        self.assertIn("ummanu-web.service", names)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_the_rendered_unit_passes_systemd_analyze_verify(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        executable = root / "product" / ".venv" / "bin" / "ummanu"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        layout = SystemdLayout(
            product_root=root / "product",
            instance_path=root / "instance",
            data_dir=root / "data",
            runtime_user=getpass.getuser(),
            runtime_home=Path.home(),
        )
        path = root / "units" / PO_UNIT
        path.parent.mkdir()
        path.write_text(self.unit(PO_UNIT, layout), encoding="utf-8")

        result = subprocess.run(
            ["systemd-analyze", "verify", "--man=no", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(PO_UNIT, result.stderr, "systemd warned about the unit")


class ThinWebTests(unittest.TestCase):
    def test_no_web_module_imports_the_runner_or_the_service(self) -> None:
        offenders = []
        for package in ("web", "webproto"):
            for path in sorted((ROOT / "src" / "ummanu" / package).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
                    for name in names:
                        if name in ("ummanu.po.runner", "ummanu.po.service"):
                            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
        self.assertEqual(offenders, [])

    def test_the_web_recovery_module_is_gone(self) -> None:
        self.assertFalse((ROOT / "src" / "ummanu" / "webproto" / "po_recovery.py").exists())


if __name__ == "__main__":
    unittest.main()
