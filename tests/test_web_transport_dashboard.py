"""The operator's half of the web transport: the pause, the open sprints, the history, and the
owner's two writes on a card.

Every test goes through :class:`ummanu.web.app.WebApp`, the object the socket handler calls.
The four new layers are either the real ones over the fake board (`TransportFixture`, where the
question is what a write does) or recording fakes (where the question is what the transport hands
down and how a page draws what came back). Nothing here reaches a live installation.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from tests.webproto_sprint_fixtures import SprintProtocolFixture
from ummanu.board import sprint_dod
from ummanu.config import validate
from ummanu.status import collect_status
from ummanu.tasks import TaskWriter
from ummanu.web.app import ROUTES, WebApp
from ummanu.webproto.card_ops import MOVE_TARGETS, CardOperationLayer
from ummanu.webproto.errors import InstallationUnavailable, ReadError
from ummanu.webproto.reads import ReadLayer, health_summary


def available() -> dict[str, Any]:
    return {
        "state": "available",
        "reason": None,
        "data_age_seconds": 0.0,
        "observed_at": "2026-09-13T12:00:00Z",
    }


class Recording:
    """A layer that records every call and answers with the document it was given per operation."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def operation(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, {"args": args, **kwargs}))
            answer = self.answers.get(name, {"kind": name})
            if isinstance(answer, Exception):
                raise answer
            return answer

        return operation


def pause_document(*, paused: bool = False, mode: str | None = None) -> dict[str, Any]:
    return {
        "kind": "pause_state",
        "state": {
            "paused": paused,
            "mode": mode,
            "since": "2026-09-13T11:00:00Z" if paused else None,
            "actor": "web" if paused else None,
            "pause_reason": "a release is being cut" if paused else None,
            "source": available(),
        },
        "dispatcher": {"phase": "production", "tracked_cards": 1, "source": available()},
        "heads": {
            "cards": [
                {"ref": "ummanu-9", "state": "claimed", "worker": "running", "reviewer": "not-running"}
            ],
            "observers": [],
        },
    }


def sprint_listing(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "sprint_list", "sprints": {"source": available(), "items": items}}


def sprint_item(ref: str = "sprint:7") -> dict[str, Any]:
    return {
        "ref": ref,
        "status": "open",
        "product": "ummanu",
        "reservations": ["ummanu"],
        "goal": "a goal " * 60,
        "current_task": {"ref": "ummanu-9", "live": True, "reason": "cut", "source": available()},
        "current_card_state": {
            "card": "ummanu-9",
            "state": "in_progress",
            "since": "2026-09-13T09:00:00Z",
            "age_seconds": 10800.0,
            "transition": "recorded",
            "reason": "ummanu-9 stands in in_progress",
            "source": available(),
        },
        "observer": {
            "declared": {"profile": "claude-observer", "state": "declared"},
            "launch": {"state": "running", "reason": "an observer head is up", "source": available()},
        },
        "waiting": {
            "state": "waiting",
            "reason": "the observer is waiting for the reviewer",
            "source": available(),
        },
        "checks": {"gate": {"state": "green"}, "reason": "the gate is green for abc", "source": available()},
        "budget": {
            "total": 13,
            "thresholds": {"signal": 12, "hard": 30},
            "signal_reached": True,
            "hard_reached": False,
            "by_type": {"red_review": 13},
        },
        "cards": {"states": {"assessment": ["ummanu-9"]}, "source": available()},
        "decision": {
            "entry": {
                "selected_step": "rework ummanu-9",
                "selected_why": "the review is red",
                "next_safe_step": "wait",
            },
            "freshness": {"value": {"fresh": False, "error": "resume_stale"}},
        },
    }


def history(items: list[dict[str, Any]], *, more: bool = False) -> dict[str, Any]:
    return {
        "kind": "command_history",
        "observed_at": "2026-09-13T12:00:00Z",
        "limit": 25,
        "commands": {
            "source": available(),
            "items": items,
            "has_more": more,
            "next_cursor": "c-2" if more else None,
        },
    }


def command(action: str, ref: str, *, outcome: str = "success", reason: str | None = None) -> dict[str, Any]:
    return {
        "occurred_at": "2026-09-13T11:59:00Z",
        "actor": {"role": "dispatcher", "id": "ummanu-production"},
        "action": action,
        "entity": {"kind": "card", "ref": ref},
        "result": {"outcome": outcome, "reason": reason},
        "request_id": "r-1",
        "event_id": "evt-1",
    }


