"""Wait cards: created with a target, a deadline and return addresses; advanced by the dispatcher (secretary-1790).

Unit-level. The spec is `board/wait_card.py`, validated before anything is read. Every create refusal
runs `TaskWriter.create` over a mock client, which records any board write. The dispatcher side runs
the real `claim_ready_task`/`advance_wait_card` against an in-memory board of several cards that
deduplicates writes by request id the way `TaskWriter` does, a GitHub host that fails on anything but
`GET repos/{repo}/actions/runs/{id}`, and a PO channel that deduplicates inputs by request id the way
`PoService.submit` does; one crash case runs against a real `PoService` over its socket instead. The
PostgreSQL paths (the stored spec, `task show`, `task cancel`, the two dispatcher edges) are in
`tests/test_tasks.py`, and migration 0020 in `tests/test_board_store_schema.py`.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.po_card_fakes import DispatcherFixture, Forbidden, SprintView
from tests.po_fake_store import FakePoStore
from ummanu.board import wait_card
from ummanu.board.completion_evidence import has_candidate, is_headless, review_required
from ummanu.board.production_rights import WAIT_OUTCOME_INPUT, card_facts, facts_problem
from ummanu.board.wait_card import (
    CANCELLED,
    DEADLINE_PASSED,
    DELIVERED,
    RESULT_READY,
    SOURCE_UNREACHABLE,
    TARGET_REACHED,
    WAITING,
    WaitSpecError,
    build_wait_spec,
    cancel_text,
    wait_view,
)
from ummanu.cli import main
from ummanu.dispatch.claim import claim_ready_task
from ummanu.dispatch.production import (
    ProbeAbort,
    _budget_event_type,
    _probe_runtime,
    _production_claim_ready,
)
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import new_attempt_id
from ummanu.dispatch.types import HostError
from ummanu.dispatch.wait_cards import advance_wait_card, delivery_request_id, pending_wait_blockers
from ummanu.po import store as po_store
from ummanu.po.client import ServiceUnavailable
from ummanu.po.store import RequestConflict, SessionClosed, SessionNotFound
from ummanu.tasks import TaskError, TaskWriter

SPRINT = "sprint:1"
WAIT = "ummanu-1900"
DEPENDENT = "ummanu-1901"
REPO = "vladmesh/ummanu"
RUN_ID = 4242
RUN_PATH = f"repos/{REPO}/actions/runs/{RUN_ID}"
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
SESSION = "po-session-1"
#: The writer's own PO-store lookup, before any test replaces it.
PO_SESSION_STATE = TaskWriter._po_session_state


def spec(
    *, returns: tuple[str, ...] = ("observer",), deadline: str = "2h", **target: str
) -> wait_card.WaitSpec:
    target = target or {"run": REPO, "run_id": str(RUN_ID)}
    return build_wait_spec(**target, deadline=deadline, returns=returns, sprint=SPRINT, now=T0)


def wait_doc(the_spec: wait_card.WaitSpec, *, state: str = "ready", ref: str = WAIT) -> dict[str, Any]:
    return {
        "ref": ref,
        "id": 1900,
        "title": "Wait for the release run",
        "description": "",
        "type": "wait",
        "state": state,
        "project": "ummanu",
        "sprint": SPRINT,
        "review": "skipped",
        "blocked_by": None,
        "claim": {"worker": None},
        "workspace": {"slug": None},
        "comments": [],
        "extensions": {"extra": {"wait": the_spec.text()}},
    }


def plain_doc(
    ref: str,
    *,
    state: str = "ready",
    blocked_by: str | None = None,
    kind: str = "code",
    project: str = "ummanu",
) -> dict[str, Any]:
    return {
        "ref": ref,
        "id": int(ref.rsplit("-", 1)[1]),
        "title": f"card {ref}",
        "description": "",
        "type": kind,
        "state": state,
        "project": project,
        "sprint": SPRINT,
        "review": "required",
        "blocked_by": blocked_by,
        "claim": {"worker": None},
        "workspace": {"slug": None},
        "comments": [],
    }


def run(status: str, conclusion: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "conclusion": conclusion,
        "html_url": RUN_URL,
        "created_at": "2026-09-27T11:58:00Z",
        "run_started_at": "2026-09-27T11:58:05Z",
        "updated_at": "2026-09-27T12:20:00Z",
    }


class SimulatedCrash(Exception):
    """The dispatcher process died here."""


class WaitBoard:
    """Several cards as the dispatcher reads and writes them, deduplicating writes by request id.

    That dedup is the receiving side's for a dependent comment and for the terminal move:
    `TaskWriter` answers a request id it already committed as a replay and writes nothing.
    """

    def __init__(self, *cards: dict[str, Any]) -> None:
        self.cards = {card["ref"]: card for card in cards}
        self.log: list[dict[str, Any]] = []
        self.state_writes: list[dict[str, Any]] = []
        #: When set, the next wait-state write for which it answers true raises `SimulatedCrash`.
        self.crash_before: Any = None

    # reader
    def show(self, reference: str) -> dict[str, Any]:
        if reference not in self.cards:
            raise TaskError("not_found", "task was not found", 2)
        return copy.deepcopy(self.cards[reference])

    def list(self, states: set[str] | None = None, **_: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(card) for card in self.cards.values() if not states or card["state"] in states]

    # writer
    def claim(self, *, role: str, reference: str, worker: str, request_id: str, **_: Any) -> dict[str, Any]:
        if self.committed_event(request_id) is None:
            card = self.cards[reference]
            assert card["state"] == "ready", "claim requires a Ready task"
            card["state"], card["claim"] = "in_progress", {"worker": worker}
            self.log.append({"request_id": request_id, "ref": reference, "kind": "claim", "role": role})
        return {"action": "claimed"}

    def move(
        self, *, role: str, reference: str, target: str, reason: str, request_id: str, **fields: Any
    ) -> dict:
        if self.committed_event(request_id) is None:
            card = self.cards[reference]
            self.log.append(
                {
                    "request_id": request_id,
                    "ref": reference,
                    "kind": "move",
                    "role": role,
                    "from": card["state"],
                    "to": target,
                    "reason": reason,
                    **fields,
                }
            )
            card["state"] = target
            if reason:
                card["comments"].append({"marker": role, "body": f"[{role}]\n{reason}"})
        return {"action": "moved"}

    def comment(self, *, role: str, reference: str, body: str, request_id: str, **_: Any) -> dict[str, Any]:
        if self.committed_event(request_id) is None:
            self.cards[reference]["comments"].append({"marker": role, "body": f"[{role}]\n{body}"})
            self.log.append({"request_id": request_id, "ref": reference, "kind": "comment", "role": role})
        return {"action": "commented"}

    def record_wait_state(self, *, role: str, reference: str, state: str, **_: Any) -> None:
        assert role == "dispatcher" and self.cards[reference]["type"] == "wait"
        if self.crash_before is not None and self.crash_before(json.loads(state)):
            self.crash_before = None
            raise SimulatedCrash(f"crashed before recording {state}")
        self.cards[reference]["extensions"]["extra"]["wait_state"] = state
        self.state_writes.append(json.loads(state))

    # audit
    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return next((event for event in self.log if event["request_id"] == request_id), None)

    def events(self, reference: str = "", **_: Any) -> list[dict[str, Any]]:
        return [event for event in self.log if not reference or event["ref"] == reference]

    # what the other writers leave on a card
    def cancel(self, reference: str, reason: str, *, by: str = "po") -> None:
        self.cards[reference]["extensions"]["extra"]["wait_cancel"] = cancel_text(
            "2026-09-27T12:05:00Z", by, by, reason
        )

    def transition(self, reference: str, column: str, at: str) -> None:
        """Another writer moves a card; the audit records when (a released `moved` event)."""
        self.log.append(
            {
                "request_id": f"move-{reference}-{column}-{at}",
                "ref": reference,
                "kind": "moved",
                "outcome": "success",
                "payload": {"from": self.cards[reference]["state"], "to": column},
                "occurred_at": at,
            }
        )
        self.cards[reference]["state"] = column

    def moves(self, reference: str) -> list[dict[str, Any]]:
        return [event for event in self.log if event["kind"] == "move" and event["ref"] == reference]

    def comments(self, reference: str) -> list[str]:
        return [comment["body"] for comment in self.cards[reference]["comments"]]

    def wait_state(self) -> wait_card.WaitState:
        return wait_card.wait_state(self.cards[WAIT])


class ReadOnlyGitHub:
    """The dispatcher host as a wait card may use it: one `gh api` read of the run, and nothing else.

    Any other command, any write flag, and any other host operation (a workspace, a head) fails.
    Answers are served in order and the last one repeats: a run dict, `("http", <gh stderr>)` for an
    answered failure, or `("down", <message>)` for a command that never got an answer.
    """

    WRITE_FLAGS = frozenset({"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"})

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def run_capture(self, args: list[str], label: str, *, cwd: Any = None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if list(args[:3]) != ["gh", "api", RUN_PATH] or set(args) & self.WRITE_FLAGS:
            raise AssertionError(f"a wait card may only read its run, not run {args}")
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, dict):
            return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")
        kind, text = answer
        if kind == "down":
            raise HostError(text)
        return subprocess.CompletedProcess(args, 1, "", text)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a wait card reached the host: {name}")


class FakePo:
    """The PO channel as `PoService` answers it: inputs, sessions and the sprint resolver, by request id.

    `submit` deduplicates inputs by request id, and a closed or missing session refuses one before
    anything is reserved; `create_session` and `sprint_session` answer a repeated request id with the
    session it already made. `rows` are the sessions' CLI, model and effort.
    """

    def __init__(self, *, down: int = 0, closed: tuple[str, ...] = (), missing: tuple[str, ...] = ()) -> None:
        self.inputs: dict[str, dict[str, Any]] = {}
        self.submits: list[str] = []
        self.down = down
        self.sessions = {SESSION: "open", **{session: "closed" for session in closed}}
        for session in missing:
            self.sessions.pop(session, None)
        self.rows = {session: ("codex", "gpt-6-sol", "xhigh") for session in self.sessions}
        self.opened: dict[str, str] = {}
        self.creates: list[dict[str, str]] = []
        self.resolves: list[dict[str, str]] = []

    def submit(
        self, *, session_id: str, text: str, request_id: str, source: str, card: dict
    ) -> dict[str, Any]:
        self.submits.append(request_id)
        if self.down:
            self.down -= 1
            raise ServiceUnavailable("the PO service is not running (ummanu-po.service)")
        known = self.inputs.get(request_id)
        if known is not None:
            if (known["session_id"], known["text"], known["card"]) != (session_id, text, card):
                raise RequestConflict(f"request id {request_id} is bound to another input")
            return {"session_id": session_id, "queued": True, "repeated": True}
        if session_id not in self.sessions:
            raise SessionNotFound(f"there is no PO session {session_id}")
        if self.sessions[session_id] == "closed":
            raise SessionClosed(f"PO session {session_id} is closed; open a new session to continue")
        assert not facts_problem(card), facts_problem(card)
        self.inputs[request_id] = {"session_id": session_id, "text": text, "source": source, "card": card}
        return {"session_id": session_id, "queued": True, "repeated": False}

    def _open(self, request_id: str) -> dict[str, Any]:
        if request_id in self.opened:
            return {"session_id": self.opened[request_id], "created": True, "repeated": True}
        session = f"successor-{len(self.opened) + 1}"
        self.sessions[session] = "open"
        self.opened[request_id] = session
        return {"session_id": session, "created": True, "repeated": False}

    def create_session(self, *, cli: str, model: str, effort: str, request_id: str) -> dict[str, Any]:
        self.creates.append({"cli": cli, "model": model, "effort": effort, "request_id": request_id})
        answer = self._open(request_id)
        self.rows[answer["session_id"]] = (cli, model, effort)
        return answer

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]:
        self.resolves.append({"sprint_ref": sprint_ref, "request_id": request_id})
        return self._open(request_id)

    def successor_choice(self, session_id: str) -> tuple[str, str, str] | None:
        return self.rows.get(session_id) or ("claude", "fable", "high")

    def request(self, request_id: str) -> None:
        return None

    def queued(self, request_id: str) -> Any:
        known = self.inputs.get(request_id)
        return SimpleNamespace(session_id=known["session_id"]) if known else None


class Clock:
    def __init__(self, moment: datetime = T0) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def at(self, **delta: float) -> None:
        self.moment = T0 + timedelta(**delta)


class DispatcherCase(unittest.TestCase):
    """A dispatcher runtime around one wait card (and its dependents), rebuilt as often as a restart."""

    def setUp(self) -> None:
        self.clock = Clock()
        self.enterContext(mock.patch("ummanu.dispatch.wait_cards.utcnow", self.clock))

    def arrange(
        self, the_spec: wait_card.WaitSpec, *others: dict[str, Any], github: Any = None, po: Any = None
    ):
        self.board = WaitBoard(wait_doc(the_spec), *others)
        self.github = github if github is not None else ReadOnlyGitHub(run("in_progress"))
        self.po = po if po is not None else FakePo()

    def runtime(self) -> SimpleNamespace:
        """A new dispatcher process: nothing but the board, the PO channel and the host in common."""
        return SimpleNamespace(
            owner="ummanu-dispatcher",
            reader=self.board,
            writer=self.board,
            audit=self.board,
            po=self.po,
            host=self.github,
            catalog=Forbidden("catalog"),
            head_health=Forbidden("head health"),
            sprints=SprintView([]),
            save_records=lambda payload, records: self.fail("a wait card keeps no dispatcher record"),
        )

    def claim(self, runtime: Any = None) -> dict[str, Any]:
        runtime = runtime or self.runtime()
        self.records: dict[str, Any] = {}
        self.payload: dict[str, Any] = {}
        return claim_ready_task(runtime, self.board.show(WAIT), self.records, self.payload, new_attempt_id())

    def tick(self, runtime: Any = None) -> dict[str, Any]:
        runtime = runtime or self.runtime()
        return advance_wait_card(runtime, self.board.show(WAIT), {}, {}, new_attempt_id())

    def assertEndedDone(self, outcome: str) -> None:
        [move] = self.board.moves(WAIT)
        self.assertEqual((move["from"], move["to"], move["wait_outcome"]), ("in_progress", "done", outcome))
        self.assertEqual(self.board.cards[WAIT]["state"], "done")
        self.assertTrue(move["reason"].startswith(f"[wait:{outcome}]\n"))

    def assertEndedBlocked(self, outcome: str) -> None:
        [move] = self.board.moves(WAIT)
        self.assertEqual(
            (move["from"], move["to"], move["wait_outcome"]), ("in_progress", "blocked", outcome)
        )
        self.assertEqual(move["terminal_taxonomy"]["disposition"], "blocked")
        self.assertEqual(self.board.cards[WAIT]["state"], "blocked")
        self.assertTrue(move["reason"].startswith(f"[wait:{outcome}]\n"))


# --- create ------------------------------------------------------------------------------------


class SpecTests(unittest.TestCase):
    def test_each_target_kind_and_each_return_address(self) -> None:
        by_repo = spec(returns=("observer", "dependents", "po-session:s-1"))
        by_url = spec(run=RUN_URL)
        self.assertEqual(by_repo.target, by_url.target)
        self.assertEqual(
            by_repo.target.to_json(), {"kind": "github_run", "repo": REPO, "run_id": RUN_ID, "url": RUN_URL}
        )
        self.assertEqual(by_repo.returns, ("observer", "dependents", "po-session:s-1"))
        card = spec(card="ummanu-12", states="done, blocked")
        self.assertEqual(
            card.target.to_json(), {"kind": "card", "ref": "ummanu-12", "states": ["done", "blocked"]}
        )
        at = spec(until="2026-09-27T13:30:00+01:00")
        self.assertEqual(at.target.to_json(), {"kind": "time", "at": "2026-09-27T12:30:00Z"})
        self.assertEqual(
            (by_repo.deadline, by_repo.created_at, by_repo.transient_window_seconds),
            ("2026-09-27T14:00:00Z", "2026-09-27T12:00:00Z", 1800),
        )
        self.assertEqual(spec(deadline="2026-09-28T09:00:00Z").deadline, "2026-09-28T09:00:00Z")
        self.assertEqual(
            build_wait_spec(
                run=RUN_URL, deadline="1d", returns=["po-session:s-1"], transient_window="10m", now=T0
            ).transient_window_seconds,
            600,
        )
        # The spec round-trips through the card's extension bag, where it is JSON text.
        self.assertEqual(wait_card.wait_spec(wait_doc(by_repo)), by_repo)

    def test_every_malformed_input_is_refused_with_a_reason(self) -> None:
        cases = (
            ({"deadline": "2h", "returns": ["observer"]}, "exactly one target"),
            (
                {"run": RUN_URL, "until": "2026-09-27T13:00:00Z", "deadline": "2h", "returns": ["observer"]},
                "exactly one target, not --wait-run and --wait-until",
            ),
            (
                {"run": "not a repo", "deadline": "2h", "returns": ["observer"]},
                "neither owner/repo nor a run URL",
            ),
            ({"run": REPO, "deadline": "2h", "returns": ["observer"]}, "give the run with --wait-run-id"),
            (
                {"run": REPO, "run_id": "x1", "deadline": "2h", "returns": ["observer"]},
                "not a positive run id",
            ),
            (
                {"run": RUN_URL, "run_id": "7", "deadline": "2h", "returns": ["observer"]},
                "contradicts the run URL",
            ),
            (
                {"card": "Ummanu 12", "states": "done", "deadline": "2h", "returns": ["observer"]},
                "not a card reference",
            ),
            ({"card": "ummanu-12", "deadline": "2h", "returns": ["observer"]}, "needs --wait-states"),
            (
                {"card": "ummanu-12", "states": "finished", "deadline": "2h", "returns": ["observer"]},
                "unknown state",
            ),
            ({"until": "2026-09-27T13:00:00", "deadline": "2h", "returns": ["observer"]}, "names no zone"),
            (
                {"until": "2026-09-27T15:00:00Z", "deadline": "2h", "returns": ["observer"]},
                "after the deadline",
            ),
            ({"run": RUN_URL, "returns": ["observer"]}, "needs --wait-deadline"),
            ({"run": RUN_URL, "deadline": "2026-09-27T11:00:00Z", "returns": ["observer"]}, "already passed"),
            ({"run": RUN_URL, "deadline": "soon", "returns": ["observer"]}, "not an ISO-8601 time"),
            ({"run": RUN_URL, "deadline": "0m", "returns": ["observer"]}, "not a positive duration"),
            ({"run": RUN_URL, "deadline": "2h"}, "at least one --wait-return"),
            (
                {"run": RUN_URL, "deadline": "2h", "returns": ["owner"]},
                "not observer, dependents or po-session",
            ),
            (
                {"run": RUN_URL, "deadline": "2h", "returns": ["po-session:"]},
                "not observer, dependents or po-session",
            ),
        )
        for fields, message in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(WaitSpecError, message):
                build_wait_spec(**fields, sprint=SPRINT, now=T0)
        with self.assertRaisesRegex(WaitSpecError, "observer needs --sprint"):
            build_wait_spec(run=RUN_URL, deadline="2h", returns=["observer"], now=T0)


class CreateValidationTests(unittest.TestCase):
    """Every refusal of a wait create is decided before anything is written."""

    WRITES = ("createTask", "updateTask", "moveTaskPosition", "saveTaskMetadata", "createComment")

    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.client = mock.Mock(instance_dir=tmp)
        self.writer = TaskWriter(self.client, data_dir=tmp)
        sprint = {
            "ref": SPRINT,
            "status": "open",
            "repositories": ["ummanu"],
            "reservations": ["ummanu"],
        }
        self.enterContext(mock.patch("ummanu.sprints.SprintReader.show", return_value=sprint))
        self.sessions = {"s-open": "open", "s-closed": "closed"}
        self.enterContext(
            mock.patch.object(
                TaskWriter, "_po_session_state", side_effect=lambda sid: self.sessions.get(sid, "")
            )
        )

    def create(
        self, role: str = "po", *, task_type: str = "wait", wait: dict | None = None, **fields: Any
    ) -> dict:
        wait = {"run": RUN_URL, "deadline": "2h", "returns": ["dependents"]} if wait is None else wait
        return self.writer.create(
            role=role, actor=role, project="ummanu", task_type=task_type, title="T", wait=wait, **fields
        )

    def assertNothingWritten(self) -> None:
        written = [call for call in self.client.mock_calls if call.args and call.args[0] in self.WRITES]
        self.assertEqual(written, [])

    def test_each_refusal_is_a_typed_validation_error_and_writes_nothing(self) -> None:
        cases = (
            ({"wait": {"deadline": "2h", "returns": ["observer"]}, "sprint": SPRINT}, "exactly one target"),
            (
                {"wait": {"run": "nope", "deadline": "2h", "returns": ["observer"]}, "sprint": SPRINT},
                "neither owner/repo nor a run URL",
            ),
            ({"wait": {"run": RUN_URL, "returns": ["observer"]}, "sprint": SPRINT}, "needs --wait-deadline"),
            (
                {
                    "wait": {"run": RUN_URL, "deadline": "2020-01-01T00:00:00Z", "returns": ["observer"]},
                    "sprint": SPRINT,
                },
                "already passed",
            ),
            (
                {"wait": {"run": RUN_URL, "deadline": "2h", "returns": ["po-session:s-ghost"]}},
                "names no PO session",
            ),
            (
                {"wait": {"run": RUN_URL, "deadline": "2h", "returns": ["po-session:s-closed"]}},
                "names a closed PO session",
            ),
            (
                {"wait": {"run": RUN_URL, "deadline": "2h", "returns": ["observer"]}},
                "observer needs --sprint",
            ),
            (
                {"sprint": SPRINT, "head": "codex"},
                "takes no --head: no head runs it; the dispatcher advances it",
            ),
            ({"sprint": SPRINT, "review_head": "claude-opus"}, "takes no --review-head"),
            ({"sprint": SPRINT, "review": "required"}, "takes no --review required"),
            ({"sprint": SPRINT, "live_impact": True}, "takes no --live-impact"),
            ({"sprint": SPRINT, "seed_ref": "abc123", "supersedes": "ummanu-1"}, "takes no --seed-ref"),
            ({"sprint": SPRINT, "base_branch": "main"}, "takes no --base-branch"),
        )
        for fields, message in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(TaskError, message) as raised:
                self.create(**fields)
            self.assertEqual(raised.exception.code, "validation")
        with self.assertRaisesRegex(TaskError, "cuts a wait card for its own sprint") as raised:
            self.create("observer")
        self.assertEqual(raised.exception.code, "validation")
        self.assertNothingWritten()

    def test_only_the_observer_and_the_po_cut_one(self) -> None:
        for role in ("worker", "reviewer", "retro", "steward"):
            with self.subTest(role=role), self.assertRaisesRegex(TaskError, "cut by the observer or the PO"):
                self.create(role, sprint=SPRINT, target="issues")
        self.assertNothingWritten()

    def test_no_other_kind_takes_a_wait_flag(self) -> None:
        for kind in ("code", "research", "decision"):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(TaskError, "belong to a wait card") as raised,
            ):
                self.create("po", task_type=kind, sprint=SPRINT)
            self.assertEqual(raised.exception.code, "validation")
        self.assertNothingWritten()

    def test_the_po_session_is_looked_up_in_the_po_store(self) -> None:
        self.assertEqual(self.writer._po_session_state.side_effect("s-open"), "open")
        with (
            mock.patch.object(TaskWriter, "_po_session_state", PO_SESSION_STATE),
            mock.patch("ummanu.po.store.PoStore.for_instance") as store,
        ):
            store.return_value.session.side_effect = po_store.SessionNotFound("there is no PO session s-x")
            with self.assertRaisesRegex(TaskError, "names no PO session"):
                self.create(wait={"run": RUN_URL, "deadline": "2h", "returns": ["po-session:s-x"]})
            store.return_value.session.side_effect = po_store.PoStoreError("the board store did not answer")
            with self.assertRaisesRegex(TaskError, "cannot verify PO session s-x") as raised:
                self.create(wait={"run": RUN_URL, "deadline": "2h", "returns": ["po-session:s-x"]})
            self.assertEqual(raised.exception.code, "po_store_unavailable")
        self.assertNothingWritten()

    def test_the_kind_is_headless_skips_review_and_has_no_candidate(self) -> None:
        card = wait_doc(spec())
        self.assertTrue(is_headless(card))
        self.assertFalse(has_candidate(card))
        self.assertFalse(review_required(card))

    def test_the_cli_passes_the_wait_flags_and_the_cancel_through(self) -> None:
        writer = mock.Mock()
        writer.return_value.create.return_value = {"action": "created"}
        writer.return_value.cancel.return_value = {"action": "wait_cancelled"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("ummanu.tasks.TaskWriter", writer),
            mock.patch("ummanu.task_commands.card_client"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            created = main(
                [
                    "task",
                    "create",
                    "--role",
                    "po",
                    "--instance",
                    tmp,
                    "--data-dir",
                    tmp,
                    "--project",
                    "ummanu",
                    "--type",
                    "wait",
                    "--title",
                    "T",
                    "--wait-run",
                    REPO,
                    "--wait-run-id",
                    str(RUN_ID),
                    "--wait-deadline",
                    "3h",
                    "--wait-return",
                    "dependents",
                    "--wait-return",
                    "po-session:s-1",
                    "--wait-transient-window",
                    "10m",
                ]
            )
            reason = f"{tmp}/reason.md"
            with open(reason, "w", encoding="utf-8") as handle:
                handle.write("The release was withdrawn.\n")
            cancelled = main(
                [
                    "task",
                    "cancel",
                    "--ref",
                    WAIT,
                    "--role",
                    "observer",
                    "--instance",
                    tmp,
                    "--data-dir",
                    tmp,
                    "--reason-file",
                    reason,
                    "--request-id",
                    "cancel-1",
                ]
            )
            with contextlib.redirect_stderr(io.StringIO()):
                refused = main(["task", "cancel", "--ref", WAIT, "--role", "worker", "--reason", "x"])
        self.assertEqual((created, cancelled, refused), (0, 0, 2))
        self.assertEqual(
            writer.return_value.create.call_args.kwargs["wait"],
            {
                "run": REPO,
                "run_id": str(RUN_ID),
                "card": "",
                "states": "",
                "until": "",
                "deadline": "3h",
                "returns": ["dependents", "po-session:s-1"],
                "transient_window": "10m",
            },
        )
        kwargs = writer.return_value.cancel.call_args.kwargs
        self.assertEqual(
            (kwargs["reference"], kwargs["role"], kwargs["reason"], kwargs["request_id"]),
            (WAIT, "observer", "The release was withdrawn.\n", "cancel-1"),
        )


# --- the dispatcher ----------------------------------------------------------------------------


class ClaimTests(DispatcherCase):
    def test_the_claim_cuts_no_workspace_launches_no_head_and_reads_the_run_with_get_only(self) -> None:
        self.arrange(spec())

        outcome = self.claim()

        self.assertEqual((outcome["step"], outcome["action"]), ("wait-card", "wait-waiting"))
        self.assertEqual(self.board.cards[WAIT]["state"], "in_progress")
        self.assertEqual([event["kind"] for event in self.board.log], ["claim"])
        # No dispatcher record, so no workspace, handle or head to own.
        self.assertEqual(self.records, {})
        self.assertEqual(self.github.calls, [["gh", "api", RUN_PATH, "--jq", mock.ANY]])
        state = self.board.wait_state()
        self.assertEqual(
            (state.since, state.observation), ("2026-09-27T12:00:00Z", f"run {REPO}#{RUN_ID} is in_progress")
        )
        self.assertIsNone(state.result)

    def test_an_unchanged_observation_writes_nothing(self) -> None:
        self.arrange(spec())
        self.claim()
        self.clock.at(minutes=1)
        self.tick()
        self.tick()
        self.assertEqual(len(self.board.state_writes), 1)
        self.assertEqual(len(self.github.calls), 3)

    def test_the_tick_hands_a_wait_card_to_its_own_lane_before_any_head_path(self) -> None:
        with mock.patch(
            "ummanu.dispatch.runtime._advance_wait_card", return_value={"action": "wait"}
        ) as lane:
            outcome = DispatcherRuntime._tick_task(
                SimpleNamespace(), wait_doc(spec(), state="in_progress"), {}, {}, "a"
            )
        self.assertEqual((outcome, lane.call_count), ({"action": "wait"}, 1))

    def test_the_probe_aborts_before_a_wait_state_write(self) -> None:
        probe = _probe_runtime(
            SimpleNamespace(writer=object(), host=object(), production_state=object(), po=object())
        )
        with self.assertRaises(ProbeAbort):
            probe.writer.record_wait_state(role="dispatcher", actor="d", reference=WAIT, state="{}")


class TargetReachedTests(DispatcherCase):
    RETURNS = ("observer", "dependents", f"po-session:{SESSION}")

    def test_a_concluded_run_is_frozen_delivered_everywhere_and_the_card_is_done(self) -> None:
        for conclusion in ("success", "failure"):
            with self.subTest(conclusion=conclusion):
                self.arrange(
                    spec(returns=self.RETURNS),
                    plain_doc(DEPENDENT, blocked_by=WAIT),
                    github=ReadOnlyGitHub(run("in_progress"), run("completed", conclusion)),
                )
                self.claim()

                outcome = self.tick()

                self.assertEqual(
                    (outcome["action"], outcome["outcome"]), ("wait-target-reached", TARGET_REACHED)
                )
                result = self.board.wait_state().result
                self.assertEqual(
                    (result["outcome"], result["fact"]["conclusion"]), (TARGET_REACHED, conclusion)
                )
                self.assertEqual(result["evidence"], RUN_URL)
                [move] = self.board.moves(WAIT)
                self.assertEqual((move["to"], move["wait_outcome"]), ("done", TARGET_REACHED))
                self.assertIn(f"Conclusion: {conclusion}", move["reason"])
                self.assertIn(f"Evidence: {RUN_URL}", move["reason"])
                [delivered] = self.po.inputs.values()
                self.assertEqual(delivered["session_id"], SESSION)
                self.assertEqual(delivered["source"], "dispatcher")
                self.assertEqual(delivered["card"]["input"], WAIT_OUTCOME_INPUT)
                for line in (
                    f"Card: {WAIT}",
                    f"Outcome: {TARGET_REACHED}",
                    f"Conclusion: {conclusion}",
                    RUN_URL,
                ):
                    self.assertIn(line, delivered["text"])
                # The dependent is claimable again, with the result on it, and was not moved.
                [note] = [body for body in self.board.comments(DEPENDENT) if "[wait:" in body]
                self.assertIn(f"concluded {conclusion}", note)
                self.assertEqual(
                    (self.board.cards[DEPENDENT]["state"], self.board.moves(DEPENDENT)), ("ready", [])
                )
                self.assertEqual(pending_wait_blockers(self.runtime(), self.board.show(DEPENDENT)), [])

    def test_a_card_event_and_a_time_are_targets_too(self) -> None:
        self.arrange(
            spec(card="ummanu-7", states="done,blocked"), plain_doc("ummanu-7", state="validate")
        )
        self.claim()
        self.assertEqual(self.board.wait_state().observation, "ummanu-7 is validate")
        self.board.transition("ummanu-7", "blocked", "2026-09-27T12:10:00Z")
        self.assertEqual(self.tick()["action"], "wait-target-reached")
        self.assertEqual(
            self.board.wait_state().result["fact"],
            {"ref": "ummanu-7", "state": "blocked", "entered_at": "2026-09-27T12:10:00Z"},
        )

        self.arrange(spec(until="2026-09-27T12:30:00Z"))
        self.assertEqual(self.claim()["action"], "wait-waiting")
        self.clock.at(minutes=30)
        self.assertEqual(self.tick()["action"], "wait-target-reached")
        self.assertEqual(self.board.wait_state().result["summary"], "the time 2026-09-27T12:30:00Z arrived")

    def test_the_first_terminal_fact_stands(self) -> None:
        self.arrange(
            spec(returns=(f"po-session:{SESSION}",)),
            po=FakePo(down=1),
            github=ReadOnlyGitHub(run("completed", "failure"), run("completed", "success")),
        )
        self.claim()
        frozen = self.board.wait_state().result
        self.board.cancel(WAIT, "too late to matter")
        self.clock.at(hours=5)

        self.tick()

        self.assertEqual(self.board.wait_state().result, frozen)
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.board.moves(WAIT)[0]["to"], "done")


class RestartTests(DispatcherCase):
    def test_a_new_runtime_continues_a_pending_run_from_the_card_and_delivers_once(self) -> None:
        for conclusion in ("success", "failure"):
            with self.subTest(conclusion=conclusion):
                self.clock.at()
                returns = ("observer", "dependents", f"po-session:{SESSION}")
                self.arrange(spec(returns=returns), plain_doc(DEPENDENT, blocked_by=WAIT))
                self.claim(self.runtime())
                self.assertIsNone(self.board.wait_state().result)

                # The old process is gone: a new runtime, a new host and a new PO client, and no
                # record of any kind. The board is the only thing the two share.
                self.github = ReadOnlyGitHub(run("completed", conclusion))
                persisted, audit = copy.deepcopy(self.board.cards), copy.deepcopy(self.board.log)
                self.board = WaitBoard(*persisted.values())
                self.board.log = audit
                self.clock.at(minutes=40)

                outcome = self.tick(self.runtime())
                again = self.tick(self.runtime())

                self.assertEqual(outcome["action"], "wait-target-reached")
                self.assertEqual(again["action"], "wait-closed")
                self.assertEqual(self.board.wait_state().since, "2026-09-27T12:00:00Z")
                self.assertEqual(self.board.wait_state().result["fact"]["conclusion"], conclusion)
                self.assertEqual(len(self.po.inputs), 1)
                self.assertEqual(
                    len([body for body in self.board.comments(DEPENDENT) if "[wait:" in body]), 1
                )
                self.assertEqual([move["to"] for move in self.board.moves(WAIT)], ["done"])
                self.assertEqual(len(self.github.calls), 1)


class DuplicateDeliveryTests(DispatcherCase):
    RETURNS = ("dependents", f"po-session:{SESSION}", "observer")

    def crash_then_restart(self, crash_before: Any) -> None:
        self.arrange(
            spec(returns=self.RETURNS),
            plain_doc(DEPENDENT, blocked_by=WAIT),
            github=ReadOnlyGitHub(run("completed", "failure")),
        )
        self.board.crash_before = crash_before
        with self.assertRaises(SimulatedCrash):
            self.claim()
        self.assertIsNotNone(self.board.wait_state().result)
        self.assertEqual(self.board.cards[WAIT]["state"], "in_progress")

        self.tick(self.runtime())

    def assertDeliveredOnce(self) -> None:
        key = self.board.wait_state().result["key"]
        self.assertEqual(len(self.po.inputs), 1)
        self.assertEqual(
            set(self.po.submits), {delivery_request_id(WAIT, "po", f"po-session:{SESSION}", key)}
        )
        self.assertEqual(len([body for body in self.board.comments(DEPENDENT) if "[wait:" in body]), 1)
        self.assertEqual(len(self.board.moves(WAIT)), 1)
        self.assertEqual(
            len([body for body in self.board.comments(WAIT) if "[wait:target_reached]" in body]), 1
        )
        self.assertEqual(self.board.wait_state().deliveries.keys(), {"dependents", f"po-session:{SESSION}"})

    def test_a_crash_after_the_dependents_took_it_repeats_nothing(self) -> None:
        self.crash_then_restart(lambda state: "dependents" in state["deliveries"])
        self.assertEqual(len(self.board.log), 1 + 1 + 1)  # claim, one comment, the terminal move
        self.assertDeliveredOnce()

    def test_a_crash_after_the_po_took_it_repeats_nothing(self) -> None:
        self.crash_then_restart(lambda state: f"po-session:{SESSION}" in state["deliveries"])
        self.assertEqual(len(self.po.submits), 2)  # the repeat, answered as the same input
        self.assertDeliveredOnce()

    def test_a_crash_before_the_terminal_move_was_recorded_moves_once(self) -> None:
        self.arrange(spec(returns=self.RETURNS), github=ReadOnlyGitHub(run("completed", "success")))
        original = self.board.move

        def dies_after_the_move(**fields: Any) -> dict:
            original(**fields)
            raise SimulatedCrash("died with the move committed")

        self.board.move = dies_after_the_move  # type: ignore[method-assign]
        with self.assertRaises(SimulatedCrash):
            self.claim()
        self.board.move = original  # type: ignore[method-assign]
        self.assertEqual(self.tick()["action"], "wait-closed")
        self.assertEqual(len(self.board.moves(WAIT)), 1)


class RealPoServiceDeliveryTests(DispatcherFixture):
    """The PO side of exactly-once against the real service: the repeat is its own request-id dedup."""

    def test_a_crash_after_the_service_took_it_leaves_one_input_in_the_session(self) -> None:
        service = self.start()
        session = self.session(service)
        clock = Clock()
        self.enterContext(mock.patch("ummanu.dispatch.wait_cards.utcnow", clock))
        from ummanu.dispatch.po_cards import ServicePoChannel

        channel = ServicePoChannel(self.data, None)
        channel._store = FakePoStore(self.board)
        board = WaitBoard(wait_doc(spec(returns=(f"po-session:{session}",))))
        board.crash_before = lambda state: bool(state["deliveries"])
        runtime = SimpleNamespace(
            owner="d",
            reader=board,
            writer=board,
            audit=board,
            po=channel,
            host=ReadOnlyGitHub(run("completed", "failure")),
            save_records=None,
        )
        with self.assertRaises(SimulatedCrash):
            claim_ready_task(runtime, board.show(WAIT), {}, {}, new_attempt_id())

        outcome = advance_wait_card(runtime, board.show(WAIT), {}, {}, new_attempt_id())

        self.assertEqual(outcome["action"], "wait-target-reached")
        self.settled(session, 1)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 1)
        [call] = [call for call in self.calls() if "Wait card" in json.dumps(call)]
        self.assertIn("Conclusion: failure", json.dumps(call))

    def test_a_closed_session_is_succeeded_and_the_successor_takes_the_same_delivery_id(self) -> None:
        service = self.start()
        closed = self.session(service)
        service.close_session(session_id=closed, actor="owner")
        self.enterContext(mock.patch("ummanu.dispatch.wait_cards.utcnow", Clock()))
        from ummanu.dispatch.po_cards import ServicePoChannel

        channel = ServicePoChannel(self.data, None)
        channel._store = FakePoStore(self.board)
        board = WaitBoard(wait_doc(spec(returns=(f"po-session:{closed}",), until="2026-09-27T12:00:00Z")))
        runtime = SimpleNamespace(
            owner="d",
            reader=board,
            writer=board,
            audit=board,
            po=channel,
            host=Forbidden("host"),
            save_records=None,
            sprints=SimpleNamespace(show=lambda ref, **_: {"ref": ref, "status": "open", "po_session": None}),
        )

        outcome = claim_ready_task(runtime, board.show(WAIT), {}, {}, new_attempt_id())

        self.assertEqual(outcome["action"], "wait-target-reached")
        record = wait_card.wait_state(board.cards[WAIT]).deliveries[f"po-session:{closed}"]
        successor = record["session"]
        self.assertNotEqual(successor, closed)
        store = FakePoStore(self.board)
        before, after = store.session(closed), store.session(successor)
        self.assertEqual((after.cli, after.model, after.effort), (before.cli, before.model, before.effort))
        self.settled(successor, 1)
        self.assertEqual((len(store.turns(closed)), len(store.turns(successor))), (0, 1))
        self.assertEqual(store.request(record["detail"]).session_id, successor)


class OtherOutcomeTests(DispatcherCase):
    RETURNS = ("observer", "dependents", f"po-session:{SESSION}")

    def arrange_all(self, the_spec: wait_card.WaitSpec, github: Any = None) -> None:
        self.arrange(
            the_spec,
            plain_doc(DEPENDENT, blocked_by=WAIT),
            plain_doc("ummanu-1902", state="issues", blocked_by=f"ummanu-3,{WAIT}"),
            github=github,
        )

    def assertDeliveredEverywhere(self, outcome: str) -> None:
        if outcome == CANCELLED:
            self.assertEndedDone(outcome)
        else:
            self.assertEndedBlocked(outcome)
            self.assertNotIn("done", [move["to"] for move in self.board.log if move["kind"] == "move"])
        [delivered] = self.po.inputs.values()
        self.assertIn(f"Outcome: {outcome}", delivered["text"])
        # A Ready dependent is Blocked with the outcome; one in Issues gets the comment only.
        [held] = self.board.moves(DEPENDENT)
        self.assertEqual((held["from"], held["to"], held["wait_outcome"]), ("ready", "blocked", outcome))
        self.assertIn(outcome, held["reason"])
        for ref in (DEPENDENT, "ummanu-1902"):
            self.assertEqual(
                len([body for body in self.board.comments(ref) if f"[wait:{outcome}]" in body]), 1
            )
        self.assertEqual(self.board.cards["ummanu-1902"]["state"], "issues")
        self.assertEqual(wait_view(self.board.show(WAIT))["state"], outcome)

    def test_cancelled(self) -> None:
        self.arrange_all(spec(returns=self.RETURNS))
        self.claim()
        self.board.cancel(WAIT, "the release was withdrawn")
        self.assertEqual(self.tick()["action"], "wait-ended")
        self.assertDeliveredEverywhere(CANCELLED)
        self.assertEqual(self.board.wait_state().result["fact"]["reason"], "the release was withdrawn")

    def test_a_card_cancelled_while_ready_is_claimed_and_ended_without_reading_the_run(self) -> None:
        self.arrange_all(spec(returns=self.RETURNS))
        self.board.cancel(WAIT, "never mind")
        self.claim()
        self.assertEqual(self.github.calls, [])
        self.assertDeliveredEverywhere(CANCELLED)

    def test_deadline_passed(self) -> None:
        self.arrange_all(spec(returns=self.RETURNS, deadline="30m"))
        self.claim()
        self.clock.at(minutes=29)
        self.assertEqual(self.tick()["action"], "wait-waiting")
        self.clock.at(minutes=30)
        self.tick()
        self.assertDeliveredEverywhere(DEADLINE_PASSED)
        self.assertIn("last observation: run", self.board.wait_state().result["summary"])

    def test_a_definitive_404_is_source_unreachable_at_once(self) -> None:
        self.arrange_all(
            spec(returns=self.RETURNS), github=ReadOnlyGitHub(("http", "gh: Not Found (HTTP 404)"))
        )
        self.claim()
        self.assertDeliveredEverywhere(SOURCE_UNREACHABLE)
        self.assertIn("HTTP 404", self.board.wait_state().result["summary"])

    def test_no_access_is_source_unreachable_and_a_rate_limit_is_not(self) -> None:
        self.arrange(
            spec(), github=ReadOnlyGitHub(("http", "gh: API rate limit exceeded for installation (HTTP 403)"))
        )
        self.claim()
        self.assertIsNone(self.board.wait_state().result)
        self.arrange(
            spec(), github=ReadOnlyGitHub(("http", "gh: Resource not accessible by integration (HTTP 403)"))
        )
        self.claim()
        self.assertEqual(self.board.wait_state().result["outcome"], SOURCE_UNREACHABLE)

    def test_a_card_target_that_does_not_exist_is_source_unreachable(self) -> None:
        self.arrange(spec(card="ummanu-77", states="done"))
        self.claim()
        self.assertEqual(
            self.board.wait_state().result["summary"],
            "the source is unreachable: card ummanu-77 does not exist",
        )

    def test_transient_errors_are_recorded_and_retried_until_their_window_ends_it(self) -> None:
        failing = ReadOnlyGitHub(("http", "gh: Server Error (HTTP 502)"), ("down", "timed out after 60s"))
        self.arrange_all(
            build_wait_spec(
                run=RUN_URL,
                deadline="3h",
                returns=self.RETURNS,
                transient_window="20m",
                sprint=SPRINT,
                now=T0,
            ),
            github=failing,
        )
        self.claim()
        state = self.board.wait_state()
        self.assertIsNone(state.result)
        self.assertEqual(
            (state.error_since, state.error_at), ("2026-09-27T12:00:00Z", "2026-09-27T12:00:00Z")
        )
        self.assertIn("HTTP 502", state.error)
        self.assertIn("HTTP 502", wait_view(self.board.show(WAIT))["last_error"]["text"])
        self.clock.at(minutes=10)
        self.tick()
        state = self.board.wait_state()
        self.assertEqual((state.result, state.error_since), (None, "2026-09-27T12:00:00Z"))
        self.assertIn("timed out after 60s", state.error)
        self.assertEqual(self.board.cards[WAIT]["state"], "in_progress")
        self.clock.at(minutes=20)
        self.tick()
        self.assertDeliveredEverywhere(SOURCE_UNREACHABLE)
        self.assertEqual(self.board.wait_state().result["fact"]["window_seconds"], 1200)

    def test_an_answer_between_errors_restarts_the_window(self) -> None:
        self.arrange(
            build_wait_spec(
                run=RUN_URL,
                deadline="3h",
                returns=["observer"],
                transient_window="20m",
                sprint=SPRINT,
                now=T0,
            ),
            github=ReadOnlyGitHub(("down", "reset"), run("queued"), ("down", "reset")),
        )
        self.claim()
        self.clock.at(minutes=15)
        self.tick()
        self.assertEqual(self.board.wait_state().error, "")
        self.clock.at(minutes=30)
        self.tick()
        self.clock.at(minutes=45)
        self.tick()
        self.assertIsNone(self.board.wait_state().result)
        self.assertEqual(self.board.wait_state().error_since, "2026-09-27T12:30:00Z")

    def test_the_window_never_runs_past_the_deadline(self) -> None:
        self.arrange(
            build_wait_spec(
                run=RUN_URL,
                deadline="15m",
                returns=["observer"],
                transient_window="1h",
                sprint=SPRINT,
                now=T0,
            ),
            github=ReadOnlyGitHub(("down", "reset")),
        )
        self.claim()
        self.clock.at(minutes=15)
        self.tick()
        self.assertEndedBlocked(DEADLINE_PASSED)

    def test_a_po_service_that_is_down_postpones_the_delivery_and_loses_nothing(self) -> None:
        self.arrange(
            spec(returns=(f"po-session:{SESSION}", "observer"), until="2026-09-27T12:00:00Z"),
            po=FakePo(down=2),
        )
        first = self.claim()
        second = self.tick()
        self.assertEqual([first["status"], second["status"]], ["degraded", "degraded"])
        self.assertEqual(self.board.cards[WAIT]["state"], "in_progress")
        self.assertEqual(wait_view(self.board.show(WAIT))["state"], RESULT_READY)
        self.assertEqual(self.tick()["action"], "wait-target-reached")
        self.assertEqual(len(set(self.po.submits)), 1)
        self.assertEqual(len(self.po.inputs), 1)


class DeadlineTests(DispatcherCase):
    """The deadline is the cutoff: past it, only `deadline_passed`, unless the source proves otherwise."""

    def run_seen_late(self, updated_at: str) -> None:
        completed = {**run("completed", "failure"), "updated_at": updated_at}
        self.arrange(spec(deadline="30m"), github=ReadOnlyGitHub(run("in_progress"), completed))
        self.claim()
        # The dispatcher did not tick again until an hour later.
        self.clock.at(hours=1)
        self.tick()

    def test_a_run_that_completed_after_the_deadline_is_deadline_passed(self) -> None:
        self.run_seen_late("2026-09-27T12:45:00Z")
        self.assertEndedBlocked(DEADLINE_PASSED)
        result = self.board.wait_state().result
        self.assertEqual(result["fact"]["seen_after_deadline"], TARGET_REACHED)
        self.assertIn("seen after it: run", result["summary"])

    def test_a_run_whose_completion_time_is_before_the_deadline_is_target_reached(self) -> None:
        for updated_at in ("2026-09-27T12:20:00Z", "2026-09-27T12:30:00Z"):
            with self.subTest(updated_at=updated_at):
                self.clock.at()
                self.run_seen_late(updated_at)
                self.assertEqual(self.board.wait_state().result["outcome"], TARGET_REACHED)
                self.assertEqual([move["to"] for move in self.board.moves(WAIT)], ["done"])

    def test_a_404_first_seen_after_the_deadline_is_deadline_passed(self) -> None:
        self.arrange(
            spec(deadline="30m"), github=ReadOnlyGitHub(run("queued"), ("http", "gh: Not Found (HTTP 404)"))
        )
        self.claim()
        self.clock.at(minutes=31)
        self.tick()
        self.assertEndedBlocked(DEADLINE_PASSED)

    def test_a_transient_window_or_a_cancel_seen_after_the_deadline_is_deadline_passed(self) -> None:
        self.arrange(
            build_wait_spec(
                run=RUN_URL,
                deadline="30m",
                returns=["observer"],
                transient_window="10m",
                sprint=SPRINT,
                now=T0,
            ),
            github=ReadOnlyGitHub(("down", "reset")),
        )
        self.claim()
        self.clock.at(minutes=45)
        self.tick()
        self.assertEndedBlocked(DEADLINE_PASSED)

        self.arrange(spec(deadline="30m"))
        self.claim()
        self.board.cancel(WAIT, "withdrawn")
        self.clock.at(minutes=40)
        self.tick()
        self.assertEndedBlocked(DEADLINE_PASSED)

    def test_a_card_event_counts_by_the_time_the_audit_gives_its_transition(self) -> None:
        for entered, outcome in (
            ("2026-09-27T12:25:00Z", TARGET_REACHED),
            ("2026-09-27T12:35:00Z", DEADLINE_PASSED),
        ):
            with self.subTest(entered=entered):
                self.clock.at()
                self.arrange(
                    spec(card="ummanu-7", states="done", deadline="30m"), plain_doc("ummanu-7")
                )
                self.claim()
                self.board.transition("ummanu-7", "done", entered)
                self.clock.at(hours=1)
                self.tick()
                self.assertEqual(self.board.wait_state().result["outcome"], outcome)

    def test_a_time_target_at_or_before_the_deadline_is_reached_however_late_it_is_seen(self) -> None:
        self.arrange(spec(until="2026-09-27T12:30:00Z", deadline="30m"))
        self.clock.at(hours=2)
        self.claim()
        self.assertEqual(self.board.wait_state().result["outcome"], TARGET_REACHED)

    def test_every_observation_goes_through_the_one_freezing_function(self) -> None:
        from ummanu.dispatch import wait_cards

        cases = (
            (spec(), ReadOnlyGitHub(run("completed", "success"))),
            (spec(), ReadOnlyGitHub(("http", "gh: Not Found (HTTP 404)"))),
            (spec(until="2026-09-27T12:00:00Z"), None),
            (spec(card="ummanu-77", states="done"), None),
        )
        for the_spec, github in cases:
            with self.subTest(target=the_spec.target.kind):
                self.clock.at()
                self.arrange(the_spec, github=github)
                with mock.patch.object(wait_cards, "_settle", wraps=wait_cards._settle) as settle:
                    self.claim()
                self.assertEqual(settle.call_count, 1)
                self.assertIsNotNone(self.board.wait_state().result)


class ClosedSessionTests(DispatcherCase):
    """A closed or missing return session never swallows a result: it gets one successor."""

    ADDRESS = f"po-session:{SESSION}"

    def arrange_closed(self, **po: Any) -> None:
        self.arrange(spec(returns=(self.ADDRESS, "observer"), until="2026-09-27T12:00:00Z"), po=FakePo(**po))

    def assertTakenOnceBySuccessor(self) -> None:
        [delivered] = self.po.inputs.values()
        self.assertEqual(delivered["session_id"], "successor-1")
        self.assertEqual(set(self.po.opened.values()), {"successor-1"})
        record = self.board.wait_state().deliveries[self.ADDRESS]
        self.assertEqual((record["status"], record["session"]), ("accepted", "successor-1"))
        self.assertEqual(
            wait_view(self.board.show(WAIT))["po_sessions"],
            {self.ADDRESS: {"addressed": SESSION, "received_by": "successor-1"}},
        )
        [move] = self.board.moves(WAIT)
        self.assertEqual(move["to"], "done")
        self.assertIn(f"{self.ADDRESS}: accepted", move["reason"])
        self.assertIn("taken by PO session successor-1", move["reason"])

    def test_a_closed_session_gets_a_successor_with_its_cli_model_and_effort(self) -> None:
        for po in ({"closed": (SESSION,)}, {"missing": (SESSION,)}):
            with self.subTest(po=po):
                self.arrange_closed(**po)
                self.claim()
                self.assertTakenOnceBySuccessor()
                [create] = self.po.creates
                expected = ("codex", "gpt-6-sol", "xhigh") if "closed" in po else ("claude", "fable", "high")
                self.assertEqual((create["cli"], create["model"], create["effort"]), expected)
                self.assertEqual(self.po.resolves, [])

    def test_a_crash_between_opening_the_successor_and_the_submit_repeats_nothing(self) -> None:
        # Crash before the successor's id is on the card: the repeat asks again under the same id.
        self.arrange_closed(closed=(SESSION,))
        self.board.crash_before = lambda state: bool(
            (state["successors"].get(self.ADDRESS) or {}).get("session")
        )
        with self.assertRaises(SimulatedCrash):
            self.claim()
        self.assertEqual((len(self.po.opened), self.po.inputs), (1, {}))
        self.tick(self.runtime())
        self.assertTakenOnceBySuccessor()
        self.assertEqual(len({create["request_id"] for create in self.po.creates}), 1)

        # Crash with the successor recorded, before the submit reached it: the repeat submits there.
        self.clock.at()
        self.arrange_closed(closed=(SESSION,))
        original = self.po.submit

        def dies_on_the_successor(**fields: Any) -> dict:
            if fields["session_id"] == "successor-1":
                self.po.submit = original
                raise SimulatedCrash("died before the submit")
            return original(**fields)

        self.po.submit = dies_on_the_successor
        with self.assertRaises(SimulatedCrash):
            self.claim()
        self.tick(self.runtime())
        self.assertTakenOnceBySuccessor()
        self.assertEqual(len(self.po.creates), 1)

    def test_a_sprints_own_closed_session_goes_through_its_resolver(self) -> None:
        self.arrange_closed(closed=(SESSION,))
        runtime = self.runtime()
        runtime.sprints = SimpleNamespace(
            show=lambda ref, **_: {"ref": ref, "status": "open", "po_session": SESSION}
        )
        self.claim(runtime)
        [resolve] = self.po.resolves
        self.assertEqual(resolve["sprint_ref"], SPRINT)
        self.assertEqual(self.po.creates, [])
        self.assertEqual(self.board.wait_state().successors[self.ADDRESS]["via"], "sprint_session")
        self.assertTakenOnceBySuccessor()

    def test_a_po_service_that_cannot_open_the_successor_only_postpones(self) -> None:
        self.arrange_closed(closed=(SESSION,))
        self.po.create_session = mock.Mock(side_effect=ServiceUnavailable("not running"))
        outcome = self.claim()
        self.assertEqual((outcome["status"], outcome["action"]), ("degraded", "wait-delivery-postponed"))
        self.assertEqual(self.board.cards[WAIT]["state"], "in_progress")
        self.assertNotIn(self.ADDRESS, self.board.wait_state().deliveries)
        del self.po.create_session
        self.tick()
        self.assertTakenOnceBySuccessor()


class DependentsTests(DispatcherCase):
    def claim_pass(self, *cards: dict[str, Any]) -> tuple[dict | None, list[str]]:
        board = WaitBoard(*cards)
        claimed: list[str] = []

        def claim(runtime: Any, task: dict, *_: Any, **__: Any) -> dict:
            claimed.append(task["ref"])
            return {"status": "ok", "action": "claimed", "pilot_ref": task["ref"]}

        runtime = SimpleNamespace(
            reader=board, sprints=SimpleNamespace(show=lambda ref, **_: {"ref": ref, "status": "open"})
        )
        with mock.patch("ummanu.dispatch.production.claim_ready_task", side_effect=claim):
            outcome = _production_claim_ready(runtime, {}, {})
        return outcome, claimed

    def test_a_card_held_by_a_pending_wait_is_not_claimed(self) -> None:
        for column in ("ready", "in_progress"):
            with self.subTest(wait=column):
                outcome, claimed = self.claim_pass(
                    wait_doc(spec(), state=column), plain_doc(DEPENDENT, blocked_by=WAIT)
                )
                self.assertNotIn(DEPENDENT, claimed)
                if column == "in_progress":
                    self.assertEqual(
                        outcome["skipped_ready"],
                        [{"ref": DEPENDENT, "reason": f"blocked by pending wait {WAIT}"}],
                    )

    def test_a_card_blocked_by_an_ended_wait_or_by_another_kind_is_claimed_as_before(self) -> None:
        for blocker in (
            wait_doc(spec(), state="done"),
            plain_doc(WAIT, state="in_progress", project="relay"),
            None,
        ):
            with self.subTest(blocker=blocker and (blocker["type"], blocker["state"])):
                cards = [plain_doc(DEPENDENT, blocked_by=WAIT)] + ([blocker] if blocker else [])
                _outcome, claimed = self.claim_pass(*cards)
                self.assertEqual(claimed, [DEPENDENT])

    def test_after_target_reached_the_dependent_is_claimable_with_the_result(self) -> None:
        self.arrange(
            spec(returns=("dependents",), until="2026-09-27T12:00:00Z"), plain_doc(DEPENDENT, blocked_by=WAIT)
        )
        # A wait holds its dependents from the moment it is cut, Ready included.
        self.assertEqual(pending_wait_blockers(self.runtime(), self.board.show(DEPENDENT)), [WAIT])
        self.claim()
        self.assertEqual(self.board.cards[WAIT]["state"], "done")
        self.assertEqual(pending_wait_blockers(self.runtime(), self.board.show(DEPENDENT)), [])
        [note] = [body for body in self.board.comments(DEPENDENT) if "[wait:" in body]
        self.assertIn("This card is claimable now.", note)
        self.assertEqual(self.board.cards[DEPENDENT]["state"], "ready")


class BudgetTests(unittest.TestCase):
    def test_a_wait_outcome_is_not_charged_to_the_sprint_budget(self) -> None:
        def event(**data: Any) -> dict[str, Any]:
            taxonomy = {
                "version": 2,
                "disposition": "blocked",
                "blocked_reason": "other",
                "source_evidence": "other",
                "budget_class": "blocked",
                "provenance": "forward",
            }
            return {
                "record_type": "board_event",
                "kind": "card_blocked",
                "request_id": "dispatcher-wait-terminal-x",
                "transition": {"source": "in_progress", "target": "blocked"},
                "data": {"terminal_taxonomy": taxonomy, **data},
            }

        from ummanu.board.models import Event

        with mock.patch.object(Event, "RECORD_TYPE", "board_event"):
            self.assertEqual(_budget_event_type(event()), "blocked")
            self.assertIsNone(_budget_event_type(event(wait_outcome=DEADLINE_PASSED)))


class ViewTests(unittest.TestCase):
    def test_task_show_names_each_state_distinctly(self) -> None:
        the_spec = spec(returns=("observer", "dependents"))
        card = wait_doc(the_spec, state="in_progress")
        view = wait_view(card)
        self.assertEqual(view["state"], WAITING)
        self.assertEqual(view["target"]["link"], RUN_URL)
        self.assertEqual(
            (view["waiting_since"], view["deadline"]), ("2026-09-27T12:00:00Z", "2026-09-27T14:00:00Z")
        )
        self.assertIsNone(view["last_observation"])

        state = wait_card.WaitState(
            since="2026-09-27T12:01:00Z", observation="run x is queued", observed_at="2026-09-27T12:01:00Z"
        )
        state.result = {
            "outcome": TARGET_REACHED,
            "fact": {},
            "summary": "s",
            "evidence": RUN_URL,
            "frozen_at": "2026-09-27T12:30:00Z",
        }
        state.result["key"] = wait_card.result_key(state.result)
        card["extensions"]["extra"]["wait_state"] = state.text()
        view = wait_view(card)
        self.assertEqual((view["state"], view["waiting_since"]), (RESULT_READY, "2026-09-27T12:01:00Z"))
        self.assertEqual(view["last_observation"], {"at": "2026-09-27T12:01:00Z", "text": "run x is queued"})
        self.assertEqual(view["deliveries"], {"observer": "pending", "dependents": "pending"})

        state.deliveries["dependents"] = {"status": "accepted", "at": "t", "detail": ""}
        card["extensions"]["extra"]["wait_state"] = state.text()
        self.assertEqual(wait_view(card)["state"], RESULT_READY)
        card["state"] = "done"
        self.assertEqual((wait_view(card)["state"], wait_view(card)["delivery"]), (DELIVERED, "complete"))

        for outcome in (CANCELLED, DEADLINE_PASSED, SOURCE_UNREACHABLE):
            state.result["outcome"] = outcome
            card["extensions"]["extra"]["wait_state"] = state.text()
            self.assertEqual(wait_view(card)["state"], outcome)
        self.assertIsNone(wait_view(plain_doc("ummanu-3")))
        self.assertEqual(wait_view({**card, "extensions": {}})["state"], "malformed")


class FactsTests(unittest.TestCase):
    def test_a_wait_outcome_input_needs_no_sprint_and_no_production(self) -> None:
        facts = card_facts(
            card_ref=WAIT, kind="wait", touches_production=None, sprint_ref="", input=WAIT_OUTCOME_INPUT
        )
        self.assertEqual(facts_problem(facts), "")
        self.assertIn("wait_outcome", facts_problem({**facts, "input": "card"}))
        self.assertIn("names no production", facts_problem({**facts, "touches_production": "relay"}))
        self.assertIn("card_ref", facts_problem({**facts, "card_ref": ""}))


if __name__ == "__main__":
    unittest.main()
