"""Delegation, waits and what a sprint waits for, on the pages (secretary-1811, sprint:1469 DoD 11).

The layers are recording fakes (`tests/web_fakes.py`) answering with the documents the read layers
produce, so each rule is driven through the real route and the real page: the card page's
delegation, wait and e2e blocks; the PO session page and its JSON with the cards it delegated; the
sprint page's `waiting_on`. `card_waits`, the derivation behind `work.waiting_on`, and the PO title
lookup are driven directly. The last suite is the hostile-value table: every new read over missing,
partial and legacy values, none of which may raise or answer 500.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

from tests.po_fake_store import FakePoStore
from tests.web_fakes import Recording
from ummanu.board.e2e_record import e2e_view
from ummanu.po import token as po_token
from ummanu.web import pages
from ummanu.web.app import WebApp
from ummanu.webproto.errors import RuntimeUnavailable
from ummanu.webproto.po_auth import PoTokenLayer
from ummanu.webproto.po_ops import PoLayer
from ummanu.webproto.sprint_reads import WAITING_ON_KINDS, card_waits

SESSION = "3f0a6c2e-1111-4b4b-9c9c-000000000001"
SUCCESSOR = "9b7d1e44-2222-4b4b-9c9c-000000000002"
GONE = "deadbeef-3333-4b4b-9c9c-000000000003"
RUN_URL = "https://github.com/vladmesh/ummanu/actions/runs/4242"
AVAILABLE = {
    "state": "available",
    "reason": None,
    "observed_at": "2026-09-28T00:00:00Z",
    "data_age_seconds": 0.0,
}


def snapshot(ref: str = "ummanu-520", **value: Any) -> dict[str, Any]:
    """A task snapshot as `reads.task_snapshot` answers it, the card carrying `value`."""
    card = {
        "state": "in_progress",
        "title": f"card {ref}",
        "type": "code",
        "origin": None,
        "wait": None,
        "e2e": None,
    }
    card.update(value)
    return {
        "schema_version": 1,
        "kind": "task",
        "observed_at": "2026-09-28T00:00:00Z",
        "ref": ref,
        "card": {"source": AVAILABLE, "value": card},
        "project": {"id": "ummanu", "registered": True},
        "attempt": {"source": AVAILABLE, "value": None},
        "agents": {"source": AVAILABLE, "items": []},
        "heads": {},
        "work": {},
        "events": {"source": AVAILABLE, "items": [], "next_cursor": ""},
    }


def origin(**changes: Any) -> dict[str, Any]:
    block = {
        "po_session": SESSION,
        "request_id": "req-po-1",
        "current_session": SESSION,
        "executor": None,
        "returns": [],
    }
    block.update(changes)
    return block


def wait(state: str = "waiting", **changes: Any) -> dict[str, Any]:
    block = {
        "state": state,
        "target": {
            "kind": "github_run",
            "repo": "vladmesh/ummanu",
            "run_id": 4242,
            "url": RUN_URL,
            "link": RUN_URL,
        },
        "waiting_since": "2026-09-27T12:00:00Z",
        "deadline": "2026-09-27T14:00:00Z",
        "return_to": ["observer", f"po-session:{SESSION}", "card:ummanu-600"],
        "result": None,
        "delivery": "pending",
        "deliveries": {
            "observer": "pending",
            f"po-session:{SESSION}": "pending",
            "card:ummanu-600": "pending",
        },
        "po_sessions": {f"po-session:{SESSION}": {"addressed": SESSION, "received_by": None}},
    }
    block.update(changes)
    return block


def panel(html: str, title: str) -> str:
    """The body of the page's panel titled `title`, or "" when the page has none.

    A disclosure panel matches when its summary starts with `title` (`Delegated cards: 2 — ...`).
    """
    found = re.search(
        rf"<h2>{re.escape(title)}</h2>.*?<div class=\"body\">(.*?)</div></section>", html, re.DOTALL
    ) or re.search(
        rf"<summary>{re.escape(title)}[^<]*</summary><div class=\"body\">(.*?)</div></details>",
        html,
        re.DOTALL,
    )
    return found.group(1) if found else ""


def delegated_details(html: str) -> tuple[str, str]:
    """The delegated-cards disclosure's opening tag and its summary text, or ("", "") when absent."""
    found = re.search(r'(<details [^>]*id="po-delegated"[^>]*>)<summary>([^<]*)</summary>', html)
    return (found.group(1), found.group(2)) if found else ("", "")


class PageFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.tmp / "data"
        self.data.mkdir()
        po_token.ensure_token(self.data)
        self.cookie = po_token.cookie_value(po_token.read_token(self.data))
        self.po = Recording(
            po_session_titles={
                "kind": "po_session_titles",
                "sessions": {
                    SESSION: {"title": "Grill the waits", "state": "closed"},
                    SUCCESSOR: {"title": None, "state": "open"},
                },
            },
            po_session={
                "kind": "po_session",
                "session": {"session_id": SESSION, "title": "Grill the waits", "state": "open"},
                "turns": [],
                "feed": [],
                "queued": [],
            },
        )

    def app(self, reads: Recording, *, sprint_reads: Recording | None = None, po: Any = "default") -> WebApp:
        layers = [
            reads,
            Recording(run_list={"items": []}),
            sprint_reads or Recording(),
            Recording(),
            Recording(),
            Recording(),
            Recording(),
            Recording(),
        ]
        po_layer = self.po if po == "default" else po
        auth = PoTokenLayer(self.tmp, data_dir=self.data) if po_layer is not None else None
        return WebApp(*layers, po_auth=auth, po=po_layer)

    def card_page(self, document: dict[str, Any], **options: Any) -> str:
        response = self.app(Recording(task_snapshot=document), **options).handle(
            "GET", f"/tasks/{document['ref']}"
        )
        self.assertEqual(response.status, 200, response.body[:400])
        return response.body.decode()

    def po_get(self, path: str, reads: Recording, *, query: str = "") -> Any:
        return self.app(reads).handle(
            "GET", path, query=query, headers={"Cookie": f"{po_token.COOKIE_NAME}={self.cookie}"}
        )


class CardOriginTests(PageFixture):
    def test_a_delegated_card_names_its_session_by_title_with_a_link(self) -> None:
        html = self.card_page(snapshot(origin=origin()))
        body = panel(html, "Delegation")
        self.assertIn(f'Delegated by <a href="/po/sessions/{SESSION}">Grill the waits', body)
        self.assertIn("req-po-1", body)
        self.assertNotIn("succeeded", body)
        self.assertIn("nothing has been returned", body)
        self.assertEqual(self.po.calls, [("po_session_titles", {"args": ([SESSION],)})])

    def test_an_untitled_session_shows_its_short_id(self) -> None:
        self.po.answers["po_session_titles"] = {
            "kind": "po_session_titles",
            "sessions": {SESSION: {"title": None, "state": "open"}},
        }
        body = panel(self.card_page(snapshot(origin=origin())), "Delegation")
        self.assertIn(f'<a href="/po/sessions/{SESSION}"><span class="id">{SESSION[:8]}</span></a>', body)
        self.assertNotIn("no such session", body)

    def test_a_successor_session_takes_the_result_and_the_returns_are_listed(self) -> None:
        returns = [
            {
                "state": "blocked",
                "status": "delivered",
                "delivered_at": "2026-09-27T13:00:00Z",
                "session": SESSION,
            },
            {
                "state": "done",
                "status": "delivered",
                "delivered_at": "2026-09-27T15:00:00Z",
                "session": SUCCESSOR,
            },
            {"state": "done", "status": None, "delivered_at": None, "session": None},
        ]
        body = panel(
            self.card_page(snapshot(origin=origin(current_session=SUCCESSOR, returns=returns))), "Delegation"
        )
        self.assertIn(
            f'the result goes to <a href="/po/sessions/{SUCCESSOR}"><span class="id">{SUCCESSOR[:8]}</span></a>',
            body,
        )
        rows = re.findall(r"<tr>(.*?)</tr>", body)[1:]
        self.assertEqual(len(rows), 3)
        self.assertIn("blocked", rows[0])
        self.assertIn("2026-09-27T13:00:00Z", rows[0])
        self.assertIn(f"/po/sessions/{SESSION}", rows[0])
        self.assertIn(f"/po/sessions/{SUCCESSOR}", rows[1])
        self.assertIn("pending", rows[2])

    def test_a_card_with_no_origin_shows_nothing_new_and_asks_no_titles(self) -> None:
        html = self.card_page(snapshot())
        for title in ("Delegation", "Wait", "E2E"):
            self.assertEqual(panel(html, title), "", title)
        self.assertNotIn("Delegated by", html)
        self.assertEqual(self.po.calls, [])


