"""PO delegation: a card returns its result to the PO session that cut it (secretary-1792), unit-level.

The dispatcher side runs the real `reconcile_origin_returns` over an in-memory board of cards with their
typed audit, a PO channel that deduplicates inputs by request id the way `PoService.submit` does, and
the in-memory owner-event store of `tests.owner_event_fakes`. The out-of-sprint decision card runs the
real `claim_ready_task`/`advance_po_card` against a real `PoService` over its socket
(`tests.po_card_fakes`). The create path over PostgreSQL (the stored origin, `task show`/`task list`,
the refusals, the wait's default return address) is in `tests/test_tasks.py`, and migration 0021 in
`tests/test_board_store_schema.py`.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.origin_outbox_fakes import FakeOriginOutbox, SimulatedCrash
from tests.owner_event_fakes import FakeOwnerEvents
from tests.po_card_fakes import DECISION_BODY, DispatcherFixture, Forbidden, card
from tests.po_fake_store import FakePoStore
from ummanu.board import po_origin as origin_field
from ummanu.board.po_origin import origin_text, origin_view, return_state
from ummanu.board.production_rights import (
    DELEGATED_RESULT_INPUT,
    RIGHTS_HEADING,
    card_facts,
    facts_problem,
    rights_note,
)
from ummanu.board.task_routing import TaskReview
from ummanu.cli import main
from ummanu.dispatch.origin_returns import (
    pending_returns,
    reconcile_origin_returns,
    return_request_id,
    successor_request_id,
)
from ummanu.po.client import ServiceUnavailable
from ummanu.po.store import RequestConflict, SessionClosed, SessionNotFound
from ummanu.tasks import TaskError, _po_card_create_refusal

SPRINT = "sprint:1"
SESSION = "po-session-1"
REQUEST = "web-owner-7"
REF = "ummanu-1950"
PR = "https://github.com/vladmesh/ummanu/pull/590"
RUN = "https://github.com/vladmesh/ummanu/actions/runs/4242"


def delegated(
    ref: str = REF,
    *,
    kind: str = "code",
    state: str = "ready",
    sprint: str = SPRINT,
    origin: tuple[str, str] | None = (SESSION, REQUEST),
    **extra: str,
) -> dict[str, Any]:
    """One card as `task show` reads it, carrying the PO turn it was cut in."""
    bag = {**({origin_field.PO_ORIGIN: origin_text(*origin)} if origin else {}), **extra}
    return {
        "ref": ref,
        "id": int(ref.rsplit("-", 1)[1]),
        "title": f"The {kind} work",
        "description": "Do the thing.",
        "type": kind,
        "state": state,
        "project": "ummanu",
        "sprint": sprint,
        "review": "required",
        "blocked_by": None,
        "claim": {"worker": None},
        "workspace": {"slug": None},
        "comments": [],
        "extensions": {"extra": bag},
    }


class DelegationBoard:
    """Cards with their typed audit, as the dispatcher reads them; the one `po_return` writer."""

    def __init__(self, *cards: dict[str, Any]) -> None:
        self.cards = {task["ref"]: task for task in cards}
        self.log: list[dict[str, Any]] = []
        self.returns: list[dict[str, Any]] = []
        self.outbox = FakeOriginOutbox()
        self.client = SimpleNamespace(owner_events=FakeOwnerEvents(), origin_outbox=self.outbox)
        self.audit_reads: list[str] = []

    # reader
    def show(self, reference: str) -> dict[str, Any]:
        if reference not in self.cards:
            raise TaskError("not_found", "task was not found", 2)
        return copy.deepcopy(self.cards[reference])

    def list(self, states: set[str] | None = None, **_: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(task) for task in self.cards.values() if not states or task["state"] in states]

    # writer
    def record_po_return(self, *, role: str, reference: str, state: str, **_: Any) -> None:
        assert role == "dispatcher", role
        assert origin_field.po_origin(self.cards[reference]) is not None, reference
        self.cards[reference]["extensions"]["extra"][origin_field.PO_RETURN] = state
        self.returns.append(json.loads(state))

    # audit
    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return next((event for event in self.log if event["request_id"] == request_id), None)

    def events(self, reference: str = "", **_: Any) -> list[dict[str, Any]]:
        self.audit_reads.append(reference)
        return [copy.deepcopy(event) for event in self.log if not reference or event["ref"] == reference]

    # what the other writers leave behind
    def _event(self, reference: str, kind: str, reason: str, data: dict[str, Any], **fields: Any) -> dict:
        number = len(self.log) + 1
        event = {
            "record_type": "board.protocol_event",
            "schema_version": 2,
            "event_id": f"evt_{number:04d}",
            "request_id": f"req-{number}",
            "kind": kind,
            "ref": reference,
            "occurred_at": f"2026-09-27T12:{number:02d}:00Z",
            "actor": {"role": "dispatcher", "id": "d"},
            "reason": reason,
            "data": data,
            **fields,
        }
        self.log.append(event)
        return event

    def move(self, reference: str, target: str, reason: str = "moved", **data: Any) -> str:
        source = self.cards[reference]["state"]
        event = self._event(
            reference, "card.moved", reason, data, transition={"source": source, "target": target}
        )
        self.cards[reference]["state"] = target
        # The store's commit of the transition writes the row it owes, in the same transaction.
        self.outbox.enqueue(self.cards[reference], event["request_id"], event)
        return event["event_id"]

    def archive(self, reference: str) -> None:
        """An authorized archive: the card leaves every listing; its audit and outbox rows stay."""
        self.cards[reference]["closed"] = True

    def report(self, reference: str, kind: str, body: str, classification: str | None = None) -> None:
        self._event(
            reference,
            "card.reported",
            body,
            {"marker": f"report:{kind}", "status": kind, "body": body, "classification": classification},
        )

    def delivered(self, reference: str = REF) -> dict[str, dict[str, Any]]:
        return self.outbox.delivered(reference)

    def notices(self) -> list[Any]:
        return self.client.owner_events.events()


class FakePo:
    """The PO channel as `PoService` answers it: inputs and sessions by request id, one closed or missing."""

    def __init__(self, *, down: int = 0, closed: tuple[str, ...] = (), missing: tuple[str, ...] = ()) -> None:
        self.inputs: dict[str, dict[str, Any]] = {}
        self.submits: list[str] = []
        self.down = down
        self.sessions = {
            SESSION: "open",
            "sprint-session": "open",
            **{session: "closed" for session in closed},
        }
        for session in missing:
            self.sessions.pop(session, None)
        self.opened: dict[str, str] = {}
        self.creates: list[dict[str, str]] = []

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
        assert source == "dispatcher" and not facts_problem(card), facts_problem(card)
        self.inputs[request_id] = {"session_id": session_id, "text": text, "source": source, "card": card}
        return {"session_id": session_id, "queued": True, "repeated": False}

    def create_session(self, *, cli: str, model: str, effort: str, request_id: str) -> dict[str, Any]:
        self.creates.append({"cli": cli, "model": model, "effort": effort, "request_id": request_id})
        if request_id in self.opened:
            return {"session_id": self.opened[request_id], "created": True, "repeated": True}
        session = f"successor-{len(self.opened) + 1}"
        self.sessions[session] = "open"
        self.opened[request_id] = session
        return {"session_id": session, "created": True, "repeated": False}

    def sprint_session(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("a delegated card's origin is not a sprint's session here")

    def successor_choice(self, session_id: str) -> tuple[str, str, str] | None:
        return ("codex", "gpt-6-sol", "xhigh")

    def request(self, request_id: str) -> None:
        return None

    def queued(self, request_id: str) -> Any:
        known = self.inputs.get(request_id)
        return SimpleNamespace(session_id=known["session_id"]) if known else None


class Sprints:
    """The sprint reader: sprint:1 is open and its recorded PO session is not the origin."""

    def show(self, reference: str, **_: Any) -> dict[str, Any]:
        return {"ref": reference, "status": "open", "po_session": "sprint-session", "comments": []}


class ReturnCase(unittest.TestCase):
    def arrange(self, *cards: dict[str, Any], po: FakePo | None = None) -> None:
        self.board = DelegationBoard(*cards)
        self.po = po if po is not None else FakePo()

    def runtime(self) -> SimpleNamespace:
        """A new dispatcher process: nothing in common with the last one but the board and the PO."""
        return SimpleNamespace(
            owner="ummanu-dispatcher",
            reader=self.board,
            writer=self.board,
            audit=self.board,
            po=self.po,
            sprints=Sprints(),
            host=Forbidden("host"),
            catalog=Forbidden("catalog"),
            head_health=Forbidden("head health"),
        )

    def tick(self) -> list[dict[str, Any]]:
        return reconcile_origin_returns(self.runtime())

    def done_code_card(self, ref: str = REF) -> str:
        """A code card through its round: claimed, reported done with its PR, released to Done."""
        self.board.move(ref, "in_progress", "claimed")
        self.board.report(ref, "done", f"Shipped the narrow cut.\n\nPR: {PR}\nCI: {RUN}")
        self.board.move(ref, "validate", "worker report:done")
        return self.board.move(
            ref,
            "done",
            "review green; merged",
            release_merge={"base": "main", "merge_sha": "a" * 40, "path": "pr"},
        )

    def blocked_research_card(self, ref: str = REF) -> str:
        self.board.move(ref, "in_progress", "claimed")
        self.board.report(ref, "blocked", "The vendor API needs a key only the owner holds.", "external_fact")
        return self.board.move(
            ref,
            "blocked",
            "worker report:blocked: the vendor API needs a key only the owner holds",
            terminal_taxonomy={"disposition": "blocked", "blocked_reason": "external_fact"},
        )


class DeliveryTests(ReturnCase):
    def test_a_merged_card_returns_only_after_final_done(self) -> None:
        self.arrange(delegated())
        self.board.move(REF, "in_progress", "claimed")
        self.board.report(REF, "done", f"Shipped.\n\nPR: {PR}")
        self.board.move(REF, "assessment", "review green")
        marker = {"base": "main", "merge_sha": "a" * 40, "path": "github-pr"}
        self.board.move(REF, "post_merge_ci", "merged; awaiting CI", release_merge=marker)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.po.inputs, {})
        final = self.board.move(REF, "done", "post-merge CI green", release_merge=marker)
        [outcome] = self.tick()
        self.assertEqual((outcome["action"], outcome["event_id"]), ("origin-returned", final))
        self.assertEqual(self.tick(), [])

    def test_a_done_card_returns_its_report_once_and_writes_no_owner_event(self) -> None:
        self.arrange(delegated())
        event_id = self.done_code_card()

        [outcome] = self.tick()

        request_id = return_request_id(REF, event_id)
        self.assertEqual(
            (outcome["action"], outcome["session"], outcome["po_request_id"]),
            ("origin-returned", SESSION, request_id),
        )
        self.assertEqual(list(self.po.inputs), [request_id])
        sent = self.po.inputs[request_id]
        self.assertEqual(sent["session_id"], SESSION)
        self.assertEqual(
            sent["card"],
            card_facts(
                card_ref=REF,
                kind="code",
                touches_production=None,
                sprint_ref=SPRINT,
                input=DELEGATED_RESULT_INPUT,
            ),
        )
        text = sent["text"]
        for fragment in (
            f"Card {REF} (code), which PO session {SESSION} cut (answering request {REQUEST}), settled Done.",
            f"Card: {REF} (code): The code work",
            "## The worker's done report",
            "Shipped the narrow cut.",
            "## Links",
            f"- {PR}",
            f"- {RUN}",
            f"- merged {'a' * 40} onto main",
        ):
            self.assertIn(fragment, text)
        self.assertEqual(self.board.notices(), [])
        self.assertEqual(pending_returns(self.board.outbox), [])
        delivered = self.board.delivered()[event_id]
        self.assertEqual(
            {name: delivered[name] for name in ("state", "status", "session", "request_id")},
            {
                "state": "done",
                "status": "delivered",
                "session": SESSION,
                "request_id": request_id,
            },
        )
        self.assertIsNotNone(delivered["delivered_at"])

        # The next ticks send nothing: that transition is delivered.
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.po.submits), 1)

    def test_a_card_reopened_and_done_again_returns_its_new_result_once_more(self) -> None:
        """Every entry into Done is its own delivery, keyed by its event (BLOCKER-DONE-REDISPATCH)."""
        self.arrange(delegated())
        first = self.done_code_card()
        self.tick()

        self.board.move(REF, "ready", "reopened by the owner: the cut was too narrow")
        second = self.done_code_card()
        [outcome] = self.tick()

        self.assertEqual((outcome["action"], outcome["event_id"]), ("origin-returned", second))
        self.assertEqual(
            list(self.po.inputs), [return_request_id(REF, first), return_request_id(REF, second)]
        )
        self.assertEqual(set(self.board.delivered()), {first, second})
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.po.submits), 2)

    def test_a_blocked_card_carries_its_reason_and_classification(self) -> None:
        self.arrange(delegated(kind="research"))
        event_id = self.blocked_research_card()

        self.tick()

        [sent] = self.po.inputs.values()
        text = sent["text"]
        self.assertIn(f"Card {REF} (research), which PO session {SESSION} cut", text)
        self.assertIn("settled Blocked.", text)
        self.assertIn("## Why it is Blocked\n\nworker report:blocked: the vendor API needs a key", text)
        self.assertIn("Classification: external_fact", text)
        self.assertIn("## The worker's blocked report", text)
        self.assertEqual(self.board.delivered()[event_id]["state"], "blocked")
        # A Blocked card is read each tick (it may be Blocked again), and sends nothing new.
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.po.submits), 1)

    def test_a_dispatcher_block_with_no_report_is_classified_by_its_taxonomy(self) -> None:
        self.arrange(delegated(kind="infra"))
        self.board.move(REF, "in_progress", "claimed")
        self.board.move(
            REF,
            "blocked",
            "wait watchdog: stalled after respawn",
            terminal_taxonomy={"disposition": "blocked", "blocked_reason": "operator"},
        )

        self.tick()

        [sent] = self.po.inputs.values()
        self.assertIn("Classification: board: operator", sent["text"])
        self.assertNotIn("## Links", sent["text"])

    def test_a_crash_between_the_submit_and_the_record_sends_no_second_input(self) -> None:
        for column in ("done", "blocked"):
            with self.subTest(column=column):
                self.arrange(delegated(kind="code" if column == "done" else "research"))
                event_id = self.done_code_card() if column == "done" else self.blocked_research_card()
                self.board.outbox.crash_before = lambda row: True

                with self.assertRaises(SimulatedCrash):
                    self.tick()
                self.assertEqual(len(self.po.inputs), 1)
                self.assertEqual(self.board.delivered(), {})

                [outcome] = self.tick()

                self.assertEqual(outcome["action"], "origin-returned")
                # The submit was repeated under the same id and the service took it as the earlier one.
                self.assertEqual(self.po.submits, [return_request_id(REF, event_id)] * 2)
                self.assertEqual(len(self.po.inputs), 1)
                self.assertEqual(self.board.delivered()[event_id]["status"], "delivered")
                self.assertEqual(self.tick(), [])

    def test_a_closed_origin_session_gets_one_successor_that_takes_every_later_result(self) -> None:
        self.arrange(delegated(kind="research"), po=FakePo(closed=(SESSION,)))
        first = self.blocked_research_card()

        [outcome] = self.tick()

        self.assertEqual(outcome["session"], "successor-1")
        self.assertEqual(
            self.po.creates,
            [
                {
                    "cli": "codex",
                    "model": "gpt-6-sol",
                    "effort": "xhigh",
                    "request_id": successor_request_id(REF, SESSION),
                }
            ],
        )
        [sent] = self.po.inputs.values()
        self.assertEqual(sent["session_id"], "successor-1")
        state = return_state(self.board.cards[REF])
        self.assertEqual(
            state.successors,
            {SESSION: {"replaces": SESSION, "via": "create_session", "session": "successor-1"}},
        )
        self.assertEqual(self.board.delivered()[first]["session"], "successor-1")
        self.assertEqual(origin_view(self.board.cards[REF])["current_session"], "successor-1")

        # The card is Blocked again later: that result goes straight to the successor.
        self.board.move(REF, "ready", "retry")
        self.board.move(REF, "in_progress", "claimed")
        second = self.board.move(REF, "blocked", "the second round stalled")
        self.tick()

        self.assertEqual(len(self.po.creates), 1)
        self.assertEqual(
            [(entry["session_id"], request) for request, entry in self.po.inputs.items()],
            [("successor-1", return_request_id(REF, first)), ("successor-1", return_request_id(REF, second))],
        )

    def test_a_missing_origin_session_gets_a_successor_too(self) -> None:
        self.arrange(delegated(), po=FakePo(missing=(SESSION,)))
        self.done_code_card()
        [outcome] = self.tick()
        self.assertEqual(outcome["session"], "successor-1")

    def test_a_service_that_is_down_postpones_the_return_and_the_next_tick_makes_it_once(self) -> None:
        self.arrange(delegated(), po=FakePo(down=1))
        event_id = self.done_code_card()

        [postponed] = self.tick()

        self.assertEqual((postponed["status"], postponed["action"]), ("degraded", "origin-return-postponed"))
        self.assertIn("not running", postponed["reason"])
        self.assertEqual((self.po.inputs, self.board.notices(), self.board.delivered()), ({}, [], {}))

        [outcome] = self.tick()

        self.assertEqual(outcome["action"], "origin-returned")
        self.assertEqual(list(self.po.inputs), [return_request_id(REF, event_id)])

    def test_a_card_with_no_origin_is_not_returned(self) -> None:
        self.arrange(delegated(origin=None))
        self.done_code_card()
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.po.submits, self.board.notices()), ([], []))

    def test_the_same_transition_always_renders_the_same_input(self) -> None:
        self.arrange(delegated())
        self.done_code_card()
        self.board.outbox.crash_before = lambda row: True
        with self.assertRaises(SimulatedCrash):
            self.tick()
        # A comment added in between changes nothing: the input is the card and its audit up to Done.
        self.board.cards[REF]["comments"].append({"marker": "po", "body": "[po]\nLater thoughts."})
        self.tick()
        self.assertEqual(len(self.po.inputs), 1)


class BacklogTests(ReturnCase):
    """Every undelivered transition into Done or Blocked is returned, oldest first, from `pending_returns`."""

    def test_a_done_reopened_and_done_again_before_any_tick_returns_both_in_order(self) -> None:
        """BLOCKER-TERMINAL-BACKLOG-LOSS: the reviewer's reproduction."""
        self.arrange(delegated())
        first = self.done_code_card()
        self.board.move(REF, "ready", "reopened before the dispatcher saw the Done")
        second = self.done_code_card()

        self.assertEqual([row.event_id for row in pending_returns(self.board.outbox)], [first, second])
        outcomes = self.tick()

        self.assertEqual(
            [(o["action"], o["event_id"]) for o in outcomes],
            [("origin-returned", first), ("origin-returned", second)],
        )
        self.assertEqual(
            list(self.po.inputs), [return_request_id(REF, first), return_request_id(REF, second)]
        )
        self.assertEqual(
            {key: record["state"] for key, record in self.board.delivered().items()},
            {first: "done", second: "done"},
        )
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.po.submits), 2)

    def test_a_blocked_card_moved_to_ready_before_any_tick_returns_its_blocked(self) -> None:
        self.arrange(delegated(kind="research"))
        event_id = self.blocked_research_card()
        self.board.move(REF, "ready", "the owner will add the key; retry")

        [outcome] = self.tick()

        self.assertEqual(self.board.cards[REF]["state"], "ready")
        self.assertEqual(
            (outcome["action"], outcome["state"], outcome["event_id"]),
            ("origin-returned", "blocked", event_id),
        )
        [sent] = self.po.inputs.values()
        self.assertIn("settled Blocked.", sent["text"])
        self.assertIn("State: Blocked, at 2026-09-27T12:03:00Z", sent["text"])
        self.assertIn(
            "## Why it is Blocked\n\nworker report:blocked: the vendor API needs a key", sent["text"]
        )
        self.assertIn("Classification: external_fact", sent["text"])

    def arrange_backlog(self) -> tuple[str, str]:
        """Done at A, reopened, Blocked at B: two returns owed before any tick."""
        self.arrange(delegated())
        first = self.done_code_card()
        self.board.move(REF, "ready", "reopened")
        self.board.move(REF, "in_progress", "claimed")
        second = self.board.move(REF, "blocked", "the second round stalled")
        return first, second

    def test_a_crash_on_the_first_leaves_both_for_the_next_tick_with_no_duplicate(self) -> None:
        first, second = self.arrange_backlog()
        self.board.outbox.crash_before = lambda row: True

        with self.assertRaises(SimulatedCrash):
            self.tick()
        self.assertEqual(list(self.po.inputs), [return_request_id(REF, first)])

        outcomes = self.tick()

        self.assertEqual([o["event_id"] for o in outcomes], [first, second])
        self.assertEqual(
            list(self.po.inputs), [return_request_id(REF, first), return_request_id(REF, second)]
        )
        self.assertEqual(self.tick(), [])

    def test_a_card_archived_before_any_pass_still_returns_its_done(self) -> None:
        """BLOCKER-ARCHIVED-ORIGIN-RETURN-LOSS: the obligation was written with the move, not inferred later."""
        self.arrange(delegated())
        event_id = self.done_code_card()
        self.board.archive(REF)

        [outcome] = self.tick()

        self.assertEqual((outcome["action"], outcome["event_id"]), ("origin-returned", event_id))
        self.assertEqual(len(self.po.inputs), 1)
        self.assertEqual(self.tick(), [])

    def test_a_delivered_row_is_never_selected_again_and_the_pass_reads_no_card_list(self) -> None:
        self.arrange(delegated(), delegated("ummanu-1951", origin=None))
        event_id = self.done_code_card()
        self.board.list = mock.Mock(side_effect=AssertionError("the pass scans no card listing"))

        self.tick()
        self.board.audit_reads.clear()

        self.assertEqual(self.tick(), [])
        self.assertEqual(self.board.audit_reads, [])
        self.assertEqual([row.event_id for row in self.board.outbox.rows if row.delivered_at], [event_id])
        self.assertEqual(pending_returns(self.board.outbox), [])

    def test_the_store_owes_nothing_for_a_card_with_no_origin_a_wait_card_or_a_move_out(self) -> None:
        self.arrange(
            delegated(), delegated("ummanu-1951", origin=None), delegated("ummanu-1952", kind="wait")
        )
        for ref in ("ummanu-1951", "ummanu-1952"):
            self.board.move(ref, "in_progress", "claimed")
            self.board.move(ref, "done", "done")
        self.board.move(REF, "in_progress", "claimed")
        self.board.move(REF, "validate", "report:done")
        self.assertEqual(self.board.outbox.rows, [])


