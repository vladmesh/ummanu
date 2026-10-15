"""The shared bottom bar: every page carries it, it costs no provider read, and it never invents a number.

Hermetic. Every layer is a recording fake and the one real object is
:class:`~ummanu.web.provider_usage.ProviderUsageLayer`, driven over a temporary home with a
counting `fetch_json` and a clock this test moves — which is the only way to ask the question
criterion 4 asks: does *rendering* cost a provider read. It does not; the cache decides.

The route list is never written out here. :data:`ummanu.web.app.ROUTES` is walked, so a page
route added tomorrow enters these assertions with it and a shell that stopped rendering the bar
fails on every one of them at once.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.web_fakes import Recording, system_snapshot
from ummanu.web import page_assets, page_parts, page_shell, pages
from ummanu.web.app import ROUTES, WebApp
from ummanu.web.provider_usage import (
    CACHE_SECONDS,
    CLAUDE_USAGE_URL,
    CODEX_RESET_CREDITS_URL,
    CODEX_USAGE_URL,
    ProviderUsageLayer,
)
from ummanu.webproto.errors import InstallationUnavailable, ValidationRefused

#: What a path placeholder is filled with when this test drives the route it belongs to. A route
#: added with a placeholder nobody listed here fails :meth:`RouteFixture.concrete` rather than being
#: quietly skipped: an untested page route is exactly what this suite exists to prevent.
PLACEHOLDERS = {"ref": "ummanu-9", "project": "ummanu", "session": "s-1", "run_id": "r-1"}

NOW = 1_800_000_000.0

#: The moment every countdown in this suite is measured from. It is the render clock, not any
#: reading's `observed_at`, which is the whole of criterion 4.
RENDERED_AT = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

#: Where a percentage would be, if there were one: what a stale or unavailable provider must not show.
PERCENTAGE = re.compile(r"\d+(?:\.\d+)?%")


def available() -> dict[str, Any]:
    return {
        "state": "available",
        "reason": None,
        "data_age_seconds": 0.0,
        "observed_at": "2026-09-13T12:00:00Z",
    }


def snapshot_with_a_project() -> dict[str, Any]:
    document = system_snapshot()
    document["projects"]["items"] = [
        {
            "id": "ummanu",
            "repo": "/srv/ummanu",
            "adapter": "ummanu",
            "default_branch": "main",
            "enabled": True,
        }
    ]
    return document


def usage_document(providers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "provider_usage", "observed_at": "2026-09-20T12:00:00Z", "providers": providers}


def provider(
    provider_id: str,
    label: str,
    *,
    status: str = "available",
    reason: str | None = None,
    age_seconds: float | None = 0.0,
    windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": provider_id,
        "label": label,
        "status": status,
        "reason": reason,
        "observed_at": "2026-09-20T12:00:00Z",
        "age_seconds": age_seconds,
        "windows": windows or [],
    }


def window(name: str, remaining: float, resets_at: str) -> dict[str, Any]:
    return {"name": name, "window_minutes": 300, "remaining_percent": remaining, "resets_at": resets_at}


def bar_of(page: str) -> str:
    """The bar out of a rendered page, and the proof there is exactly one."""
    found = re.findall(r'<footer class="statusbar".*?</footer>', page, re.DOTALL)
    if len(found) != 1:
        raise AssertionError(f"a page carries one status bar, this one carries {len(found)}")
    return found[0]


def provider_places(bar: str) -> list[str]:
    """Each provider's place on the bar, as its own markup."""
    return [f'<span class="provider">{part}' for part in bar.split('<span class="provider">')[1:]]