class CardWaitTests(PageFixture):
    def test_each_target_kind(self) -> None:
        cases = {
            "run": (
                wait()["target"],
                f'<a href="{RUN_URL}" rel="noreferrer">GitHub run vladmesh/ummanu#4242</a>',
            ),
            "run with only its url": (
                {"kind": "github_run", "repo": "vladmesh/ummanu", "run_id": 4242, "url": RUN_URL},
                f'href="{RUN_URL}"',
            ),
            "card": (
                {"kind": "card", "ref": "ummanu-600", "states": ["done", "blocked"]},
                '<a class="ref" href="/tasks/ummanu-600">ummanu-600</a> reaching done or blocked',
            ),
            "time": ({"kind": "time", "at": "2026-10-01T09:00:00Z"}, "the time 2026-10-01T09:00:00Z"),
        }
        for name, (target, expected) in cases.items():
            with self.subTest(target=name):
                body = panel(self.card_page(snapshot(type="wait", wait=wait(target=target))), "Wait")
                self.assertIn(expected, body)
                self.assertIn("2026-09-27T12:00:00Z", body)
                self.assertIn("2026-09-27T14:00:00Z", body)

    def test_each_state_and_the_frozen_result_with_its_evidence(self) -> None:
        result = {"outcome": "target_reached", "summary": "the run concluded success", "evidence": RUN_URL}
        for state in (
            "waiting",
            "result_ready",
            "delivered",
            "deadline_passed",
            "source_unreachable",
            "cancelled",
        ):
            with self.subTest(state=state):
                frozen = (
                    None
                    if state == "waiting"
                    else {
                        **result,
                        "outcome": "target_reached" if state in ("result_ready", "delivered") else state,
                    }
                )
                body = panel(self.card_page(snapshot(type="wait", wait=wait(state, result=frozen))), "Wait")
                self.assertIn('<span class="chip', body)
                self.assertIn(state.replace("_", " "), body)
                if frozen is None:
                    self.assertNotIn("<th>result</th>", body)
                else:
                    self.assertIn("the run concluded success", body)
                    self.assertIn(f'<a href="{RUN_URL}" rel="noreferrer">evidence</a>', body)

    def test_each_return_address_with_its_delivery(self) -> None:
        block = wait(
            "delivered",
            deliveries={
                "observer": "accepted",
                f"po-session:{SESSION}": "accepted",
                "card:ummanu-600": "pending",
            },
            po_sessions={f"po-session:{SESSION}": {"addressed": SESSION, "received_by": SUCCESSOR}},
        )
        body = panel(self.card_page(snapshot(type="wait", wait=block)), "Wait")
        rows = re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td></tr>", body)
        self.assertEqual(len(rows), 3)
        self.assertIn("observer", rows[0][0])
        self.assertIn("accepted", rows[0][1])
        self.assertIn(f'/po/sessions/{SESSION}">Grill the waits', rows[1][0])
        self.assertIn(f'taken by its successor <a href="/po/sessions/{SUCCESSOR}">', rows[1][0])
        self.assertIn("/tasks/ummanu-600", rows[2][0])
        self.assertIn("pending", rows[2][1])

    def test_a_malformed_wait_says_so(self) -> None:
        body = panel(
            self.card_page(
                snapshot(
                    type="wait",
                    wait={"state": "malformed", "reason": "the card carries no well-formed wait spec"},
                )
            ),
            "Wait",
        )
        self.assertIn("malformed", body)
        self.assertIn("no well-formed wait spec", body)


class CardE2eTests(PageFixture):
    RUN: ClassVar[dict[str, Any]] = {
        "sha": "0123456789abcdef0123",
        "dispatch_id": "ummanu-520-e2e-1",
        "workflow": "e2e.yml",
        "state": "success",
        "run": RUN_URL,
        "wait_card": "ummanu-530",
        "dispatched_at": "2026-09-27T12:00:00Z",
        "result": {"outcome": "target_reached", "conclusion": "success", "summary": "all stands green"},
    }

    def test_each_run_with_its_sha_link_result_and_wait_card(self) -> None:
        block = {
            "runs_dispatched": 2,
            "run_cap": 3,
            "budget": None,
            "runs": [
                self.RUN,
                {
                    **self.RUN,
                    "sha": "fedcba9876543210",
                    "state": "waiting",
                    "result": None,
                    "wait_card": "ummanu-531",
                },
            ],
        }
        body = panel(self.card_page(snapshot(e2e=block)), "E2E")
        self.assertIn("2 run(s) dispatched of this card's cap of 3", body)
        self.assertIn("<code>0123456789ab</code>", body)
        self.assertIn(f'<a href="{RUN_URL}" rel="noreferrer">run</a>', body)
        self.assertIn("all stands green", body)
        self.assertIn('href="/tasks/ummanu-530"', body)
        self.assertIn('href="/tasks/ummanu-531"', body)
        self.assertIn("waiting", body)

    def test_the_budget_mark_and_the_sprint_it_is_charged_to(self) -> None:
        block = {
            "runs_dispatched": 3,
            "run_cap": None,
            "budget": "sprint:1469",
            "runs": [self.RUN],
            "mark": "e2e: budget spent, waiting on ummanu-599",
            "waiting_on": "ummanu-599",
        }
        body = panel(self.card_page(snapshot(e2e=block)), "E2E")
        self.assertIn('charged to <a href="/sprints/sprint%3A1469">sprint:1469</a>', body)
        self.assertIn("budget spent", body)
        self.assertIn('href="/tasks/ummanu-599"', body)

    def test_the_after_merge_state_and_its_runs(self) -> None:
        block = {
            "placement": "after_merge",
            "state": "covered by " + RUN_URL,
            "merge_sha": "aaaabbbbccccdddd",
            "run": RUN_URL,
            "covered_by": "d-1",
            "carrier": "ummanu-525",
            "runs_dispatched": 0,
            "run_cap": 3,
            "budget": None,
            "runs": [],
            "after_merge_runs": [{**self.RUN, "state": "waiting", "result": None}],
        }
        body = panel(self.card_page(snapshot(e2e=block)), "E2E")
        self.assertIn("after merge", body)
        self.assertIn("covered by", body)
        self.assertIn("<code>aaaabbbbcccc</code>", body)
        self.assertIn('href="/tasks/ummanu-525"', body)
        self.assertIn('href="/tasks/ummanu-530"', body)
        self.assertNotIn("no e2e run has been dispatched", body)