class ExceptionTests(ReturnCase):
    """The two exceptions: a wait card, and a decision/operation card its origin session completed."""

    def test_a_wait_card_delivers_only_through_its_own_return_addresses(self) -> None:
        self.arrange(delegated(kind="wait", sprint=""))
        self.board.move(REF, "in_progress", "claimed")
        self.board.move(REF, "done", "[wait:target_reached]", wait_outcome="target_reached")
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.po.submits, self.board.notices()), ([], []))

    def complete(self, **data: str) -> str:
        """The decision card completed with `task complete`; `data` is its transition data."""
        self.board.move(REF, "in_progress", "claimed")
        return self.board.move(REF, "done", "[completion:decision]\n\n## Decision\nShip it.", **data)

    def test_a_done_completed_in_a_turn_of_the_origin_line_is_not_sent_back(self) -> None:
        """BLOCKER-ORIGIN-EXECUTOR-PROOF: the completion's own `po_session` is the proof."""
        successor = {"replaces": SESSION, "via": "create_session", "session": "successor-1"}
        for completer in (SESSION, "successor-1"):
            with self.subTest(completer=completer):
                state = origin_field.ReturnState(executor=completer, successors={SESSION: dict(successor)})
                self.arrange(delegated(kind="decision", sprint="", po_return=state.text()))
                event_id = self.complete(po_session=completer)

                [outcome] = self.tick()

                self.assertEqual(
                    (outcome["action"], outcome["session"]), ("origin-return-skipped", completer)
                )
                self.assertEqual((self.po.submits, self.board.notices()), ([], []))
                self.assertEqual(self.board.delivered()[event_id]["status"], "skipped")
                self.assertEqual(self.tick(), [])

    def test_a_done_completed_anywhere_else_is_sent_back(self) -> None:
        """The session the dispatcher handed it to proves nothing: only the completion's session does."""
        for label, data in (
            ("another session", {"po_session": "someone-else"}),
            ("outside a PO turn", {}),
            ("a successor the card never recorded", {"po_session": "successor-9"}),
        ):
            with self.subTest(completed_by=label):
                # The dispatcher handed it to the origin session; that is not who completed it.
                state = origin_field.ReturnState(executor=SESSION)
                self.arrange(delegated(kind="decision", sprint="", po_return=state.text()))
                event_id = self.complete(**data)

                [outcome] = self.tick()

                self.assertEqual((outcome["action"], outcome["session"]), ("origin-returned", SESSION))
                self.assertEqual(list(self.po.inputs), [return_request_id(REF, event_id)])
                self.assertEqual(self.board.delivered()[event_id]["status"], "delivered")

    def test_its_blocked_is_sent_back_and_a_done_in_another_session_is_too(self) -> None:
        state = origin_field.ReturnState(executor=SESSION)
        self.arrange(delegated(kind="operation", sprint="", po_return=state.text()))
        self.board.move(REF, "in_progress", "claimed")
        self.board.move(
            REF,
            "blocked",
            f"PO turn {SESSION}/2 ended completed without completing the card",
            terminal_taxonomy={"disposition": "blocked", "blocked_reason": "other"},
        )
        [outcome] = self.tick()
        self.assertEqual(outcome["action"], "origin-returned")
        [sent] = self.po.inputs.values()
        self.assertIn("ended completed without completing the card", sent["text"])

        # The same kind of card in a sprint, executed by that sprint's session: its Done goes back.
        other = origin_field.ReturnState(executor="sprint-session")
        self.arrange(delegated(kind="decision", po_return=other.text()))
        self.board.move(REF, "in_progress", "claimed")
        self.board.move(
            REF, "done", "[completion:decision]\n\n## Decision\nShip it.\n\n## How to verify\n`x`"
        )
        [outcome] = self.tick()
        self.assertEqual(outcome["action"], "origin-returned")
        [sent] = self.po.inputs.values()
        self.assertIn("## Completion record\n\n[completion:decision]", sent["text"])