class RouteFixture(unittest.TestCase):
    """The application over recording fakes for every layer, including the PO half."""

    def setUp(self) -> None:
        self.enterContext(pages.render_clock(lambda: RENDERED_AT))
        unreadable = InstallationUnavailable("not part of this test")
        self.reads = Recording(
            system_snapshot=snapshot_with_a_project(),
            task_snapshot={
                "ref": "ummanu-9",
                "observed_at": "2026-09-20T12:00:00Z",
                "card": {
                    "source": available(),
                    "value": {"title": "a card", "state": "validate", "project": "ummanu"},
                },
                "project": {"id": "ummanu", "registered": True},
                "attempt": {},
                "agents": {"source": available(), "items": []},
                "work": {},
                "events": {"source": available(), "items": [], "next_cursor": "c"},
            },
        )
        self.sprint_reads = Recording(
            sprint_list={"kind": "sprint_list", "sprints": {"source": available(), "items": []}},
            sprint_state={
                "ref": "ummanu-9",
                "sprint": {
                    "source": available(),
                    "value": {"ref": "sprint:7", "status": "open", "goal": "a goal"},
                },
                "observer": {},
                "work": {},
            },
        )
        self.command_reads = Recording(
            command_history={
                "kind": "command_history",
                "observed_at": "2026-09-20T12:00:00Z",
                "limit": 25,
                "commands": {
                    "source": available(),
                    "items": [],
                    "has_more": False,
                    "next_cursor": None,
                },
            }
        )
        self.po = Recording(
            po_running_count={"kind": "po_running", "running": 0},
            po_overview={"kind": "po_overview", "sessions": [], "models": {}, "running": 0},
            po_session={
                "kind": "po_session",
                "session": {"session_id": "s-1", "state": "idle", "cli": "claude", "model": "opus"},
                "turns": [],
                "feed": [],
                "running": False,
            },
        )
        self.owner_events = Recording(
            owner_event_list={
                "kind": "owner_events",
                "observed_at": "2026-09-20T12:00:00Z",
                "source": {"state": "available", "reason": None},
                "unread_only": False,
                "unread": 0,
                "events": [],
            },
            unread_count={"state": "available", "reason": None, "count": 0},
        )
        # The pause refuses, so the dashboard draws that section as a marked block: this suite is
        # about the bar, and a page that also has an unreadable section is the harder case for it.
        self.layers = [
            self.reads,
            Recording(card_runs={"items": []}),
            self.sprint_reads,
            Recording(),
            Recording(pause_state=unreadable),
            Recording(),
            self.command_reads,
            Recording(),
        ]
        self.doctor = Recording(
            doctor_snapshot={
                "kind": "doctor",
                "observed_at": "2026-09-20T12:00:00Z",
                "readable": True,
                "reason": None,
                "colour": "green",
                "problems": [],
                "source": None,
            }
        )
        self.usage = Recording(
            usage_snapshot=usage_document(
                [
                    provider(
                        "claude",
                        "Claude",
                        windows=[window("5-hour", 74.0, "2026-09-20T18:00:00Z")],
                    ),
                    provider("codex", "Codex", windows=[window("weekly", 91.0, "2026-09-25T00:00:00Z")]),
                ]
            )
        )

    def app(self, *, provider_usage: Any = ..., po: Any = ..., doctor: Any = ...) -> WebApp:
        return WebApp(
            *self.layers,
            provider_usage=self.usage if provider_usage is ... else provider_usage,
            doctor=self.doctor if doctor is ... else doctor,
            po=self.po if po is ... else po,
            owner_events=self.owner_events,
        )

    def page_routes(self) -> list[Any]:
        """Every route that answers a person with a page on a GET, out of the table itself."""
        return [route for route in ROUTES if route.page and route.method == "GET"]

    def concrete(self, pattern: str) -> str:
        def fill(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in PLACEHOLDERS:
                raise AssertionError(
                    f"the route {pattern} has a placeholder {{{name}}} this suite does not know how "
                    "to drive; add it to PLACEHOLDERS so its page is walked like every other"
                )
            return PLACEHOLDERS[name]

        return re.sub(r"\{([a-z_]+)\}", fill, pattern)

    def get(self, path: str, *, app: WebApp | None = None) -> str:
        response = (app or self.app()).handle("GET", path)
        self.assertEqual(response.status, 200, f"{path} answered {response.status}")
        return response.body.decode("utf-8")


# -- criterion 1: the bar is the shell's, so every page route has it -----------------------------


class EveryPageCarriesTheBarTests(RouteFixture):
    def test_the_walked_table_really_covers_the_pages_this_card_names(self) -> None:
        """The walk is the assertion; this pins that the walk is not empty or narrowed."""
        walked = {self.concrete(route.pattern) for route in self.page_routes()}
        self.assertEqual(
            walked,
            {
                "/",
                "/tasks/ummanu-9",
                "/tasks/ummanu-9/heads/r-1",
                "/sprints",
                "/sprints/new",
                "/sprints/ummanu-9",
                "/sprints/ummanu-9/heads/r-1",
                "/projects",
                "/projects/ummanu",
                "/history",
                "/doctor",
                "/po",
                "/po/sessions/s-1",
                "/owner-events",
            },
        )

    def test_every_get_page_route_renders_one_status_bar_with_both_providers(self) -> None:
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                bar = bar_of(self.get(path))
                self.assertIn("<b>Claude</b>", bar)
                self.assertIn("<b>Codex</b>", bar)
                self.assertIn("74%", bar)
                self.assertIn("91%", bar)

    def test_every_page_carries_one_doctor_lamp_and_it_links_to_the_doctor_page(self) -> None:
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                bar = bar_of(self.get(path))
                lamps = re.findall(r'<a class="lamp lamp-(\w+)" href="(/doctor)"', bar)
                self.assertEqual(lamps, [("green", "/doctor")], bar)

    def test_a_refusal_page_carries_the_bar_too(self) -> None:
        response = self.app().handle("GET", "/projects/absent")
        self.assertEqual(response.status, 404)
        self.assertIn("<b>Claude</b>", bar_of(response.body.decode("utf-8")))

    def test_a_json_route_carries_no_bar_and_costs_no_provider_read(self) -> None:
        response = self.app().handle("GET", "/api/system")
        self.assertNotIn("statusbar", response.body.decode("utf-8"))
        self.assertEqual(self.usage.calls, [])


# -- criterion 2: it is fixed to the bottom and the space under the page is reserved -------------


class BarLayoutTests(RouteFixture):
    def test_the_bar_is_the_only_fixed_element_and_the_page_reserves_its_height(self) -> None:
        fixed = set(re.findall(r"^(\S+) \{[^}]*position: fixed", page_assets.STYLE, re.MULTILINE))
        self.assertEqual(fixed, {".statusbar"}, "only the bar is lifted out of the page's flow")
        self.assertIn(".statusbar { position: fixed; left: 0; right: 0; bottom: 0;", page_assets.STYLE)
        self.assertIn("--bar-height:", page_assets.STYLE)
        self.assertIn("body { padding-bottom: var(--bar-height); }", page_assets.STYLE)
        self.assertIn("height: var(--bar-height)", page_assets.STYLE)

    def test_the_bar_keeps_one_line_at_any_width_so_the_reserved_height_stays_true(self) -> None:
        row = re.search(r"\.statusbar \.row \{([^}]*)\}", page_assets.STYLE)
        assert row is not None
        self.assertIn("white-space: nowrap", row.group(1))
        self.assertIn("overflow-x: auto", row.group(1))
        # The phone-width rule narrows the gutters; it must not reintroduce wrapping or a height.
        phone = re.search(r"@media \(max-width: 600px\) \{ \.statusbar[^}]*\}", page_assets.STYLE)
        assert phone is not None
        self.assertNotIn("white-space", phone.group(0))

    def test_the_po_composer_sits_above_the_bar_rather_than_under_it(self) -> None:
        page = self.get("/po/sessions/s-1")
        self.assertIn('<textarea id="po-text" name="text">', page)
        # The composer is inside <main>, which the reserved padding sits below; the bar follows it.
        self.assertLess(page.index('id="po-text"'), page.index("</main>"))
        self.assertLess(page.index("</main>"), page.index('<footer class="statusbar"'))
        self.assertNotIn("position: fixed", re.search(r"form\.sprint \{[^}]*\}", page_assets.STYLE).group(0))

    def test_a_provider_is_a_group_the_eye_can_find_on_a_single_line(self) -> None:
        """The bar is one line, so the grouping is drawn rather than spelled with newlines."""
        heading = re.search(r"\.statusbar \.provider > b \{([^}]*)\}", page_assets.STYLE)
        assert heading is not None
        # The heading is set apart from the windows the way a panel heading is: see `h2`.
        self.assertIn("text-transform: uppercase", heading.group(1))
        self.assertIn("letter-spacing", heading.group(1))
        # One provider is separated from the next by a rule and not only by a gap.
        rule = re.search(r"\.statusbar \.provider \+ \.provider \{([^}]*)\}", page_assets.STYLE)
        assert rule is not None
        self.assertIn("border-left: 1px solid", rule.group(1))

    def test_a_window_is_one_chip_and_its_figure_sits_next_to_its_name(self) -> None:
        chip = re.search(r"\.statusbar \.window \{([^}]*)\}", page_assets.STYLE)
        assert chip is not None
        self.assertIn("background: var(--raised)", chip.group(1))
        figure = re.search(r"\.statusbar \.window > b \{([^}]*)\}", page_assets.STYLE)
        assert figure is not None
        # No fixed column: it would open a hole between the window's name and its percentage.
        self.assertNotIn("min-width", figure.group(1))
        self.assertIn("font-variant-numeric: tabular-nums", figure.group(1))


# -- criterion 3 and 5: a reading, a reading that is not current, and no reading at all ----------


class BarReadingTests(RouteFixture):
    def bar(self, section: dict[str, Any] | None) -> str:
        return bar_of(f"<main></main>{page_shell._limits_bar_of(section)}")

    def section(self, providers: list[dict[str, Any]]) -> dict[str, Any]:
        return {"available": True, "reason": None, "document": usage_document(providers)}

    def test_an_available_provider_shows_every_window_its_reading_carries(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider(
                        "claude",
                        "Claude",
                        windows=[
                            window("5-hour", 74.0, "2026-09-20T18:00:00Z"),
                            window("weekly", 91.0, "2026-09-25T00:00:00Z"),
                        ],
                    ),
                    provider("codex", "Codex", windows=[window("5-hour", 41.0, "2026-09-20T15:00:00Z")]),
                ]
            )
        )
        claude, codex = provider_places(bar)[:2]
        self.assertIn("5-hour", claude)
        self.assertIn("74%", claude)
        # The reset is the time left until it, and the moment itself is the hover title.
        self.assertIn('<span class="resets" title="2026-09-20T18:00:00Z">6h 0m left</span>', claude)
        self.assertIn("weekly", claude)
        self.assertIn("91%", claude)
        self.assertIn('<span class="resets" title="2026-09-25T00:00:00Z">4d 12h left</span>', claude)
        self.assertIn("41%", codex)

    def test_a_window_is_drawn_as_one_group_of_name_percentage_and_countdown(self) -> None:
        """What belongs to one window is inside one element, in that order and with one separator."""
        bar = self.bar(
            self.section(
                [provider("claude", "Claude", windows=[window("5-hour", 73.0, "2026-09-20T13:06:00Z")])]
            )
        )
        self.assertIn(
            '<span class="window"><span class="win-name">5-hour</span><b>73%</b>'
            '<span class="dot">·</span>'
            '<span class="resets" title="2026-09-20T13:06:00Z">1h 6m left</span></span>',
            bar,
        )

    def test_an_available_reading_that_is_not_new_says_how_old_it_is(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider("claude", "Claude", age_seconds=1800.0, windows=[window("5-hour", 74.0, "x")]),
                    provider("codex", "Codex", windows=[window("5-hour", 41.0, "x")]),
                ]
            )
        )
        claude, codex = provider_places(bar)[:2]
        self.assertIn("30m old", claude)
        self.assertNotIn("old", codex)

    def test_a_stale_provider_shows_its_status_and_reason_and_no_percentage(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider(
                        "claude",
                        "Claude",
                        status="stale",
                        reason="Latest Codex usage observation is stale",
                        age_seconds=3600.0,
                        # A stale reading still carries the last windows. They are not drawn: a
                        # percentage on the bar is read as what is left now, and this one is not.
                        windows=[window("5-hour", 12.0, "x")],
                    ),
                    provider("codex", "Codex", windows=[window("5-hour", 41.0, "x")]),
                ]
            )
        )
        claude = provider_places(bar)[0]
        self.assertIn("stale", claude)
        self.assertIn("Latest Codex usage observation is stale", claude)
        self.assertIn("60m old", claude)
        self.assertIn("no current reading", claude)
        self.assertIsNone(PERCENTAGE.search(claude), claude)
        self.assertNotIn("12.0", claude)

    def test_an_unavailable_provider_shows_its_status_and_reason_and_no_percentage(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider("claude", "Claude", windows=[window("5-hour", 74.0, "x")]),
                    provider(
                        "codex",
                        "Codex",
                        status="unavailable",
                        reason="Codex login is unavailable",
                        age_seconds=None,
                    ),
                ]
            )
        )
        codex = provider_places(bar)[1]
        self.assertIn("unavailable", codex)
        self.assertIn("Codex login is unavailable", codex)
        self.assertIn("no current reading", codex)
        self.assertIsNone(PERCENTAGE.search(codex), codex)

    def test_a_provider_the_reading_does_not_carry_keeps_its_place_and_says_so(self) -> None:
        bar = self.bar(self.section([provider("claude", "Claude", windows=[window("5-hour", 74.0, "x")])]))
        codex = provider_places(bar)[1]
        self.assertIn("<b>Codex</b>", codex)
        self.assertIn(page_shell.LIMITS_NOT_IN_READING, codex)
        self.assertIsNone(PERCENTAGE.search(codex), codex)

    def test_a_provider_beyond_the_two_is_shown_rather_than_hidden(self) -> None:
        bar = self.bar(self.section([provider("other", "Other", status="unavailable", reason="no login")]))
        self.assertEqual(len(provider_places(bar)), 3)
        self.assertIn("<b>Other</b>", provider_places(bar)[2])

    def test_a_process_built_without_the_layer_still_serves_every_page(self) -> None:
        app = self.app(provider_usage=None, po=self.po)
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                bar = bar_of(self.get(path, app=app))
                self.assertIn(page_shell.LIMITS_NOT_BUILT, bar)
                self.assertIsNone(PERCENTAGE.search(bar), bar)

    def test_a_provider_read_that_refuses_still_serves_every_page_with_the_reason(self) -> None:
        app = self.app(provider_usage=Recording(usage_snapshot=InstallationUnavailable("no instance here")))
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                bar = bar_of(self.get(path, app=app))
                self.assertIn("no instance here", bar)
                self.assertIsNone(PERCENTAGE.search(bar), bar)