class FakeAppFixture(unittest.TestCase):
    """The application over recording fakes for every layer."""

    def setUp(self) -> None:
        self.reads = Recording(
            system_snapshot={
                "observed_at": "2026-09-13T12:00:00Z",
                "installation": {
                    "instance": "/i",
                    "name": "test",
                    "data_dir": "/d",
                    "health": {"source": available(), "status": health_summary({})},
                },
                "projects": {"source": available(), "items": []},
                "tasks": {"source": available(), "items": []},
                "agents": {"source": available(), "items": []},
            }
        )
        self.ops = Recording(card_runs={"items": []})
        self.sprint_reads = Recording(sprint_list=sprint_listing([sprint_item()]))
        self.sprint_ops = Recording(
            sprint_comment={"kind": "sprint_comment", "comment_id": "evt-c", "saved": True},
            sprint_close={"kind": "sprint_closed"},
        )
        self.pause_reads = Recording(pause_state=pause_document())
        self.pause_ops = Recording(pause_drain={"kind": "pause_drain"}, pause_resume={"kind": "pause_resume"})
        self.command_reads = Recording(
            command_history=history([command("card.assessed", "ummanu-9", outcome="", reason="review:red")])
        )
        self.card_ops = Recording(task_comment={"kind": "card_commented"}, task_move={"kind": "card_moved"})

    def app(self) -> WebApp:
        return WebApp(
            self.reads,
            self.ops,
            self.sprint_reads,
            self.sprint_ops,
            self.pause_reads,
            self.pause_ops,
            self.command_reads,
            self.card_ops,
        )

    def get(self, path: str, query: str = ""):
        return self.app().handle("GET", path, query=query)

    def post(self, path: str, payload: dict[str, Any]):
        return self.app().handle("POST", path, body=json.dumps(payload).encode("utf-8"))

    def text(self, response) -> str:
        return response.body.decode("utf-8")

    def error(self, response) -> dict[str, Any]:
        return json.loads(response.body)["error"]


# -- criterion 1: the routes name their operations and hand down exactly what was sent ------------