class OutOfSprintCardTests(DispatcherFixture):
    """An out-of-sprint decision card goes to the PO session that cut it, through the real PO service."""

    def out_of_sprint(self, session: str) -> dict[str, Any]:
        task = card("decision")
        task["sprint"] = ""
        task["extensions"] = {"extra": {origin_field.PO_ORIGIN: origin_text(session, "owner-msg-1")}}
        return task

    def test_it_is_claimed_submitted_to_its_origin_session_and_completed_there(self) -> None:
        service = self.start()
        session = self.session(service)
        runtime = self.runtime(self.out_of_sprint(session), comments=[])

        outcome = self.claim(runtime)

        self.assertEqual(
            (outcome["action"], outcome["po_session"], outcome["po_session_outcome"]),
            ("po-card-submitted", session, "origin"),
        )
        # No sprint was resolved and no session opened: the origin session took it.
        self.assertEqual(self.session_ids(), [session])
        self.assertEqual(self.record().po_submission.card["sprint_ref"], "")
        self.assertEqual(return_state(self.cards.card).executor, session)
        self.settled(session, 1)
        [call] = [call for call in self.calls() if "ummanu-1900" in call["prompt"]]
        self.assertIn(
            "(outside every sprint; you cut it in this session, so this session executes it)", call["prompt"]
        )
        self.assertNotIn("## Comments of", call["prompt"])
        self.assertIn("task complete --ref ummanu-1900 --role po --kind decision", call["prompt"])

        self.cards.complete_as_po("decision", DECISION_BODY)
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")
        # Its Done, in the audit, is not sent back: the session that completed it has the result.
        done = {
            "record_type": "board.protocol_event",
            "event_id": "evt_done",
            "request_id": "complete-1",
            "ref": "ummanu-1900",
            "kind": "card.moved",
            "reason": "[completion:decision]",
            "transition": {"source": "in_progress", "target": "done"},
            # What `task complete` records when the origin session's turn runs it.
            "data": {"po_session": session},
            "occurred_at": "2026-09-27T13:00:00Z",
        }
        # The completion's commit, as the store makes it: the audit event and the row it owes.
        self.cards.log.append(done)
        outbox = FakeOriginOutbox()
        self.assertTrue(outbox.enqueue(self.cards.card, "complete-1", done))
        runtime.reader.client = SimpleNamespace(owner_events=FakeOwnerEvents(), origin_outbox=outbox)
        [returned] = reconcile_origin_returns(runtime)
        self.assertEqual(returned["action"], "origin-return-skipped")
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 1)
        self.assertEqual(runtime.reader.client.owner_events.events(), [])

    def test_a_closed_origin_session_gets_a_successor_that_executes_it(self) -> None:
        service = self.start()
        session = self.session(service)
        FakePoStore(self.board).close_session(session, "owner")
        runtime = self.runtime(self.out_of_sprint(session), comments=[])

        outcome = self.claim(runtime)

        successor = outcome["po_session"]
        self.assertEqual(outcome["action"], "po-card-submitted")
        self.assertNotEqual(successor, session)
        self.assertEqual(sorted(self.session_ids()), sorted([session, successor]))
        state = return_state(self.cards.card)
        self.assertEqual(state.executor, successor)
        self.assertEqual(
            state.successors[session],
            {"replaces": session, "via": "create_session", "session": successor},
        )
        self.assertEqual(self.board.sessions[successor].cli, "claude")
        self.settled(successor, 1)

    def test_one_with_no_origin_is_still_blocked_at_claim(self) -> None:
        task = card("decision")
        task["sprint"] = ""
        runtime = self.runtime(task)
        outcome = self.claim(runtime)
        self.assertEqual(outcome["status"], "blocked")
        self.assertIn("names no sprint and no PO session it came from", outcome["reason"])