class PoSessionPageTests(PageFixture):
    DELEGATED: ClassVar[dict[str, Any]] = {
        "kind": "po_delegated",
        "session": SESSION,
        "source": AVAILABLE,
        "items": [
            {
                "ref": "ummanu-520",
                "title": "Decide the cap",
                "type": "decision",
                "state": "in_progress",
                "relation": "delegated",
                "last_return": None,
            },
            {
                "ref": "ummanu-521",
                "title": "Run the op",
                "type": "operation",
                "state": "done",
                "relation": "inherited",
                "last_return": {"state": "done", "status": "delivered", "session": SESSION},
            },
        ],
    }

    def test_the_page_lists_the_delegated_cards_with_their_states(self) -> None:
        reads = Recording(po_delegated=self.DELEGATED)
        response = self.po_get(f"/po/sessions/{SESSION}", reads)
        self.assertEqual(response.status, 200)
        body = panel(response.body.decode(), "Delegated cards")
        self.assertIn('href="/tasks/ummanu-520"', body)
        self.assertIn("Decide the cap", body)
        self.assertIn("decision", body)
        self.assertIn("in progress", body)
        self.assertIn("(as successor)", body)
        self.assertIn("delivered", body)
        self.assertEqual(reads.calls, [("po_delegated", {"args": (SESSION,)})])

    def test_the_json_twin_carries_the_same_list_and_the_poller_skips_it(self) -> None:
        reads = Recording(po_delegated=self.DELEGATED)
        document = json.loads(self.po_get(f"/po/api/sessions/{SESSION}", reads).body)
        self.assertEqual(
            [item["ref"] for item in document["delegated"]["items"]], ["ummanu-520", "ummanu-521"]
        )
        self.assertEqual(len(reads.calls), 1)

        polled = json.loads(self.po_get(f"/po/api/sessions/{SESSION}", reads, query="cards=0").body)
        self.assertIsNone(polled["delegated"])
        self.assertEqual(len(reads.calls), 1, "the poll reads no board")
        self.assertIn("?cards=0", pages._PO_SESSION_SCRIPT)

    def test_a_session_that_delegated_nothing_says_so(self) -> None:
        reads = Recording(po_delegated={**self.DELEGATED, "items": []})
        html = self.po_get(f"/po/sessions/{SESSION}", reads).body.decode()
        body = panel(html, "Delegated cards")
        self.assertIn("delegated no card", body)
        tag, summary = delegated_details(html)
        self.assertEqual(summary, "Delegated cards: none")
        self.assertNotIn(" open", tag)

    def test_the_panel_sits_collapsed_by_the_title_before_the_feed(self) -> None:
        reads = Recording(po_delegated=self.DELEGATED)
        html = self.po_get(f"/po/sessions/{SESSION}", reads).body.decode()
        tag, summary = delegated_details(html)
        self.assertTrue(tag, "the page has the delegated-cards disclosure")
        self.assertNotIn(" open", tag, "collapsed by default")
        self.assertEqual(summary, "Delegated cards: 2 — 1 in progress, 1 done")
        start = html.index('id="po-delegated"')
        self.assertLess(html.index('id="po-title"'), start)
        self.assertLess(start, html.index('id="po-feed"'))
        self.assertLess(start, html.index('id="po-send"'))

    def test_the_summary_tallies_columns_and_hints_at_results_not_returned(self) -> None:
        items = [
            {
                "ref": "ummanu-530",
                "state": "done",
                "last_return": {"state": "done", "status": "delivered"},
            },
            {"ref": "ummanu-531", "state": "done", "last_return": {"state": "done", "status": None}},
            {"ref": "ummanu-532", "state": "validate", "last_return": None},
            {"ref": "ummanu-533", "state": "blocked", "last_return": None},
        ]
        reads = Recording(po_delegated={**self.DELEGATED, "items": items})
        _, summary = delegated_details(self.po_get(f"/po/sessions/{SESSION}", reads).body.decode())
        self.assertEqual(
            summary, "Delegated cards: 4 — 2 done, 1 in validate, 1 blocked · 2 results not returned yet"
        )

    def test_the_in_place_update_leaves_the_panel_alone(self) -> None:
        # The session script replaces exactly PO_SESSION_BLOCKS by id; the disclosure is in none of them,
        # so its `open`, set by the owner, survives every in-place update and it never moves.
        reads = Recording(po_delegated=self.DELEGATED)
        html = self.po_get(f"/po/sessions/{SESSION}", reads).body.decode()
        start = html.index('<details class="panel po-delegated"')
        end = html.index("</details>", start)
        self.assertNotIn("po-delegated", pages.PO_SESSION_BLOCKS)
        # #po-head is the one swapped block above the panel, and it closes before the rename form;
        # every other swapped block opens after the panel has closed, so none of them contains it.
        self.assertLess(html.index('id="po-head"'), html.index('id="po-title"'))
        self.assertLess(html.index('id="po-title"'), start)
        for block in pages.PO_SESSION_BLOCKS:
            if block != "po-head":
                self.assertLess(end, html.index(f'id="{block}"'), f"#{block} opens after the panel")