class RouteTests(FakeAppFixture):
    def test_the_operator_routes_are_in_the_table_naming_their_operation(self) -> None:
        table = {(route.method, route.pattern): route.operation for route in ROUTES}
        self.assertEqual(table[("GET", "/api/pause")], "pause_reads.pause_state")
        self.assertEqual(table[("GET", "/api/pause/scope")], "pause_reads.pause_scope")
        self.assertEqual(table[("POST", "/api/pause/drain")], "pause_ops.pause_drain")
        self.assertEqual(table[("POST", "/api/pause/resume")], "pause_ops.pause_resume")
        self.assertEqual(table[("GET", "/api/sprints")], "sprint_reads.sprint_list")
        self.assertEqual(table[("POST", "/api/sprints/{ref}/comment")], "sprint_ops.sprint_comment")
        self.assertEqual(table[("POST", "/api/sprints/{ref}/close")], "sprint_ops.sprint_close")
        self.assertEqual(table[("GET", "/api/history")], "command_reads.command_history")
        self.assertEqual(table[("GET", "/api/history/{request_id}")], "command_reads.command_request")
        self.assertEqual(table[("GET", "/history")], "command_reads.command_history")
        self.assertEqual(table[("POST", "/api/tasks/{ref}/comment")], "card_ops.task_comment")
        self.assertEqual(table[("POST", "/api/tasks/{ref}/move")], "card_ops.task_move")

    def test_a_drain_carries_the_reason_and_names_the_web_as_the_actor(self) -> None:
        response = self.post("/api/pause/drain", {"reason": "cutting a release"})
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.pause_ops.calls,
            [("pause_drain", {"args": (), "actor": "web", "reason": "cutting a release"})],
        )

    def test_a_drain_without_a_reason_is_refused_before_the_layer(self) -> None:
        self.assertEqual(self.post("/api/pause/drain", {}).status, 400)
        self.assertEqual(self.post("/api/pause/drain", {"reason": "x", "mode": "freeze"}).status, 400)
        self.assertEqual(self.pause_ops.calls, [])

    def test_a_resume_takes_an_empty_object_and_nothing_else(self) -> None:
        self.assertEqual(self.post("/api/pause/resume", {}).status, 200)
        self.assertEqual(self.pause_ops.calls, [("pause_resume", {"args": (), "actor": "web"})])
        self.assertEqual(self.post("/api/pause/resume", {"reason": "x"}).status, 400)

    def test_the_sprint_listing_passes_the_status_filter_through(self) -> None:
        self.assertEqual(self.get("/api/sprints").status, 200)
        self.assertEqual(self.get("/api/sprints", "status=open&status=stopped").status, 200)
        self.assertEqual(
            [call[1]["statuses"] for call in self.sprint_reads.calls], [None, ["open", "stopped"]]
        )

    def test_a_sprint_comment_is_made_as_the_po_from_the_web(self) -> None:
        response = self.post("/api/sprints/sprint:7/comment", {"request_id": "r-1", "body": "hold on"})
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.sprint_ops.calls,
            [
                (
                    "sprint_comment",
                    {
                        "args": (),
                        "request_id": "r-1",
                        "actor": "web",
                        "role": "po",
                        "reference": "sprint:7",
                        "body": "hold on",
                    },
                )
            ],
        )

    def test_a_sprint_close_hands_the_decisions_down_unparsed(self) -> None:
        text = "cards:\n  - {ref: ummanu-9, verdict: drop, reason: not now}\n"
        response = self.post(
            "/api/sprints/sprint:7/close",
            {"request_id": "r-1", "reason": "why", "closeout": "what became", "decisions": text},
        )
        self.assertEqual(response.status, 200)
        ((name, call),) = self.sprint_ops.calls
        self.assertEqual(name, "sprint_close")
        self.assertEqual(call["decisions"], text)
        self.assertEqual((call["actor"], call["role"], call["reference"]), ("web", "po", "sprint:7"))

    def test_a_sprint_close_without_decisions_hands_down_none(self) -> None:
        self.post(
            "/api/sprints/sprint:7/close",
            {"request_id": "r-1", "reason": "why", "closeout": "done", "decisions": ""},
        )
        self.assertIsNone(self.sprint_ops.calls[0][1]["decisions"])
        self.assertEqual(
            self.post(
                "/api/sprints/sprint:7/close",
                {"request_id": "r-1", "reason": "why", "closeout": "d", "decisions": 3},
            ).status,
            400,
        )

    def test_a_close_missing_its_closeout_is_refused_by_name(self) -> None:
        response = self.post("/api/sprints/sprint:7/close", {"request_id": "r-1", "reason": "why"})
        self.assertEqual(response.status, 400)
        self.assertIn("closeout", self.error(response)["message"])
        self.assertEqual(self.sprint_ops.calls, [])

    def test_the_history_routes_pass_cursor_and_limit(self) -> None:
        self.assertEqual(self.get("/api/history", "cursor=c-1&limit=5").status, 200)
        self.assertEqual(self.command_reads.calls, [("command_history", {"args": ("c-1",), "limit": 5})])
        self.assertEqual(self.get("/api/history/r-9").status, 200)
        self.assertEqual(self.command_reads.calls[-1], ("command_request", {"args": ("r-9",)}))

    def test_a_card_comment_and_a_move_are_made_as_the_po_from_the_web(self) -> None:
        self.assertEqual(
            self.post("/api/tasks/ummanu-9/comment", {"request_id": "r-1", "body": "hi"}).status, 200
        )
        self.assertEqual(
            self.card_ops.calls[-1],
            (
                "task_comment",
                {
                    "args": (),
                    "request_id": "r-1",
                    "actor": "web",
                    "role": "po",
                    "reference": "ummanu-9",
                    "body": "hi",
                },
            ),
        )
        response = self.post(
            "/api/tasks/ummanu-9/move",
            {
                "request_id": "r-2",
                "target": "ready",
                "reason": "reslice",
                "sprint_override": True,
                "sprint_override_reason": "owner",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.card_ops.calls[-1],
            (
                "task_move",
                {
                    "args": (),
                    "request_id": "r-2",
                    "actor": "web",
                    "role": "po",
                    "reference": "ummanu-9",
                    "target": "ready",
                    "reason": "reslice",
                    "sprint_override": True,
                    "sprint_override_reason": "owner",
                },
            ),
        )

    def test_a_move_without_the_override_hands_down_false(self) -> None:
        self.post("/api/tasks/ummanu-9/move", {"request_id": "r-2", "target": "ready", "reason": "x"})
        self.assertIs(self.card_ops.calls[-1][1]["sprint_override"], False)
        self.assertEqual(self.card_ops.calls[-1][1]["sprint_override_reason"], "")

    def test_a_move_with_a_non_boolean_override_is_refused(self) -> None:
        response = self.post(
            "/api/tasks/ummanu-9/move",
            {"request_id": "r-2", "target": "ready", "reason": "x", "sprint_override": "maybe"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.card_ops.calls, [])

    def test_a_refusal_of_a_new_route_keeps_the_layer_code(self) -> None:
        self.pause_ops = Recording(pause_drain=InstallationUnavailable("no instance"))
        response = self.post("/api/pause/drain", {"reason": "x"})
        self.assertEqual(response.status, 503)
        self.assertEqual(self.error(response)["code"], "backend_unavailable")


# -- criterion 2: the dashboard draws each read, and one refusing marks its own section -----------


class DashboardPageTests(FakeAppFixture):
    def test_the_dashboard_is_compact_and_draws_active_sprints(self) -> None:
        page = self.text(self.get("/"))
        self.assertIn("running — the dispatcher claims cards and raises heads", page)
        self.assertIn('data-action="/api/pause/drain"', page)
        self.assertIn("sprint:7", page)
        # A waiting state without a scoped owner event is neutral (sprint:1471 comment 8).
        self.assertNotIn("attention required", page)
        # Usage windows are the bottom bar's alone: the dashboard draws no second copy of them.
        self.assertNotIn("Usage limits", page)
        # The chat placeholder became the PO indicator (secretary-1631); an app built without the PO
        # layers draws no PO panel at all.
        self.assertNotIn("Chat — coming later", page)
        self.assertNotIn('href="/po"', page.split("<main>", 1)[1])
        self.assertNotIn("Recent commands", page)
        self.assertNotIn("Start a run by hand", page)
        self.assertEqual([name for name, _ in self.sprint_reads.calls], ["sprint_list"])
        self.assertEqual(self.sprint_reads.calls[0][1]["statuses"], ["open"])
        self.assertEqual(self.command_reads.calls, [])

    def test_a_paused_pipeline_offers_resume_and_says_since_when_and_why(self) -> None:
        self.pause_reads = Recording(pause_state=pause_document(paused=True, mode="drain"))
        page = self.text(self.get("/"))
        self.assertIn("drained — no new card is claimed", page)
        self.assertIn('data-action="/api/pause/resume"', page)
        self.assertNotIn('data-action="/api/pause/drain"', page)
        self.assertIn("2026-09-13T11:00:00Z", page)
        self.assertIn("a release is being cut", page)

    def test_a_side_read_that_refuses_marks_its_section_and_the_page_stands(self) -> None:
        self.pause_reads = Recording(pause_state=InstallationUnavailable("the pause flag is unreadable"))
        self.sprint_reads = Recording(sprint_list=ReadError("the sprint registry refused"))
        response = self.get("/")
        page = self.text(response)
        self.assertEqual(response.status, 200)
        self.assertIn("could not read whether the pipeline is paused", page)
        self.assertIn("the pause flag is unreadable", page)
        self.assertIn("could not read the open sprints", page)
        self.assertIn("the sprint registry refused", page)

    def test_the_snapshot_refusing_is_the_page_refusing(self) -> None:
        self.reads = Recording(system_snapshot=InstallationUnavailable("no installation"))
        response = self.get("/")
        self.assertEqual(response.status, 503)
        self.assertIn("no installation", self.text(response))

    def test_no_open_sprint_is_said_in_words(self) -> None:
        self.sprint_reads = Recording(sprint_list=sprint_listing([]))
        self.assertIn("no sprint is open.", self.text(self.get("/")))

    def test_health_problems_are_listed_by_name(self) -> None:
        status = health_summary(
            {
                "host": {
                    "units": [
                        {
                            "name": "ummanu-web.service",
                            "kind": "service",
                            "present": True,
                            "active": "failed",
                        }
                    ]
                },
                "dispatcher": {"pause": {"paused": True, "mode": "freeze"}},
            }
        )
        self.reads.answers["system_snapshot"]["installation"]["health"]["status"] = status
        page = self.text(self.get("/"))
        self.assertIn("attention", page)
        self.assertIn("ummanu-web.service is failed", page)
        self.assertIn("the pipeline is paused (freeze)", page)

    def test_the_history_page_links_to_the_older_page_by_cursor(self) -> None:
        self.command_reads = Recording(command_history=history([command("routing", "ummanu-9")], more=True))
        page = self.text(self.get("/history"))
        self.assertIn("routing", page)
        self.assertIn('href="/history?cursor=c-2&amp;limit=25"', page)

    def test_the_sprints_page_lists_every_sprint_and_marks_the_filter(self) -> None:
        page = self.text(self.get("/sprints"))
        self.assertIn("sprint:7", page)
        self.assertIn('href="/sprints" aria-current="true"', page)
        self.assertIn('href="/sprints/sprint%3A7"', page)
        self.assertEqual(self.sprint_reads.calls[-1][1]["statuses"], ["open"])
        self.assertIn('aria-current="page"', self.text(self.get("/sprints")))

    def test_the_sprints_page_says_where_the_current_card_stands_and_since_when(self) -> None:
        """The duration in the text, the exact ISO moment as the title -- `_reset`'s convention."""
        page = self.text(self.get("/sprints"))
        self.assertIn('title="2026-09-13T09:00:00Z"', page)
        self.assertIn("3h in this state", page)
        self.assertIn(">in progress<", page)

    def test_a_card_the_journal_dates_no_transition_for_shows_no_age(self) -> None:
        """Criterion 6 on the page: words, and never a duration that reads as "just now"."""
        item = sprint_item()
        item["current_card_state"] = {
            **item["current_card_state"],
            "since": None,
            "age_seconds": None,
            "transition": "absent",
            "reason": "the committed audit records no state transition of ummanu-9",
        }
        self.sprint_reads = Recording(sprint_list=sprint_listing([item]))
        page = self.text(self.get("/sprints"))
        self.assertIn("no transition recorded", page)
        self.assertNotIn("in this state", page)
        self.assertNotIn("0s", page)

    def test_a_sprint_that_ended_shows_no_ticking_age_for_its_card(self) -> None:
        item = sprint_item()
        item["status"] = "closed"
        item["current_card_state"] = {
            **item["current_card_state"],
            "since": None,
            "age_seconds": None,
            "transition": "not_applicable",
            "reason": "sprint:7 is closed: ummanu-9 is the card it ended on",
        }
        self.sprint_reads = Recording(sprint_list=sprint_listing([item]))
        page = self.text(self.get("/sprints"))
        self.assertIn("the card it ended on", page)
        self.assertNotIn("in this state", page)

    def test_a_source_that_refused_leaves_the_card_on_the_row(self) -> None:
        """Criterion 2 on the page: the reference stands, only the new part says it is unknown."""
        item = sprint_item()
        item["current_card_state"] = {
            "card": "ummanu-9",
            "state": None,
            "since": None,
            "age_seconds": None,
            "transition": "unknown",
            "reason": "the committed audit journal could not be read: denied",
            "source": {**available(), "state": "unavailable", "reason": "denied"},
        }
        self.sprint_reads = Recording(sprint_list=sprint_listing([item]))
        page = self.text(self.get("/sprints"))
        self.assertIn("ummanu-9", page)
        self.assertIn("the committed audit journal could not be read", page)
        self.assertNotIn("in this state", page)

    def test_archive_requests_terminal_sprints_and_offers_search(self) -> None:
        second = sprint_item("sprint:8")
        second["reservations"] = ["orca"]
        self.sprint_reads = Recording(sprint_list=sprint_listing([sprint_item(), second]))
        page = self.text(self.get("/sprints", "view=archive&q=goal&project=ummanu"))
        self.assertEqual(self.sprint_reads.calls[-1][1]["statuses"], ["closed", "stopped"])
        self.assertIn('name="q" value="goal"', page)
        self.assertIn('<option value="ummanu" selected>', page)
        self.assertIn('<option value="orca">', page)
        self.assertIn("sprint:7", page)
        self.assertNotIn("sprint:8</a>", page)

    def test_unknown_sprint_view_is_active_consistently(self) -> None:
        page = self.text(self.get("/sprints", "view=garbage"))
        self.assertEqual(self.sprint_reads.calls[-1][1]["statuses"], ["open"])
        self.assertIn('href="/sprints" aria-current="true"', page)

    def test_projects_have_a_list_and_an_individual_page(self) -> None:
        self.reads.answers["system_snapshot"]["projects"]["items"] = [
            {
                "id": "ummanu",
                "repo": "/srv/ummanu",
                "adapter": "ummanu",
                "default_branch": "main",
                "enabled": True,
            }
        ]
        listing = self.text(self.get("/projects"))
        self.assertIn('href="/projects/ummanu"', listing)
        detail = self.text(self.get("/projects/ummanu"))
        self.assertIn("/srv/ummanu", detail)
        self.assertIn("Sprints", detail)
        self.assertIn("sprint:7", detail)
        self.assertEqual(self.get("/projects/missing").status, 404)

    def test_usage_limits_keep_available_stale_and_unavailable_providers_visible(self) -> None:
        usage = Recording(
            usage_snapshot={
                "kind": "provider_usage",
                "providers": [
                    {
                        "id": "claude",
                        "label": "Claude",
                        "status": "available",
                        "reason": None,
                        "age_seconds": 0,
                        "windows": [{"name": "5-hour", "remaining_percent": 72, "resets_at": "soon"}],
                    },
                    {
                        "id": "codex",
                        "label": "Codex",
                        "status": "stale",
                        "reason": "old observation",
                        "age_seconds": 1800,
                        "windows": [{"name": "weekly", "remaining_percent": 41, "resets_at": "later"}],
                    },
                    {
                        "id": "other",
                        "label": "Other",
                        "status": "unavailable",
                        "reason": "no login",
                        "age_seconds": None,
                        "windows": [],
                    },
                ],
            }
        )
        app = self.app()
        app.provider_usage = usage
        page = self.text(app.handle("GET", "/"))
        for text in ("Claude", "72%", "Codex", "stale", "old observation", "Other", "no login"):
            self.assertIn(text, page)

    def test_every_page_carries_the_primary_navigation(self) -> None:
        for path in ("/", "/sprints", "/projects", "/history", "/sprints/sprint:7"):
            with self.subTest(path=path):
                page = self.text(self.get(path))
                self.assertIn('<nav class="primary"', page)
                self.assertIn('href="/sprints/new"', page)

    def test_the_card_page_draws_each_transition_with_the_records_that_made_it(self) -> None:
        events = [
            {
                "event_id": "e1",
                "kind": "card.started",
                "occurred_at": "2026-09-13T12:00:00Z",
                "actor": {"role": "dispatcher", "id": "d"},
                "reason": "claimed",
                "transition": {"source": "ready", "target": "in_progress"},
                "data": {},
            },
            {
                "event_id": "e2",
                "kind": "card.reported",
                "occurred_at": "2026-09-13T12:30:00Z",
                "actor": {"role": "worker", "id": "w"},
                "reason": "",
                "transition": None,
                "data": {"body": "Done the thing.\n\nDetails follow " * 20, "marker": "report:done"},
            },
            {
                "event_id": "e3",
                "kind": "card.submitted",
                "occurred_at": "2026-09-13T12:31:00Z",
                "actor": {"role": "dispatcher", "id": "d"},
                "reason": "worker report:done",
                "transition": {"source": "in_progress", "target": "validate"},
                "data": {},
            },
            {
                "event_id": "e4",
                "kind": "commented",
                "occurred_at": "2026-09-13T12:32:00Z",
                "actor": {"role": "po", "id": "web"},
                "reason": "",
                "transition": None,
                "data": {},
            },
        ]
        self.reads = Recording(
            task_snapshot={
                "ref": "ummanu-9",
                "observed_at": "2026-09-13T12:40:00Z",
                "card": {
                    "source": available(),
                    "value": {"title": "a card", "state": "validate", "project": "ummanu"},
                },
                "project": {"id": "ummanu", "registered": True},
                "attempt": {},
                "agents": {"source": available(), "items": []},
                "work": {},
                "events": {"source": available(), "items": events, "next_cursor": "c"},
            }
        )
        page = self.text(self.get("/tasks/ummanu-9"))
        self.assertEqual(page.count("<li><details><summary><time"), 2)
        self.assertIn(">12:31:00</time>", page)
        self.assertIn("worker report:done", page)
        self.assertIn("worker report", page)
        self.assertIn("report:done", page)
        # The long body is held to two lines and opened in place, never cut and never printed twice.
        self.assertIn('<details class="text"><summary><span class="prose">Done the thing.', page)
        self.assertNotIn("<pre>Done the thing.", page)
        self.assertIn("Details follow", page)
        # The card page asks for the whole timeline, not the API's short tail.
        self.assertEqual(self.reads.calls[-1][1]["events"], 200)

    def card_page(self, value: dict, agents: list | None = None, heads: list | None = None) -> str:
        self.reads = Recording(
            task_snapshot={
                "ref": "ummanu-9",
                "observed_at": "2026-09-13T12:40:00Z",
                "card": {"source": available(), "value": value},
                "project": {"id": "ummanu", "registered": True},
                "attempt": {},
                "agents": {"source": available(), "items": agents or []},
                "heads": {"source": available(), "items": heads or []},
                "work": {},
                "events": {"source": available(), "items": [], "next_cursor": "c"},
            }
        )
        return self.text(self.get("/tasks/ummanu-9"))

    def test_the_card_page_opens_on_the_whole_task_rendered_from_its_markdown(self) -> None:
        page = self.card_page(
            {
                "title": "Guard escalation on QA routing",
                "state": "in_progress",
                "project": "ummanu",
                "sprint": "sprint:7",
                "description": "# Goal\n\nMake it wait.\n\n# Acceptance criteria\n\n- a `service` test\n- no new loop",
            }
        )
        self.assertIn("<h1>Guard escalation on QA routing</h1>", page)
        self.assertIn('<label for="tab-card-ummanu-9-0">Task</label>', page)
        self.assertIn('id="tab-card-ummanu-9-0" checked', page)
        self.assertIn("<h3>Acceptance criteria</h3>", page)
        self.assertIn("<code>service</code>", page)
        # The card is filed under its sprint in the crumbs.
        self.assertIn('<a href="/sprints/sprint%3A7">sprint:7</a>', page)

    def test_a_card_without_a_description_says_so(self) -> None:
        page = self.card_page({"title": "t", "state": "ready", "project": "ummanu"})
        self.assertIn("this card carries no description beyond its title", page)

    def test_the_heads_panel_names_the_model_and_the_effort(self) -> None:
        """The latest run of a role is the chip; the process behind it gives the pulse; earlier runs are a line each."""
        page = self.card_page(
            {"title": "t", "state": "in_progress", "project": "ummanu"},
            agents=[
                {"role": "worker", "run_id": "run-2", "state": "running", "reason": "a live process matches"}
            ],
            heads=[
                {
                    "role": "worker",
                    "run_id": "run-1",
                    "head": "claude-opus",
                    "current": False,
                    "attempt": 1,
                    "model": "opus",
                    "resolved_model": "claude-sonnet-5",
                    "effort": "medium",
                    "resolved_effort": "medium",
                    "runtime": "local-pty",
                    "local_pty": True,
                    "state": "finished",
                    "reason": "no supervisor holds this run any more",
                },
                {
                    "role": "worker",
                    "run_id": "run-2",
                    "head": "claude-opus",
                    "current": True,
                    "attempt": 2,
                    "model": "opus",
                    "resolved_model": "claude-opus-5-5",
                    "effort": "high",
                    "resolved_effort": "high",
                    "runtime": "local-pty",
                    "local_pty": True,
                    "state": "running",
                    "reason": "a supervisor holds this run",
                },
            ],
        )
        self.assertIn('<span class="model">Opus 5.5</span>', page)
        self.assertIn("<span>high</span>", page)
        self.assertIn('<span class="ref">claude-opus-5-5</span>', page)
        self.assertIn('class="pulse live"', page)
        self.assertIn("a live process matches", page)
        self.assertIn('href="/tasks/ummanu-9/heads/run-2"', page)
        # The earlier run is one line under the chip, with its own model and its own link.
        self.assertIn('<ul class="head-runs">', page)
        self.assertIn("Sonnet 5", page)
        self.assertIn('href="/tasks/ummanu-9/heads/run-1"', page)
        self.assertIn('<span class="age">attempt 1</span>', page)
        self.assertEqual(self.get("/tasks/ummanu-9", "events=7").status, 200)
        self.assertEqual(self.reads.calls[-1][1]["events"], 7)

    def test_top_level_pages_carry_no_crumbs_and_nested_pages_only_the_identifier(self) -> None:
        self.assertNotIn('class="crumbs"', self.text(self.get("/history")))
        self.assertNotIn('class="crumbs"', self.text(self.get("/sprints")))
        self.assertNotIn('class="crumbs"', self.text(self.get("/")))
        self.sprint_reads = Recording(
            sprint_state={
                "ref": "sprint:7",
                "sprint": {
                    "source": available(),
                    "value": {"ref": "sprint:7", "status": "open", "goal": "g", "dod": sprint_dod.section(None)},
                },
                "observer": {},
                "work": {},
            }
        )
        page = self.text(self.get("/sprints/sprint:7"))
        self.assertIn('<div class="crumbs"><span class="here">sprint:7</span></div>', page)

    def test_the_sprint_page_carries_the_comment_and_close_forms_on_an_open_sprint(self) -> None:
        self.sprint_reads = Recording(
            sprint_state={
                "ref": "sprint:7",
                "sprint": {
                    "source": available(),
                    "value": {"ref": "sprint:7", "status": "open", "goal": "g", "dod": sprint_dod.section(None)},
                },
                "observer": {},
                "work": {"waiting": {"state": "working", "reason": "busy"}},
            }
        )
        page = self.text(self.get("/sprints/sprint:7"))
        self.assertIn('data-action="/api/sprints/sprint%3A7/comment"', page)
        self.assertIn('data-action="/api/sprints/sprint%3A7/close"', page)
        self.assertIn("busy", page)
        self.sprint_reads.answers["sprint_state"]["sprint"]["value"]["status"] = "closed"
        self.assertNotIn("/close", self.text(self.get("/sprints/sprint:7")))


# -- criterion 3: the card writes really write, under the writer's own rules ---------------------


class CardOperationTests(SprintProtocolFixture):
    #: The card writes land in a real card store.

    def layer(self) -> CardOperationLayer:
        return CardOperationLayer(
            self.instance, data_dir=self.data_dir, board_client=self.board, clock=lambda: self.clock
        )

    def writer(self) -> TaskWriter:
        return TaskWriter(self.board, data_dir=self.data_dir)

    def card(self) -> tuple[str, str]:
        """The card the board is seeded with, and the state it is in: `secretary-12`, in Ready."""
        return "ummanu-12", self.state_of("ummanu-12")

    def state_of(self, ref: str) -> str:
        return str(self.writer().reader.show(ref)["state"])

    def test_a_comment_lands_on_the_card_as_the_po(self) -> None:
        ref, _state = self.card()
        answer = self.layer().task_comment(request_id="r-1", actor="web", reference=ref, body="hold on")
        self.assertEqual(answer["kind"], "card_commented")
        self.assertEqual(answer["ref"], ref)
        self.assertEqual(validate(answer, "web-card", answer["kind"]), [])
        comments = [str(comment.get("comment")) for comment in self.board.comments(self.board.key_of(ref))]
        self.assertTrue(any("hold on" in comment for comment in comments), comments)

    def test_a_comment_on_no_card_is_not_found(self) -> None:
        with self.assertRaises(ReadError) as caught:
            self.layer().task_comment(request_id="r-1", actor="web", reference="ummanu-none", body="x")
        self.assertEqual(caught.exception.code, "not_found")

    def test_an_empty_comment_and_a_missing_request_id_are_refused_before_the_board(self) -> None:
        ref, _state = self.card()
        for kwargs in ({"request_id": "", "body": "x"}, {"request_id": "r", "body": "  "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ReadError) as caught:
                self.layer().task_comment(actor="web", reference=ref, **kwargs)
            self.assertEqual(caught.exception.code, "validation")

    def test_a_move_to_a_state_that_is_not_one_is_refused_naming_the_choices(self) -> None:
        ref, _state = self.card()
        with self.assertRaises(ReadError) as caught:
            self.layer().task_move(request_id="r-1", actor="web", reference=ref, target="Done", reason="x")
        self.assertEqual(caught.exception.code, "validation")
        for target in MOVE_TARGETS:
            self.assertIn(target, caught.exception.message)

    def test_an_override_without_its_own_reason_is_refused(self) -> None:
        ref, _state = self.card()
        with self.assertRaises(ReadError) as caught:
            self.layer().task_move(
                request_id="r-1",
                actor="web",
                reference=ref,
                target="blocked",
                reason="x",
                sprint_override=True,
            )
        self.assertEqual(caught.exception.code, "validation")

    def test_a_move_the_writer_permits_moves_the_card(self) -> None:
        ref, state = self.card()
        self.assertNotEqual(state, "blocked")
        answer = self.layer().task_move(
            request_id="r-1", actor="web", reference=ref, target="blocked", reason="hold"
        )
        self.assertEqual(answer["kind"], "card_moved")
        self.assertEqual(self.state_of(ref), "blocked")
        self.assertEqual(validate(answer, "web-card", answer["kind"]), [])

    def test_a_move_the_writer_forbids_is_an_owner_conflict(self) -> None:
        """A PO may move a card between any two states; to the state it is in is not a move."""
        ref, state = self.card()
        with self.assertRaises(ReadError) as caught:
            self.layer().task_move(request_id="r-1", actor="web", reference=ref, target=state, reason="x")
        self.assertEqual(caught.exception.code, "owner_conflict")
        self.assertEqual(self.state_of(ref), state)


# -- the health summary and the cheaper status read ----------------------------------------------


class HealthSummaryTests(unittest.TestCase):
    def test_nothing_wrong_is_ok_with_no_problems(self) -> None:
        summary = health_summary(
            {"host": {"units": [{"name": "a.timer", "kind": "timer", "present": True, "active": "active"}]}}
        )
        self.assertEqual((summary["state"], summary["problems"]), ("ok", []))

    def test_each_failure_the_collector_marks_is_a_problem_by_name(self) -> None:
        summary = health_summary(
            {
                "host": {
                    "units": [{"name": "x.service", "kind": "service", "present": False, "active": None}],
                    # A status document from before A20 step 9 (secretary-1726) still names the
                    # host-owned Orca server; the summary no longer reports it as a problem.
                    "external_runtime": {"name": "orca-server.service", "active": "inactive"},
                    "inventory_errors": {"units": "systemctl timed out"},
                },
                "dispatcher": {"pause": {"paused": True, "mode": "drain"}, "divergences": {"open_count": 2}},
                "checkpoint": {
                    "checkpoint_status": "failed",
                    "checkpoint_last_failure_reason": "push refused",
                    "blocked_reason": "no remote",
                },
                "secret_store": {"installation_key": {"present": True, "usable": False}},
                "memory": {"index_present": False},
            }
        )
        self.assertEqual(summary["state"], "attention")
        self.assertEqual(
            summary["problems"],
            [
                "x.service is not installed on this host",
                "the host inventory could not read units: systemctl timed out",
                "the pipeline is paused (drain)",
                "2 dispatcher divergence(s) are open",
                "the checkpoint is blocked: no remote",
                "the last checkpoint failed: push refused",
                "the secret store's installation key is not usable",
                "the memory index is missing",
            ],
        )
        self.assertTrue(summary["dispatcher"]["paused"])

    def test_an_inactive_oneshot_service_is_not_a_problem(self) -> None:
        summary = health_summary(
            {
                "host": {
                    "units": [
                        {
                            "name": "ummanu-curator.service",
                            "kind": "service",
                            "present": True,
                            "active": "inactive",
                        }
                    ]
                }
            }
        )
        self.assertEqual(summary["problems"], [])


class StatusWithoutSprintsTests(SprintProtocolFixture):
    def test_the_web_reads_status_without_the_sprints_or_the_panel_probes(self) -> None:
        from ummanu.config import validate_instance

        report = validate_instance(self.instance)
        snapshot = collect_status(
            report, offline=True, sprint_client=self.board, sprints=False, probe_panels=False
        )
        self.assertEqual(snapshot["installation"]["sprints"]["items"], [])
        self.assertIn("skipped", snapshot["installation"]["sprints"])
        whole = collect_status(report, offline=True, sprint_client=self.board)
        self.assertNotIn("skipped", whole["installation"]["sprints"])

    def test_the_snapshot_carries_the_summary_and_not_the_whole_status(self) -> None:
        layer = ReadLayer(
            self.instance,
            data_dir=self.data_dir,
            board_client=self.board,
            status_reader=lambda: {"host": {"units": []}, "dispatcher": {"active_attempts": [1, 2]}},
        )
        health = layer.system_snapshot()["installation"]["health"]
        self.assertEqual(health["source"]["state"], "available")
        self.assertEqual(health["status"]["state"], "ok")
        self.assertEqual(health["status"]["dispatcher"]["active_attempts"], 2)
        self.assertNotIn("host", health["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