class CreateRuleTests(unittest.TestCase):
    def test_an_out_of_sprint_decision_or_operation_needs_a_po_turn(self) -> None:
        common: dict[str, Any] = {
            "head": "",
            "review_head": "",
            "review": TaskReview.SKIPPED,
            "live_impact": False,
            "seed_ref": "",
            "base_branch": "",
        }
        for kind in ("decision", "operation"):
            with self.subTest(kind=kind):
                self.assertIn("needs --sprint", _po_card_create_refusal(kind, role="po", sprint="", **common))
                self.assertEqual(
                    _po_card_create_refusal(kind, role="po", sprint="", origin=True, **common), ""
                )
                self.assertEqual(_po_card_create_refusal(kind, role="po", sprint=SPRINT, **common), "")

    def test_the_cli_reads_the_origin_from_a_po_turn_for_the_po_only(self) -> None:
        turn = {"UMMANU_PO_SESSION": "s-7", "UMMANU_PO_REQUEST": "web-42"}
        for role, environment, expected in (
            ("po", turn, {"session": "s-7", "request": "web-42"}),
            ("po", {"UMMANU_PO_SESSION": "s-7"}, {"session": "s-7", "request": ""}),
            ("po", {}, None),
            ("observer", turn, None),
        ):
            with self.subTest(role=role, environment=environment):
                writer = mock.Mock()
                writer.return_value.create.return_value = {"action": "created"}
                clean = {k: v for k, v in os.environ.items() if not k.startswith("UMMANU_PO_")}
                with (
                    mock.patch.dict(os.environ, {**clean, **environment}, clear=True),
                    mock.patch("ummanu.tasks.TaskWriter", writer),
                    mock.patch("ummanu.task_commands.card_client"),
                    contextlib.redirect_stdout(io.StringIO()),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    (Path(tmp) / "projects").mkdir()
                    (Path(tmp) / "projects" / "ummanu.yaml").write_text("id: ummanu\nenabled: true\n", encoding="utf-8")
                    code = main(
                        [
                            "task",
                            "create",
                            "--role",
                            role,
                            "--instance",
                            tmp,
                            "--data-dir",
                            tmp,
                            "--project",
                            "ummanu",
                            "--type",
                            "research",
                            "--title",
                            "T",
                            "--sprint",
                            SPRINT,
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(writer.return_value.create.call_args.kwargs["origin"], expected)

    def test_complete_and_handover_record_the_session_of_the_turn_that_runs_them(self) -> None:
        for verb, extra in (
            ("complete", ["--kind", "decision", "--body-file", "BODY"]),
            ("handover", ["--to", "owner", "--reason", "Pay the relay."]),
        ):
            for environment, expected in (({"UMMANU_PO_SESSION": "s-7"}, "s-7"), ({}, "")):
                with self.subTest(verb=verb, environment=environment):
                    writer = mock.Mock()
                    getattr(writer.return_value, verb).return_value = {"action": verb}
                    clean = {k: v for k, v in os.environ.items() if not k.startswith("UMMANU_PO_")}
                    with (
                        mock.patch.dict(os.environ, {**clean, **environment}, clear=True),
                        mock.patch("ummanu.tasks.TaskWriter", writer),
                        mock.patch("ummanu.task_commands.card_client"),
                        contextlib.redirect_stdout(io.StringIO()),
                        tempfile.TemporaryDirectory() as tmp,
                    ):
                        body = os.path.join(tmp, "body.md")
                        with open(body, "w", encoding="utf-8") as handle:
                            handle.write(DECISION_BODY)
                        argv = [
                            "task",
                            verb,
                            "--ref",
                            REF,
                            "--role",
                            "po",
                            "--instance",
                            tmp,
                            "--data-dir",
                            tmp,
                        ]
                        code = main(argv + [body if part == "BODY" else part for part in extra])
                    self.assertEqual(code, 0)
                    self.assertEqual(
                        getattr(writer.return_value, verb).call_args.kwargs["po_session"], expected
                    )

    def test_there_is_no_flag_to_name_an_origin(self) -> None:
        for flag in ("--origin", "--po-session", "--po-request"):
            with (
                self.subTest(flag=flag),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch("ummanu.tasks.TaskWriter") as writer,
            ):
                code = main(
                    [
                        "task",
                        "create",
                        "--role",
                        "po",
                        "--project",
                        "ummanu",
                        "--type",
                        "research",
                        "--title",
                        "T",
                        flag,
                        "s-1",
                    ]
                )
            self.assertEqual(code, 2)
            writer.assert_not_called()


class FactsTests(unittest.TestCase):
    def test_a_delegated_result_carries_any_kind_and_no_production(self) -> None:
        for kind in ("code", "research", "infra", "decision", "operation"):
            for sprint in (SPRINT, ""):
                facts = card_facts(
                    card_ref=REF,
                    kind=kind,
                    touches_production="relay",
                    sprint_ref=sprint,
                    input=DELEGATED_RESULT_INPUT,
                )
                facts["touches_production"] = None
                self.assertEqual(facts_problem(facts), "", (kind, sprint))
        good = card_facts(
            card_ref=REF, kind="code", touches_production=None, sprint_ref="", input=DELEGATED_RESULT_INPUT
        )
        for broken, problem in (
            ({**good, "card_ref": ""}, "no card_ref"),
            ({**good, "kind": ""}, "no kind"),
            ({**good, "sprint_ref": None}, "no sprint_ref"),
            ({**good, "touches_production": "relay"}, "names no production"),
        ):
            with self.subTest(broken=broken):
                self.assertIn(problem, facts_problem(broken))

    def test_an_out_of_sprint_operation_has_no_allowance_to_read(self) -> None:
        facts = card_facts(card_ref=REF, kind="operation", touches_production="relay", sprint_ref="")
        self.assertEqual(facts_problem(facts), "")
        note = rights_note("relay", "", None, request_id="r:allow-production")
        self.assertTrue(note.startswith(RIGHTS_HEADING))
        self.assertIn("no sprint allowance to read or to record", note)
        self.assertIn("task handover --to owner", note)
        self.assertNotIn("sprint allow-production", note)
        self.assertIn("nothing to allow", rights_note("none", "", None, request_id="r"))
        self.assertIn("the sprint allows it", rights_note("none", SPRINT, None, request_id="r"))


if __name__ == "__main__":
    unittest.main()