class SprintPageTests(PageFixture):
    def sprint_page(self, waiting_on: Any) -> str:
        document = {
            "ref": "sprint:1469",
            "observed_at": "2026-09-28T00:00:00Z",
            "sprint": {"source": AVAILABLE, "value": {"ref": "sprint:1469", "status": "open", "goal": "g"}},
            "observer": {},
            "work": {"waiting_on": waiting_on},
        }
        response = self.app(Recording(), sprint_reads=Recording(sprint_state=document)).handle(
            "GET", "/sprints/sprint:1469"
        )
        self.assertEqual(response.status, 200)
        return response.body.decode()

    def test_each_kind_is_listed_with_a_pointer_to_its_card(self) -> None:
        waiting_on = [
            {
                "kind": "run",
                "card": "ummanu-540",
                "detail": f"waits for {RUN_URL} since 2026-09-27T12:00:00Z",
            },
            {"kind": "owner", "card": "ummanu-542", "detail": "decision handed to the owner: pay"},
            {"kind": "po", "card": "ummanu-544", "detail": "operation card with the PO"},
        ]
        body = panel(self.sprint_page(waiting_on), "Waiting on")
        rows = re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>", body)
        self.assertEqual(
            [row[0] for row in rows],
            [
                '<span class="chip">a run</span>',
                '<span class="chip chip-warn">the owner</span>',
                '<span class="chip">the PO</span>',
            ],
        )
        self.assertIn('href="/tasks/ummanu-540"', rows[0][1])
        self.assertIn(f'<a href="{RUN_URL}" rel="noreferrer">{RUN_URL}</a>', rows[0][2])
        self.assertIn('href="/tasks/ummanu-542"', rows[1][1])
        self.assertIn('href="/tasks/ummanu-544"', rows[2][1])

    def test_an_empty_or_unknown_list_renders_nothing(self) -> None:
        for waiting_on in ([], None):
            with self.subTest(waiting_on=waiting_on):
                self.assertNotIn("Waiting on", self.sprint_page(waiting_on))


