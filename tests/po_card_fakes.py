"""The dispatcher side of a decision/operation card in memory: one card, one sprint, a real PO service.

Shared by `tests/test_po_cards.py` (card 3, secretary-1758) and `tests/test_po_handover.py` (card 4,
secretary-1761), so neither test module imports the other.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import signal
import stat
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.po_cli_fakes import FAKE_CLAUDE, eventually, unscoped_test_launch
from tests.po_fake_store import FakeBoard, FakePoStore, FakeSprints
from ummanu.board.completion_evidence import po_completion_fields, render_po_completion_record
from ummanu.board.owner_handover import (
    HANDED_TO_OWNER,
    MARK_KEYS,
    mark_values,
    render_handover_comment,
    waiting_owner,
)
from ummanu.dispatch.claim import claim_ready_task
from ummanu.dispatch.po_cards import ServicePoChannel, advance_po_card
from ummanu.dispatch.state import DispatcherRecord, new_attempt_id
from ummanu.po import store as po_store
from ummanu.po.runner import PoRunner
from ummanu.po.service import PoService, listening

REF = "ummanu-1900"
SPRINT = "sprint:1"
DECISION_BODY = "## Decision\nShip the narrow cut.\n\n## How to verify\n`ummanu sprint show --ref sprint:1`\n"
OPERATION_BODY = "## What was done\nRotated the key.\n\n## How to verify\n`ssh relay true` exits 0\n"


class Forbidden:
    """A collaborator a decision/operation card must never reach: no head, no workspace, no registry."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        raise AssertionError(f"a PO-executed card reached the {self._name}: {attribute}")


class OneCardBoard:
    """One card as the dispatcher reads and writes it, and the audit its writes leave behind."""

    def __init__(self, card: dict[str, Any]) -> None:
        self.card = card
        self.log: list[dict[str, Any]] = []

    # reader
    def show(self, reference: str) -> dict[str, Any]:
        assert reference == self.card["ref"], reference
        return copy.deepcopy(self.card)

    # writer
    def claim(self, *, role: str, reference: str, worker: str, request_id: str, **_: Any) -> dict[str, Any]:
        if self.committed_event(request_id) is None:
            assert self.card["state"] == "ready", "claim requires a Ready task"
            self.card["state"] = "in_progress"
            self.card["claim"] = {"worker": worker}
            self.log.append({"request_id": request_id, "ref": reference, "kind": "claimed", "role": role, "event_id": f"evt-{request_id}", "occurred_at": datetime.now(UTC).isoformat()})
        return {"action": "claimed"}

    def move(
        self, *, role: str, reference: str, target: str, reason: str, request_id: str, **fields: Any
    ) -> dict[str, Any]:
        if self.committed_event(request_id) is None:
            self.card["state"] = target
            self.card.get("extensions", {}).get("extra", {}).pop("owner_escalation", None)
            self.log.append(
                {"request_id": request_id, "ref": reference, "kind": "move", "role": role, "to": target,
                 "reason": reason, **fields}
            )
        return {"action": "moved"}

    def escalate_po_card(self, *, reference: str, episode: str, reason: str, **_: Any) -> None:
        from ummanu.board.owner_handover import OWNER_ESCALATION
        assert reference == self.card["ref"]
        bag = self.card.setdefault("extensions", {}).setdefault("extra", {})
        bag[OWNER_ESCALATION] = json.dumps({"episode": episode, "reason": reason})

    def list(self, states: set[str] | None = None, **_: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(self.card)] if not states or self.card["state"] in states else []

    def record_po_return(self, *, role: str, reference: str, state: str, **_: Any) -> None:
        """The dispatcher's `po_return` of a delegated card (secretary-1792)."""
        assert role == "dispatcher" and reference == self.card["ref"], (role, reference)
        self.card.setdefault("extensions", {}).setdefault("extra", {})["po_return"] = state

    def record_po_execution(self, *, role: str, reference: str, state: str, **_: Any) -> None:
        assert role == "dispatcher" and reference == self.card["ref"]
        self.card.setdefault("extensions", {}).setdefault("extra", {})["po_execution"] = state

    # audit
    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return next((event for event in self.log if event["request_id"] == request_id), None)

    def events(self, reference: str = "", **_: Any) -> list[dict[str, Any]]:
        return [event for event in self.log if not reference or event["ref"] == reference]

    # the PO, handing the card to the owner inside its turn (what `task handover --role po` leaves)
    def hand_over_as_po(self, reason: str, *, request_id: str) -> None:
        assert self.card["state"] == "in_progress" and waiting_owner(self.card) is None
        since = f"2026-09-26T15:00:{len(self.log):02d}+00:00"
        self.card.setdefault("extensions", {}).setdefault("extra", {}).update(mark_values(since, reason, "po"))
        self.card["comments"].append(
            {"created_at": since, "marker": "po", "body": "[po]\n" + render_handover_comment(reason)}
        )
        self.log.append(
            {"request_id": request_id, "ref": self.card["ref"], "kind": HANDED_TO_OWNER,
             "event_id": f"evt-{request_id}", "role": "po", "actor": "po", "reason": reason}
        )

    # the PO, completing the card inside its turn
    def complete_as_po(self, kind: str, body: str) -> None:
        fields, refusal = po_completion_fields(kind, body)
        assert not refusal, refusal
        self.card["comments"].append({"marker": "po", "body": "[po]\n" + render_po_completion_record(kind, fields)})
        self.card["state"] = "done"
        # Leaving In progress takes the mark off, in the same transaction.
        for key in MARK_KEYS:
            self.card.get("extensions", {}).get("extra", {}).pop(key, None)