# -- criterion 4: rendering pages adds no provider call beyond the cache's own cadence -----------


class BarCostsNoExtraReadTests(RouteFixture):
    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.write(self.home / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "c"}})
        self.write(self.home / ".codex/auth.json", {"tokens": {"access_token": "x", "account_id": "a"}})
        self.clock = NOW
        self.fetched: list[str] = []
        self.layer = ProviderUsageLayer(
            home=self.home, fetch_json=self.fetch, now=lambda: self.clock, timeout=3.0
        )

    def write(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        self.fetched.append(url)
        if url == CLAUDE_USAGE_URL:
            return {
                "five_hour": {"utilization": 26.0, "resets_at": self.clock + 100},
                "seven_day": {"utilization": 9.0, "resets_at": self.clock + 200},
            }
        return {
            "rate_limit": {
                "primary_window": {"used_percent": 59, "window_minutes": 300, "reset_at": self.clock + 300}
            }
        }

    def test_many_renders_inside_one_cache_window_ask_each_provider_once(self) -> None:
        app = self.app(provider_usage=self.layer)
        paths = [self.concrete(route.pattern) for route in self.page_routes()]
        for _ in range(5):
            for path in paths:
                bar = bar_of(self.get(path, app=app))
                self.assertIn("74%", bar)
                self.assertIn("41%", bar)
        self.assertEqual(len(paths) * 5, 70, "the walk really did render many pages")
        self.assertEqual(self.fetched, [CLAUDE_USAGE_URL, CODEX_USAGE_URL])

    def test_the_next_cache_window_asks_once_more_and_not_once_per_page(self) -> None:
        app = self.app(provider_usage=self.layer)
        paths = [self.concrete(route.pattern) for route in self.page_routes()]
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(len(self.fetched), 2)

        self.clock += CACHE_SECONDS - 1
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(len(self.fetched), 2, "still inside the window the layer decided")

        self.clock += 2
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(self.fetched.count(CLAUDE_USAGE_URL), 2)
        self.assertEqual(self.fetched.count(CODEX_USAGE_URL), 2)

    def test_the_dashboard_draws_the_limits_once_from_one_read(self) -> None:
        app = self.app(provider_usage=self.layer)
        page = self.get("/", app=app)
        self.assertEqual(self.fetched, [CLAUDE_USAGE_URL, CODEX_USAGE_URL])
        # The bar is the one place the windows are drawn; the dashboard keeps no panel repeating it.
        self.assertNotIn("Usage limits", page)
        self.assertIn("74%", bar_of(page))
        self.assertEqual(page.count("74%"), 1)


class ResetCreditsCostTests(BarCostsNoExtraReadTests):
    """The same walk over a Codex reading that carries a reset credit: the list is one GET per refresh."""

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        if url == CODEX_RESET_CREDITS_URL:
            self.fetched.append(url)
            return {"credits": [{"status": "available", "expires_at": "2026-10-22T20:24:55Z"}]}
        raw = super().fetch(url, headers, timeout)
        if url == CODEX_USAGE_URL:
            raw["rate_limit_reset_credits"] = {"available_count": 1, "applicable_available_count": 0}
        return raw

    def test_many_renders_inside_one_cache_window_ask_each_provider_once(self) -> None:
        app = self.app(provider_usage=self.layer)
        for route in self.page_routes():
            bar = bar_of(self.get(self.concrete(route.pattern), app=app))
            self.assertIn(">1 reset</summary>", bar)
        self.assertEqual(self.fetched, [CLAUDE_USAGE_URL, CODEX_USAGE_URL, CODEX_RESET_CREDITS_URL])

    def test_the_next_cache_window_asks_once_more_and_not_once_per_page(self) -> None:
        app = self.app(provider_usage=self.layer)
        paths = [self.concrete(route.pattern) for route in self.page_routes()]
        for path in paths:
            self.get(path, app=app)
        self.clock += CACHE_SECONDS - 1
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(len(self.fetched), 3)
        self.clock += 2
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(self.fetched.count(CODEX_USAGE_URL), 2)
        self.assertEqual(self.fetched.count(CODEX_RESET_CREDITS_URL), 2)

    def test_the_dashboard_draws_the_limits_once_from_one_read(self) -> None:
        app = self.app(provider_usage=self.layer)
        page = self.get("/", app=app)
        self.assertEqual(self.fetched, [CLAUDE_USAGE_URL, CODEX_USAGE_URL, CODEX_RESET_CREDITS_URL])
        self.assertEqual(page.count(">1 reset</summary>"), 1, "the credits are drawn once, on the bar")
        claude, codex = provider_places(bar_of(page))[:2]
        self.assertNotIn("credits", claude, "Claude gets no analogue")
        self.assertIn(">1 reset</summary>", codex)


# -- the Codex reset credits: one label after the Codex windows ----------------------------------


class ResetCreditsLabelTests(unittest.TestCase):
    """The folded label :func:`page_shell._bar_reset_credits` draws, in every case the card names."""

    EXPIRES = "2026-10-16T10:00:00Z"  # 25d 22h after RENDERED_AT

    def setUp(self) -> None:
        self.enterContext(pages.render_clock(lambda: RENDERED_AT))

    def codex(self, credits: Any = None, *, carry: bool = True) -> str:
        codex = provider("codex", "Codex", windows=[window("5-hour", 41.0, "2026-09-20T15:00:00Z")])
        if carry:
            codex["reset_credits"] = credits
        section = {"available": True, "reason": None, "document": usage_document([codex])}
        return provider_places(bar_of(page_shell._limits_bar_of(section)))[1]

    def test_the_bar_carries_only_the_count_and_the_details_are_folded(self) -> None:
        place = self.codex({"available": 1, "applicable": 0, "next_expires_at": None})
        self.assertIn('<details class="credits"><summary', place)
        self.assertIn(">1 reset</summary>", place)
        self.assertNotIn("<details open", place, "the details open only on a click")
        self.assertNotIn("usable", place)
        self.assertNotIn("exhausted", place)
        self.assertIn('<span class="pop-line">1 reset banked</span>', place)
        self.assertNotIn("expires", place)

    def test_the_nearest_expiry_is_a_countdown_with_the_moment_as_its_title(self) -> None:
        place = self.codex({"available": 1, "applicable": 0, "next_expires_at": self.EXPIRES})
        self.assertIn(
            f'<span class="pop-line expires" title="{self.EXPIRES}">expires in 25d 22h</span>', place
        )
        summary = place[place.index("<summary") : place.index("</summary>")]
        self.assertNotIn("expires", summary, "the expiry is in the popover, not on the bar")

    def test_the_label_follows_the_windows_in_the_same_group(self) -> None:
        place = self.codex({"available": 1, "applicable": 0, "next_expires_at": self.EXPIRES})
        self.assertLess(place.index('class="window"'), place.index('class="credits"'))
        self.assertEqual(place.count('<span class="provider">'), 1)

    def test_applicability_is_a_line_of_the_popover_in_words(self) -> None:
        place = self.codex({"available": 1, "applicable": 0, "next_expires_at": None})
        self.assertIn(f'<span class="pop-line warn">{page_shell.RESET_NOT_APPLICABLE}</span>', place)
        place = self.codex({"available": 1, "applicable": None, "next_expires_at": None})
        self.assertIn(f'<span class="pop-line warn">{page_shell.RESET_NOT_APPLICABLE}</span>', place)
        place = self.codex({"available": 3, "applicable": 2, "next_expires_at": None})
        self.assertIn(">3 resets</summary>", place)
        self.assertIn(f'<span class="pop-line applies">{page_shell.RESET_APPLICABLE}</span>', place)
        self.assertNotIn("pop-line warn", place)

    def test_no_available_credit_and_no_credits_draw_no_label(self) -> None:
        for credits, carry in (
            ({"available": 0, "applicable": 0, "next_expires_at": None}, True),
            ({"available": 0, "applicable": 0, "next_expires_at": self.EXPIRES}, True),
            (None, False),
            (None, True),
        ):
            with self.subTest(credits=credits, carry=carry):
                place = self.codex(credits, carry=carry)
                self.assertNotIn("credits", place)
                self.assertNotIn(
                    "reset",
                    place.split("resets in")[0] if "resets in" in place else place.replace("resets", ""),
                )

    def test_the_countdown_is_measured_against_the_render_clock(self) -> None:
        with pages.render_clock(lambda: RENDERED_AT + timedelta(days=25, hours=21, minutes=30)):
            place = self.codex({"available": 1, "applicable": 1, "next_expires_at": self.EXPIRES})
        self.assertIn(">expires in 30m<", place)
        with pages.render_clock(lambda: RENDERED_AT + timedelta(days=30)):
            place = self.codex({"available": 1, "applicable": 1, "next_expires_at": self.EXPIRES})
        self.assertIn(">expired<", place)

    def test_hostile_documents_never_raise_and_draw_only_normalised_values(self) -> None:
        cases: list[tuple[Any, str | None]] = [
            ({"available": True, "applicable": 0}, None),
            ({"available": "1", "applicable": 0}, None),
            ({"available": 1e300, "applicable": 0}, None),
            ({"available": 10**400, "applicable": 0}, None),
            ({"available": -1, "applicable": 0}, None),
            ({"available": float("nan")}, None),
            ({"available": 1.7, "applicable": "0"}, "1 reset"),
            ({"available": 1, "applicable": -5}, "1 reset"),
            ({"available": 1, "applicable": 10**400}, "1 reset"),
            ({"available": 1, "next_expires_at": "garbage"}, "1 reset"),
            ({"available": 1, "next_expires_at": 10**400}, "1 reset"),
            ({"available": 1, "next_expires_at": "0001-01-01T00:00:00Z"}, "1 reset"),
            ({"available": 1, "next_expires_at": "9999-12-31T23:59:59Z"}, "1 reset"),
            ({"available": 1, "next_expires_at": ["x"]}, "1 reset"),
            ([], None),
            ("1 reset", None),
            (1, None),
        ]
        for credits, text in cases:
            with self.subTest(credits=credits):
                place = self.codex(credits)
                self.assertIn("41%", place, "the Codex reading stays drawn")
                if text is None:
                    self.assertNotIn('class="credits"', place)
                else:
                    self.assertIn(f">{text}</summary>", place)


# -- criterion 6: what keeps the bar current never discards what somebody is typing --------------


class BarRefreshTests(RouteFixture):
    def test_every_page_carries_the_refresh_and_the_bar_carries_its_switch(self) -> None:
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                page = self.get(path)
                self.assertIn("data-refresh-toggle", bar_of(page))
                self.assertIn(page_assets._REFRESH_SCRIPT, page)

    def test_the_dashboard_has_the_bar_switch_and_no_second_one(self) -> None:
        page = self.get("/")
        self.assertNotIn('id="auto-refresh"', page)
        self.assertEqual(page.count('<input type="checkbox"'), 1, "the bar's switch, and no other")

    def test_the_reload_is_refused_while_a_form_has_focus_or_holds_typed_text(self) -> None:
        """The rule the /po composer depends on, read out of the one script that reloads a page.

        Nothing here runs JavaScript, so what is pinned is the shape of the guard: both clauses are
        present, both are `return`s, and both stand before the only `reload` in the script.
        """
        script = page_assets._REFRESH_SCRIPT
        focus = script.index(
            "if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' "
            "|| active.tagName === 'SELECT')) return;"
        )
        typed = script.index("for (const field of document.querySelectorAll(")
        selector = re.search(r"querySelectorAll\('([^']+)'\)\) if \(field\.value\) return;", script)
        assert selector is not None
        self.assertIn("textarea", selector.group(1))
        self.assertIn("input:not([type])", selector.group(1))
        self.assertEqual(script.count("window.location.reload()"), 1)
        reload_at = script.index("window.location.reload()")
        self.assertLess(focus, reload_at)
        self.assertLess(typed, reload_at)
        # And the switch itself can turn the whole thing off.
        self.assertIn("if (!on) return;", script)

    def test_the_po_composer_is_a_field_the_guard_covers(self) -> None:
        """The composer is a textarea, which is what makes the rule above cover a half-written message."""
        page = self.get("/po/sessions/s-1")
        self.assertIn('<textarea id="po-text" name="text">', page)
        self.assertIn("for (const field of document.querySelectorAll('textarea,", page)


# -- a reset is how long is left, drawn once for both places -------------------------------------


class ResetRenderingTests(unittest.TestCase):
    """The one renderer, asked directly, at every distance the card names."""

    def left(self, resets_at: Any, *, minutes: float = 0.0) -> str:
        """What the renderer draws for `resets_at`, `minutes` after :data:`RENDERED_AT`."""
        now = RENDERED_AT + timedelta(minutes=minutes)
        with pages.render_clock(lambda: now):
            return page_parts._reset(resets_at)

    def test_more_than_a_day_away_is_whole_days_and_hours(self) -> None:
        drawn = self.left("2026-09-22T23:30:00Z")
        self.assertIn(">2d 11h left<", drawn)

    def test_under_a_day_is_hours_and_minutes(self) -> None:
        self.assertIn(">6h 45m left<", self.left("2026-09-20T18:45:00Z"))

    def test_under_an_hour_is_minutes(self) -> None:
        self.assertIn(">42m left<", self.left("2026-09-20T12:42:00Z"))

    def test_under_a_minute_is_said_as_less_than_a_minute_and_never_as_zero(self) -> None:
        drawn = self.left("2026-09-20T12:00:40Z")
        self.assertIn(">less than a minute left<", drawn)
        self.assertNotIn("0m left", drawn)

    def rolled(self, resets_at: Any, window_minutes: Any, *, minutes: float = 0.0) -> tuple[str, bool]:
        now = RENDERED_AT + timedelta(minutes=minutes)
        with pages.render_clock(lambda: now):
            return page_parts._reset_reading(resets_at, window_minutes)

    def test_a_moment_already_past_rolls_forward_by_its_window(self) -> None:
        """A reset ten minutes ago on a 5-hour window means the running window resets in 4h 50m."""
        drawn, predates = self.rolled("2026-09-20T11:50:00Z", 300)
        self.assertIn(">4h 50m left<", drawn)
        self.assertTrue(predates, "the reading belongs to the window before the reset")
        text = drawn.split(">")[1].split("<")[0]
        self.assertNotIn("-", text, "never a negative duration")

    def test_several_windows_back_rolls_to_the_same_next_reset(self) -> None:
        # 2 x 300 min + 10 min ago.
        drawn, predates = self.rolled("2026-09-20T01:50:00Z", 300)
        self.assertIn(">4h 50m left<", drawn)
        self.assertTrue(predates)

    def test_the_exact_moment_at_the_reset_starts_a_whole_new_window(self) -> None:
        drawn, predates = self.rolled("2026-09-20T12:00:00Z", 300)
        self.assertIn(">5h 0m left<", drawn)
        self.assertTrue(predates)

    def test_a_moment_still_ahead_is_not_rolled_and_its_reading_is_current(self) -> None:
        drawn, predates = self.rolled("2026-09-20T18:00:00Z", 300)
        self.assertIn(">6h 0m left<", drawn)
        self.assertFalse(predates)

    def test_a_moment_a_fraction_of_a_second_ahead_is_less_than_a_minute(self) -> None:
        drawn, predates = self.rolled("2026-09-20T12:00:00.400000Z", 300)
        self.assertIn(">less than a minute left<", drawn)
        self.assertFalse(predates)

    def test_a_past_moment_with_no_usable_window_says_no_reset_recorded(self) -> None:
        for minutes in (None, 0, -300, True, False, 300.0, "300", 1e300):
            with self.subTest(window_minutes=minutes):
                drawn, predates = self.rolled("2026-09-20T11:50:00Z", minutes)
                self.assertIn(f">{page_parts.NO_RESET_RECORDED}<", drawn)
                self.assertTrue(predates, "the percentage is stale all the same")

    def test_hostile_moments_and_windows_never_raise_or_loop(self) -> None:
        """A table of hostile inputs: each is answered in bounded time with no exception."""
        cases: list[tuple[Any, Any, str | None, bool]] = [
            # (resets_at, window_minutes, expected text or None for "any countdown", predates)
            ("2026-09-20T11:50:00Z", 1e300, page_parts.NO_RESET_RECORDED, True),
            ("2026-09-20T11:50:00Z", 10**400, page_parts.NO_RESET_RECORDED, True),
            ("2026-09-20T11:50:00Z", -(10**400), page_parts.NO_RESET_RECORDED, True),
            ("2026-09-20T11:50:00Z", True, page_parts.NO_RESET_RECORDED, True),
            ("2026-09-20T11:50:00Z", "300", page_parts.NO_RESET_RECORDED, True),
            ("2026-09-20T11:50:00Z", -1, page_parts.NO_RESET_RECORDED, True),
            ("1970-01-01T00:00:00Z", 1, "1m left", True),
            ("1970-01-01T00:00:00Z", 300, None, True),
            ("0001-01-01T00:00:00+14:00", 1, None, True),
            ("0001-01-01T00:00:00Z", page_parts.LONGEST_WINDOW_MINUTES, None, True),
            ("9999-12-31T23:59:59-14:00", 300, None, False),
            (1e300, 300, page_parts.NO_RESET_RECORDED, False),
            (10**400, 300, page_parts.NO_RESET_RECORDED, False),
            (True, 300, page_parts.NO_RESET_RECORDED, False),
            (-5, 300, page_parts.NO_RESET_RECORDED, False),
        ]
        for resets_at, minutes, text, predates in cases:
            with self.subTest(resets_at=resets_at, window_minutes=minutes):
                drawn, stale = self.rolled(resets_at, minutes)
                self.assertEqual(stale, predates)
                if text is not None:
                    self.assertIn(f">{text}<", drawn)
                else:
                    self.assertRegex(drawn, r">(\d+d \d+h|\d+h \d+m|\d+m|less than a minute) left<")

    def test_a_reading_with_no_moment_says_so_and_carries_no_title(self) -> None:
        for absent in (None, "", "   ", "not a moment", 17):
            with self.subTest(absent=absent):
                drawn = self.left(absent)
                self.assertIn(page_parts.NO_RESET_RECORDED, drawn)
                self.assertNotIn("title=", drawn)

    def test_the_moment_is_kept_as_the_hover_title(self) -> None:
        self.assertIn('title="2026-09-20T18:00:00Z"', self.left("2026-09-20T18:00:00Z"))

    def test_the_countdown_is_measured_against_the_render_clock_and_not_the_reading(self) -> None:
        """Criterion 4: the same reading, rendered later, has less time left -- by exactly the wait.

        A cached reading can be up to `CACHE_SECONDS` old, so counting from its `observed_at` would
        keep repeating the time that was left when it was observed.
        """
        self.assertIn(">6h 0m left<", self.left("2026-09-20T18:00:00Z"))
        self.assertIn(">5h 0m left<", self.left("2026-09-20T18:00:00Z", minutes=60))

    def test_a_moment_without_an_offset_is_read_as_utc_rather_than_as_local_time(self) -> None:
        self.assertIn(">6h 0m left<", self.left("2026-09-20T18:00:00"))


class PercentRenderingTests(unittest.TestCase):
    """A percentage is drawn once for both places too, and a tenth that is zero is not drawn at all."""

    def test_a_whole_reading_carries_no_trailing_tenth(self) -> None:
        self.assertEqual(page_parts._percent(73.0), "73%")
        self.assertEqual(page_parts._percent(100.0), "100%")
        self.assertEqual(page_parts._percent(72), "72%")

    def test_a_reading_that_really_is_fractional_keeps_its_one_digit(self) -> None:
        self.assertEqual(page_parts._percent(95.4), "95.4%")
        self.assertEqual(page_parts._percent(0.5), "0.5%")

    def test_no_reading_is_a_dash_and_never_a_zero_or_a_bare_per_cent_sign(self) -> None:
        for absent in (None, "", "74", True):
            with self.subTest(absent=absent):
                self.assertEqual(page_parts._percent(absent), "—")


class TheBarDrawsTheResetTests(RouteFixture):
    """Criterion 1: the bar is the one place a reset is drawn, and it draws it by one rule."""

    def test_the_bar_renders_each_reset_as_the_time_left(self) -> None:
        page = self.get("/")
        bar = bar_of(page)
        self.assertIn('<span class="resets" title="2026-09-20T18:00:00Z">6h 0m left</span>', bar)
        self.assertIn('<span class="resets" title="2026-09-25T00:00:00Z">4d 12h left</span>', bar)
        main = page[page.index("<main>") : page.index('<footer class="statusbar"')]
        self.assertNotIn('class="resets"', main)

    def test_neither_place_prints_an_iso_moment_as_the_text_of_the_reset(self) -> None:
        page = self.get("/")
        self.assertNotIn(">2026-09-20T18:00:00Z<", page)
        self.assertNotIn("resets 2026-09-20T18:00:00Z", page)

    def test_a_window_with_no_moment_says_so_in_words_once(self) -> None:
        self.usage = Recording(
            usage_snapshot=usage_document(
                [provider("claude", "Claude", windows=[{"name": "5-hour", "remaining_percent": 74.0}])]
            )
        )
        page = self.get("/")
        self.assertEqual(page.count(page_parts.NO_RESET_RECORDED), 1, "once, on the bar")


class TheBarNeverDrawsAPreResetFigureTests(RouteFixture):
    """A window whose reset has come shows its next reset, and its old percentage only as stale."""

    def test_a_rolled_window_draws_stale_and_the_countdown_to_its_next_reset(self) -> None:
        self.usage = Recording(
            usage_snapshot=usage_document(
                [
                    provider(
                        "claude",
                        "Claude",
                        windows=[
                            window("5-hour", 63.0, "2026-09-20T11:50:00Z"),
                            window("weekly", 88.0, "2026-09-22T12:00:00Z"),
                        ],
                    )
                ]
            )
        )
        bar = bar_of(self.get("/"))
        chips = re.findall(r'<span class="window">.*?</span></span>', bar)
        self.assertEqual(len(chips), 2)
        self.assertIn(">4h 50m left<", chips[0])
        self.assertIn(f'<b class="stale" title="{page_parts.READING_PREDATES_RESET}">stale</b>', chips[0])
        self.assertNotIn("63%", bar, "no figure from before the reset is drawn")
        self.assertIn("<b>88%</b>", chips[1])
        self.assertIn(">2d 0h left<", chips[1])

    def test_a_past_window_with_no_length_says_no_reset_recorded_and_stale(self) -> None:
        past = {"name": "5-hour", "remaining_percent": 63.0, "resets_at": "2026-09-20T11:50:00Z"}
        self.usage = Recording(usage_snapshot=usage_document([provider("claude", "Claude", windows=[past])]))
        bar = bar_of(self.get("/"))
        self.assertIn(page_parts.NO_RESET_RECORDED, bar)
        self.assertIn(">stale</b>", bar)
        self.assertNotIn("63%", bar)


class ClaudeUtilizationIsAPercentageTests(unittest.TestCase):
    def test_a_utilization_of_one_is_one_percent_used_and_draws_99_percent_left(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            credentials = Path(home) / ".claude/.credentials.json"
            credentials.parent.mkdir(parents=True)
            credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "c"}}), encoding="utf-8")

            def fetch(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
                if url != CLAUDE_USAGE_URL:
                    raise TimeoutError
                return {"five_hour": {"utilization": 1.0, "resets_at": "2026-09-20T15:00:00Z"}}

            document = ProviderUsageLayer(home=home, fetch_json=fetch, now=lambda: NOW).usage_snapshot()
        with pages.render_clock(lambda: RENDERED_AT):
            bar = page_shell._limits_bar_of({"available": True, "document": document})
        self.assertIn("<b>99%</b>", bar)
        self.assertIn(">3h 0m left<", bar)


# -- the two findings folded in from secretary-1645's review -------------------------------------


class RefreshGuardCoversTypedSecretsTests(RouteFixture):
    """A password field holds typed content like any other."""

    def selector(self) -> str:
        found = re.search(
            r"querySelectorAll\('([^']+)'\)\) if \(field\.value\) return;", page_assets._REFRESH_SCRIPT
        )
        assert found is not None
        return found.group(1)

    def test_the_typed_content_guard_matches_a_password_field(self) -> None:
        self.assertIn("input[type=password]", self.selector())


class APostResultDoesNotReloadItselfTests(RouteFixture):
    """A page answering a submission must not tick into the browser's resubmission prompt."""

    def refused_rename(self) -> str:
        self.po.answers["po_rename"] = ValidationRefused("the title is longer than 120 characters")
        response = self.app().handle("POST", "/po/sessions/s-1/title", body=b"title=x")
        self.assertEqual(response.status, 400)
        return response.body.decode("utf-8")

    def test_a_page_rendered_from_a_post_carries_no_auto_reload(self) -> None:
        page = self.refused_rename()
        self.assertNotIn("window.location.reload()", page)
        self.assertNotIn(page_assets._REFRESH_SCRIPT, page)

    def test_that_same_page_reached_by_a_get_does_carry_it(self) -> None:
        self.assertIn(page_assets._REFRESH_SCRIPT, self.get("/po/sessions/s-1"))

    def test_the_post_result_still_carries_the_bar_it_simply_does_not_reload(self) -> None:
        self.assertIn("<b>Claude</b>", bar_of(self.refused_rename()))

    def test_the_mark_is_unset_again_once_the_request_is_answered(self) -> None:
        self.refused_rename()
        self.assertIn(page_assets._REFRESH_SCRIPT, self.get("/"))


# -- the shell holds nothing between two requests ------------------------------------------------


class BarSourceIsPerRequestTests(RouteFixture):
    def test_the_source_is_unset_again_once_the_request_is_answered(self) -> None:
        app = self.app()
        self.assertIn("74%", bar_of(self.get("/", app=app)))
        # Rendered outside any request, the same shell says it was fed nothing rather than
        # repeating what the last request happened to see.
        self.assertIn(page_shell.LIMITS_NOT_BUILT, bar_of(pages.error(404, "not_found", "no route here")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