class CardWaitsTests(unittest.TestCase):
    """`card_waits`, the derivation of `work.waiting_on`, over each kind."""

    def test_each_kind(self) -> None:
        handed = {
            "extensions": {
                "extra": {
                    "waiting_owner": "2026-09-27T12:00:00+00:00",
                    "waiting_owner_reason": "pay",
                    "waiting_owner_by": "po",
                }
            }
        }
        e2e_run = {"sha": "0123456789abcdef", "run": RUN_URL, "state": "waiting"}
        cases = [
            ({"ref": "w", "type": "wait", "state": "in_progress", "wait": wait()}, [("run", RUN_URL)]),
            (
                {"ref": "w", "type": "wait", "state": "in_progress", "wait": wait("result_ready")},
                [("run", "result ready")],
            ),
            (
                {"ref": "c", "type": "code", "state": "validate", "e2e": {"runs": [e2e_run]}},
                [("run", RUN_URL)],
            ),
            (
                {
                    "ref": "c",
                    "type": "code",
                    "state": "validate",
                    "e2e": {"runs": [{**e2e_run, "state": "identifying", "run": None}]},
                },
                [("run", "not identified yet")],
            ),
            (
                {
                    "ref": "c",
                    "type": "code",
                    "state": "validate",
                    "e2e": {"runs": [], "mark": "e2e: budget spent, waiting on d-1"},
                },
                [("dependency", "d-1")],
            ),
            (
                {
                    "ref": "m",
                    "type": "code",
                    "state": "done",
                    "e2e": {"placement": "after_merge", "after_merge_runs": [e2e_run]},
                },
                [("run", RUN_URL)],
            ),
            (
                {
                    "ref": "m",
                    "type": "code",
                    "state": "done",
                    "e2e": {"placement": "after_merge", "mark": "e2e: budget spent, waiting on d-2"},
                },
                [("dependency", "d-2")],
            ),
            ({"ref": "d", "type": "decision", "state": "in_progress", **handed}, [("owner", "pay")]),
            ({"ref": "d", "type": "decision", "state": "in_progress"}, [("po", "with the PO")]),
            ({"ref": "o", "type": "operation", "state": "in_progress"}, [("po", "with the PO")]),
            # And the cards that wait for nothing.
            ({"ref": "w", "type": "wait", "state": "done", "wait": wait("delivered")}, []),
            ({"ref": "w", "type": "wait", "state": "blocked", "wait": wait()}, []),
            ({"ref": "w", "type": "wait", "state": "in_progress", "wait": wait("deadline_passed")}, []),
            ({"ref": "c", "type": "code", "state": "done", "e2e": {"runs": [e2e_run], "mark": "stale"}}, []),
            (
                {
                    "ref": "c",
                    "type": "code",
                    "state": "validate",
                    "e2e": {"runs": [{**e2e_run, "state": "success"}]},
                },
                [],
            ),
            ({"ref": "d", "type": "decision", "state": "ready"}, []),
            ({"ref": "d", "type": "decision", "state": "done", **handed}, []),
            ({"ref": "x", "type": "code", "state": "in_progress"}, []),
        ]
        for card, expected in cases:
            with self.subTest(card=card):
                found = card_waits(card)
                self.assertEqual([entry["kind"] for entry in found], [kind for kind, _ in expected])
                for entry, (_kind, fragment) in zip(found, expected, strict=True):
                    self.assertEqual(entry["card"], card["ref"])
                    self.assertIn(fragment, entry["detail"])
                    self.assertIn(entry["kind"], WAITING_ON_KINDS)