class SprintView:
    """The dispatcher's sprint reader: the one sprint, with its comments in board order."""

    def __init__(self, comments: list[str]) -> None:
        self.comments = comments

    def show(self, reference: str, **_: Any) -> dict[str, Any]:
        assert reference == SPRINT, reference
        return {
            "ref": SPRINT,
            "status": "open",
            "comments": [
                {"created_at": f"2026-09-26T10:0{index}:00Z", "body": body}
                for index, body in enumerate(self.comments)
            ],
        }


def card(
    kind: str = "decision",
    *,
    state: str = "ready",
    description: str = "Which cut ships first?",
    production: str | None = "none",
) -> dict:
    """One card as `task show` reads it; an operation card names its production (`none` by default)."""
    extensions = (
        {"extensions": {"extra": {"touches_production": production}}}
        if kind == "operation" and production is not None
        else {}
    )
    return {
        **extensions,
        "ref": REF,
        "id": 1900,
        "title": f"The {kind} to take",
        "description": description,
        "type": kind,
        "state": state,
        "project": "ummanu",
        "sprint": SPRINT,
        "review": "skipped",
        "claim": {"worker": None},
        "workspace": {"slug": None},
        "comments": [],
    }


class DispatcherFixture(unittest.TestCase):
    """A real PO service on its socket, and a dispatcher runtime around one card and one sprint.

    The service is the one `tests/test_po_service.py` drives: the fake `claude` of `tests.po_cli_fakes`
    runs every turn as a real process, and the board store is the in-memory `tests.po_fake_store`.
    """

    def setUp(self) -> None:
        # A short root: the service's Unix socket lives under it.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="po-")))
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        claude = self.root / "bin" / "claude"
        claude.parent.mkdir()
        claude.write_text(FAKE_CLAUDE, encoding="utf-8")
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
        self.claude = str(claude)
        self.log = self.root / "fake.log"
        self.gate = self.log.with_name(self.log.name + ".gate")
        self.board = FakeBoard()
        self.po_sprints = FakeSprints({SPRINT: None})
        self.services: list[PoService] = []
        self.addCleanup(self.stop_everything)

    def start(self, *, listen: bool = True) -> PoService:
        runner = PoRunner(
            FakePoStore(self.board),
            self.data,
            executables={"claude": self.claude},
            env={**os.environ, "FAKE_LOG": str(self.log)},
            turn_launcher=unscoped_test_launch,
        )
        service = PoService(runner, data_dir=self.data, sprints=self.po_sprints, models={"claude": ("opus",)})
        self.services.append(service)
        service.start()
        thread = threading.Thread(target=service.run, kwargs={"tick": 0.05, "say": lambda _line: None})
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(service.stop)
        if listen:
            self.enterContext(listening(service))
        return service

    def stop_everything(self) -> None:
        self.gate.touch()
        for service in self.services:
            service.stop()
            for live in list(service.runner._live.values()):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(live.process.pid, signal.SIGKILL)

    def session(self, service: PoService) -> str:
        return service.create_session(cli="claude", model="opus", effort="high", request_id="c-owner")[
            "session_id"
        ]

    def settled(self, session_id: str, seq: int) -> po_store.Turn:
        store = FakePoStore(self.board)
        eventually(
            lambda: len(store.turns(session_id)) >= seq and store.turns(session_id)[seq - 1].state != po_store.RUNNING,
            f"turn {seq} never settled",
        )
        return store.turns(session_id)[seq - 1]

    def reached_gate(self, session_id: str, seq: int) -> None:
        stdout = self.data / "po-runs" / session_id / f"turn-{seq:04d}.stdout"
        eventually(
            lambda: stdout.exists() and "TOOL-CALL-SECRET" in stdout.read_text(),
            f"turn {seq} never reached its gate",
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def runtime(self, task: dict[str, Any], *, comments: list[str] | None = None, po: Any = None):
        self.cards = OneCardBoard(task)
        channel = ServicePoChannel(self.data, None)
        channel._store = FakePoStore(self.board)
        self.saved: list[dict[str, Any]] = []
        return SimpleNamespace(
            owner="ummanu-dispatcher",
            reader=self.cards,
            writer=self.cards,
            audit=self.cards,
            sprints=SprintView(comments if comments is not None else ["Opened the sprint.", "Owner: keep it small."]),
            po=po or channel,
            host=Forbidden("host"),
            catalog=Forbidden("catalog"),
            head_health=Forbidden("head health"),
            save_records=lambda payload, records: self.saved.append(
                {ref: record.to_json() for ref, record in records.items()}
            ),
        )

    def claim(self, runtime: Any, records: dict[str, DispatcherRecord] | None = None):
        self.records = {} if records is None else records
        self.payload: dict[str, Any] = {}
        self.attempt = new_attempt_id()
        return claim_ready_task(runtime, runtime.reader.show(REF), self.records, self.payload, self.attempt)

    def tick(self, runtime: Any) -> dict[str, Any]:
        return advance_po_card(runtime, runtime.reader.show(REF), self.records, self.payload, self.attempt)

    def record(self) -> DispatcherRecord:
        return self.records[REF]

    def session_ids(self) -> list[str]:
        return list(self.board.sessions)