def merged(
    ref: str, mark: dict[str, Any] | None, *, carried: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A merged card as `TaskReader` gives it: the `e2e` field in its bag, and the view built from it."""
    document: dict[str, Any] = {"runs": []}
    if mark is not None:
        document["after_merge"] = mark
    if carried:
        document["after_merge_runs"] = carried
    card = {"ref": ref, "type": "code", "state": "done", "sprint": "sprint:1469"}
    card["extensions"] = {"extra": {"e2e": json.dumps(document)}}
    card["e2e"] = e2e_view(card)
    return card


AM_RUN_URL = "https://github.com/vladmesh/ummanu/actions/runs/5150"


def am_run(state: str = "waiting", **changes: Any) -> dict[str, Any]:
    """One after-merge run record as the carrier holds it (`E2eRun`, `placement: after_merge`)."""
    record = {
        "dispatch_id": "ummanu-561-e2e-am-1-0001",
        "sha": "cafe0000cafe0000cafe0000cafe0000cafe0000",
        "repo": "vladmesh/ummanu",
        "branch": "e2e/after-merge/1",
        "workflow": "e2e.yml",
        "intent_at": "2026-09-28T10:00:00Z",
        "dispatch": "sent",
        "run_id": 5150,
        "run_url": AM_RUN_URL,
        "head_sha": "cafe0000cafe0000cafe0000cafe0000cafe0000",
        "wait_ref": "ummanu-570",
        "placement": "after_merge",
        "covered": [
            {"ref": "ummanu-560", "merge_sha": "a" * 40},
            {"ref": "ummanu-561", "merge_sha": "b" * 40},
        ],
    }
    if state != "waiting":
        record["result"] = {"outcome": "target_reached", "conclusion": state, "summary": state, "key": "k"}
    record.update(changes)
    return record


def am_mark(state: str, merge: str = "a" * 40, **changes: Any) -> dict[str, Any]:
    mark = {"merge_sha": merge, "state": state}
    if state != "pending":
        mark.update(dispatch_id="ummanu-561-e2e-am-1-0001", run_url=AM_RUN_URL, carrier="ummanu-561")
    mark.update(changes)
    return mark


class AfterMergeWaitsTests(unittest.TestCase):
    """secretary-1811 rework: every card a coalesced after-merge run covers waits on that run."""

    def test_a_covered_card_that_is_not_the_carrier_waits_on_the_run(self) -> None:
        [entry] = card_waits(merged("ummanu-560", am_mark("covered")))
        self.assertEqual(entry["kind"], "run")
        self.assertIn(AM_RUN_URL, entry["detail"])
        self.assertIn("carried by ummanu-561", entry["detail"])

    def test_the_carrier_says_its_run_once(self) -> None:
        carrier = merged("ummanu-561", am_mark("covered", "b" * 40), carried=[am_run()])
        [entry] = card_waits(carrier)
        self.assertEqual((entry["kind"], entry["card"]), ("run", "ummanu-561"))
        self.assertIn(AM_RUN_URL, entry["detail"])

    def test_a_covered_run_not_yet_identified_is_named_by_its_dispatch_id(self) -> None:
        [entry] = card_waits(merged("ummanu-560", am_mark("covered", run_url="")))
        self.assertIn("ummanu-561-e2e-am-1-0001 (not identified yet)", entry["detail"])

    def test_a_pending_card_is_queued_for_the_next_run(self) -> None:
        [entry] = card_waits(merged("ummanu-562", am_mark("pending")))
        self.assertEqual((entry["kind"], entry["detail"]), ("run", "queued for the next after-merge run"))

    def test_green_red_and_declined_wait_for_nothing(self) -> None:
        for state in ("green", "red", "declined"):
            with self.subTest(state=state):
                self.assertEqual(
                    card_waits(merged("ummanu-560", am_mark(state, hotfix="ummanu-590"))), []
                )

    def test_a_carrier_whose_run_answered_does_not_wait_on_it_while_its_mark_still_says_covered(self) -> None:
        carrier = merged("ummanu-561", am_mark("covered", "b" * 40), carried=[am_run("success")])
        self.assertEqual(card_waits(carrier), [])


class PoSessionTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakePoStore()
        self.store.claim_session(
            session_id=SESSION,
            cli="claude",
            model="opus",
            cwd="/w",
            cli_session_id=None,
            title="Grill the waits",
        )
        self.store.claim_session(
            session_id=SUCCESSOR, cli="claude", model="opus", cwd="/w", cli_session_id=None
        )
        self.layer = PoLayer(
            Path("/nonexistent-instance"),
            data_dir=Path("/nonexistent-data"),
            store=self.store,
            models={"claude": ("opus",)},
        )

    def test_found_sessions_are_named_and_a_gone_one_is_left_out(self) -> None:
        answer = self.layer.po_session_titles([SESSION, SUCCESSOR, GONE, "", SESSION])
        self.assertEqual(
            answer["sessions"],
            {
                SESSION: {"title": "Grill the waits", "state": "open"},
                SUCCESSOR: {"title": None, "state": "open"},
            },
        )

    def test_a_store_that_does_not_answer_is_refused_as_unavailable(self) -> None:
        self.store.crash()
        with self.assertRaises(RuntimeUnavailable):
            self.layer.po_session_titles([SESSION])


class HostileValueTests(PageFixture):
    """Rule 6: every new read over missing, partial and legacy values; none raises, none answers 500."""

    HOSTILE_ORIGINS: ClassVar[dict[str, Any]] = {
        "the session row is gone": origin(po_session=GONE, current_session=GONE),
        "no current session": origin(current_session=None),
        "returns not a list": origin(returns="nope"),
        "a return row not a mapping": origin(returns=[None, 7, "x", {"state": None}]),
        "a return row with no fields": origin(returns=[{}]),
        "an empty origin": {},
        "an origin of strings": {"po_session": 5, "current_session": ["x"], "returns": {}},
    }
    HOSTILE_WAITS: ClassVar[dict[str, Any]] = {
        "a legacy wait with no target fields": {"state": "waiting", "target": {}},
        "no deliveries, no return_to": {
            "state": "delivered",
            "target": {"kind": "time", "at": "2026-01-01T00:00:00Z"},
        },
        "a run target with no url": {
            "state": "waiting",
            "target": {"kind": "github_run", "repo": "o/r", "run_id": 1},
        },
        "a card target with no states": {
            "state": "waiting",
            "target": {"kind": "card", "ref": "ummanu-9"},
        },
        "an unknown target kind": {"state": "waiting", "target": {"kind": "moon"}},
        "a target that is not a mapping": {"state": "waiting", "target": "run"},
        "a result that is not a mapping": {**wait("result_ready"), "result": "done"},
        "a delivery for an address not returned to": {
            **wait(),
            "return_to": None,
            "deliveries": {"po-session:x": "pending"},
            "po_sessions": None,
        },
        "no state at all": {},
    }
    HOSTILE_E2E: ClassVar[dict[str, Any]] = {
        "no runs at all": {},
        "runs not a list": {"runs": "x", "after_merge_runs": 3},
        "a run not a mapping": {"runs": [None, "x", {}]},
        "a run with a result not a mapping": {"runs": [{"sha": None, "result": "red", "run": 5}]},
        "after merge with no mark": {"placement": "after_merge"},
    }

    def test_the_card_page_renders_every_hostile_block(self) -> None:
        table = (
            [("origin", name, value) for name, value in self.HOSTILE_ORIGINS.items()]
            + [("wait", name, value) for name, value in self.HOSTILE_WAITS.items()]
            + [("e2e", name, value) for name, value in self.HOSTILE_E2E.items()]
            + [
                (block, "not a mapping", value)
                for block in ("origin", "wait", "e2e")
                for value in ("x", 3, [1])
            ]
        )
        for block, name, value in table:
            with self.subTest(block=block, value=name):
                self.card_page(snapshot(type="wait" if block == "wait" else "code", **{block: value}))

    def test_a_gone_session_shows_its_short_id(self) -> None:
        self.po.answers["po_session_titles"] = {"kind": "po_session_titles", "sessions": {}}
        body = panel(
            self.card_page(snapshot(origin=origin(po_session=GONE, current_session=GONE))), "Delegation"
        )
        self.assertIn(f'<span class="id">{GONE[:8]}</span>', body)
        self.assertIn("no such session", body)

    def test_a_po_store_that_is_unavailable_renders_unavailable_not_a_500(self) -> None:
        self.po.answers["po_session_titles"] = RuntimeUnavailable(
            "the PO session store is not available: down"
        )
        html = self.card_page(snapshot(origin=origin(), type="wait", wait=wait()))
        body = panel(html, "Delegation")
        self.assertIn("unavailable", body)
        self.assertIn(f'<span class="id">{SESSION[:8]}</span>', body)
        self.assertIn("unavailable", panel(html, "Wait"))

    def test_a_process_without_the_po_layer_draws_short_ids(self) -> None:
        body = panel(self.card_page(snapshot(origin=origin()), po=None), "Delegation")
        self.assertIn(f'<span class="id">{SESSION[:8]}</span>', body)
        self.assertNotIn("unavailable", body)

    def test_po_sessions_named_tolerates_any_value(self) -> None:
        for value in (
            None,
            {},
            {"card": None},
            {"card": {"value": "x"}},
            snapshot(origin="x", wait=[1]),
            snapshot(
                origin=origin(returns=[None, {"session": 3}]),
                wait={"po_sessions": {"a": None, "b": {"addressed": 4}}},
            ),
        ):
            with self.subTest(value=value):
                self.assertIsInstance(pages.po_sessions_named(value or {}), list)

    def test_card_waits_tolerates_any_value(self) -> None:
        hostile = [
            {},
            {"ref": None},
            {"ref": "c", "state": None, "type": None},
            {"ref": "w", "type": "wait", "state": "in_progress", "wait": "x"},
            {"ref": "w", "type": "wait", "state": "in_progress", "wait": {"state": "waiting"}},
            {
                "ref": "w",
                "type": "wait",
                "state": "in_progress",
                "wait": {"state": "waiting", "target": {"kind": "card", "states": "done"}},
            },
            {"ref": "c", "type": "code", "state": "validate", "e2e": "x"},
            {
                "ref": "c",
                "type": "code",
                "state": "validate",
                "e2e": {"runs": "x", "after_merge_runs": [None, 3, {"state": "waiting"}]},
            },
            {"ref": "d", "type": "decision", "state": "in_progress", "extensions": "x"},
            {
                "ref": "d",
                "type": "decision",
                "state": "in_progress",
                "extensions": {
                    "extra": {
                        "waiting_owner": "not a time",
                        "waiting_owner_reason": "r",
                        "waiting_owner_by": "po",
                    }
                },
            },
        ]
        for card in hostile:
            with self.subTest(card=card):
                for entry in card_waits(card):
                    self.assertEqual(set(entry), {"kind", "card", "detail"})
                    self.assertIn(entry["kind"], WAITING_ON_KINDS)

    def test_the_po_session_page_renders_a_board_that_did_not_answer(self) -> None:
        for delegated in (
            RuntimeUnavailable("the board store is down"),
            {
                "kind": "po_delegated",
                "source": {"state": "unavailable", "reason": "the board could not be read: down"},
                "items": None,
            },
            {"kind": "po_delegated"},
            {
                "kind": "po_delegated",
                "source": AVAILABLE,
                "items": [None, "x", {}, {"ref": None, "last_return": "x"}],
            },
        ):
            with self.subTest(delegated=delegated):
                response = self.po_get(f"/po/sessions/{SESSION}", Recording(po_delegated=delegated))
                self.assertEqual(response.status, 200)
                body = panel(response.body.decode(), "Delegated cards")
                self.assertTrue(body)
                if not isinstance(delegated, dict) or not isinstance(delegated.get("items"), list):
                    self.assertIn("could not find out the cards this session delegated", body)

    def test_the_sprint_page_renders_hostile_waiting_on(self) -> None:
        for waiting_on in (
            "x",
            3,
            {"kind": "run"},
            [None, "x", {}, {"kind": None, "card": None, "detail": None}],
        ):
            with self.subTest(waiting_on=waiting_on):
                SprintPageTests.sprint_page(self, waiting_on)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
