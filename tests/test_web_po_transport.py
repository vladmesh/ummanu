"""`/po` over the transport: the token gate, the login cookie, the model list and the dashboard indicator.

The PO layer is a recording fake here, so "reaches no PO layer" is an assertion about its calls; the
token layer is the real one over a token file in a temporary data directory. No database.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar
from urllib.parse import urlencode

from tests.web_fakes import Recording, system_snapshot
from ummanu.config import validate
from ummanu.po import token as po_token
from ummanu.po.models import DEFAULT_EFFORTS, DEFAULT_MODELS, efforts_from_instance, models_from_instance
from ummanu.web import pages
from ummanu.web.app import PO_FORM_FIELDS, PO_OPEN_ROUTES, ROUTES, WebApp, requires_po_token
from ummanu.web.server import build_server
from ummanu.webproto.errors import InstallationUnavailable, RuntimeUnavailable
from ummanu.webproto.po_auth import PoTokenLayer

PO_ROUTES = {
    ("POST", "/po/login"),
    ("GET", "/po"),
    ("POST", "/po/sessions"),
    ("GET", "/po/sessions/{session}"),
    ("POST", "/po/sessions/{session}/messages"),
    ("POST", "/po/sessions/{session}/stop"),
    ("POST", "/po/sessions/{session}/close"),
    ("POST", "/po/sessions/{session}/title"),
    ("GET", "/po/api/sessions/{session}"),
}
#: A form body carrying every field any /po POST takes; the gate answers before any field is read.
ANY_FORM = urlencode(
    [("request_id", "r"), ("text", "hi"), ("cli", "claude"), ("model", "opus"), ("seq", "1")]
)


class ServiceInputDisplayTests(unittest.TestCase):
    def test_explicit_queued_and_feed_inputs_are_closed_and_escaped_with_full_text(self):
        entry = {"role": "owner", "turn_seq": 1, "text": "<script>rights & comments; task show --ref ummanu-50</script>",
                 "metadata": {"source": "dispatcher", "summary": "Question <one>"}}
        for render in (pages._po_entry, pages._po_queued_entry):
            for source in ("dispatcher", "po-service"):
                entry["metadata"]["source"] = source
                shown = render(entry)
                self.assertIn('<details class="po-service-input">', shown)
                self.assertNotIn(" open", shown)
                self.assertLess(shown.index("Question &lt;one&gt;"), shown.index("<details"))
                self.assertIn("rights &amp; comments; task show --ref ummanu-50", shown)
                self.assertNotIn("<script>", shown)
                self.assertIn(source + " ·", shown)

    def test_owner_and_historical_text_are_never_classified_from_prose(self):
        entry = {"role": "owner", "turn_seq": 1,
                 "source": "dispatcher", "text": "The dispatcher hands you this card. ## Production rights"}
        for render in (pages._po_entry, pages._po_queued_entry):
            for metadata in (None, {"source": "web"}):
                entry["metadata"] = metadata
                shown = render(entry)
                self.assertNotIn("<details", shown)
                self.assertIn(entry["text"], shown)
                self.assertIn("owner ·", shown)
        entry.update(role="agent", text="## Answer\nYes", metadata={"source": "dispatcher"})
        self.assertIn('<div class="md">', pages._po_entry(entry))
        self.assertNotIn("<details", pages._po_entry(entry))


def concrete(pattern: str) -> str:
    return pattern.replace("{session}", "s-1")


class PoGateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.tmp / "data"
        self.data.mkdir()
        po_token.ensure_token(self.data)
        self.token = po_token.read_token(self.data)
        # The dashboard's pause and sprint sections refuse, which it draws as marked sections.
        unreadable = InstallationUnavailable("not part of this test")
        self.layers = [
            Recording(system_snapshot=system_snapshot()),
            Recording(),
            Recording(sprint_list=unreadable),
            Recording(),
            Recording(pause_state=unreadable),
            Recording(),
            Recording(),
            Recording(),
        ]
        self.po = Recording(po_running_count={"kind": "po_running", "running": 0})
        self.web = WebApp(*self.layers, po_auth=PoTokenLayer(self.tmp, data_dir=self.data), po=self.po)

    def request(self, method: str, path: str, *, body: bytes = b"", cookie: str | None = None, headers=None):
        sent = dict(headers or {})
        if cookie is not None:
            sent["Cookie"] = f"{po_token.COOKIE_NAME}={cookie}"
        return self.web.handle(method, path, body=body, headers=sent)

    def login(self, token: str, headers=None):
        return self.request("POST", "/po/login", body=urlencode([("token", token)]).encode(), headers=headers)

    def po_calls(self) -> list[str]:
        return [name for name, _ in self.po.calls]


class PoTokenGateTests(PoGateFixture):
    def test_the_route_table_lists_the_po_routes_and_all_but_the_login_are_under_the_token(self) -> None:
        under_po = {
            (route.method, route.pattern)
            for route in ROUTES
            if route.pattern == "/po" or route.pattern.startswith("/po/")
        }
        self.assertEqual(under_po, PO_ROUTES)
        self.assertEqual(PO_OPEN_ROUTES, {("POST", "/po/login")})
        guarded = {(route.method, route.pattern) for route in ROUTES if requires_po_token(route)}
        self.assertEqual(guarded, PO_ROUTES - PO_OPEN_ROUTES)

    def test_every_po_route_without_a_valid_cookie_is_refused_and_reaches_no_po_layer(self) -> None:
        wrong = po_token.cookie_value(self.token + "-old")
        for route in ROUTES:
            if not requires_po_token(route):
                continue
            for cookie in (None, "", "garbage", self.token, wrong):
                with self.subTest(route=route.pattern, method=route.method, cookie=cookie):
                    body = ANY_FORM.encode() if route.method == "POST" else b""
                    response = self.request(route.method, concrete(route.pattern), body=body, cookie=cookie)
                    self.assertEqual(response.status, 401)
                    self.assertNotIn("Set-Cookie", response.headers)
                    if route.page:
                        self.assertIn('action="/po/login"', response.body.decode())
                    else:
                        self.assertEqual(json.loads(response.body)["error"]["code"], "po_token_required")
        self.assertEqual(self.po.calls, [])

    def test_a_wrong_token_is_refused_and_sets_no_cookie(self) -> None:
        for token in ("", "nope", self.token + "x", self.token[:-1]):
            with self.subTest(token=token):
                response = self.login(token)
                self.assertEqual(response.status, 401)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn("not this installation&#x27;s PO token", response.body.decode())
        self.assertEqual(self.po.calls, [])

    def test_the_right_token_sets_a_derived_http_only_strict_cookie_on_path_po(self) -> None:
        response = self.login(self.token)

        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/po")
        cookie = response.headers["Set-Cookie"]
        first, *attributes = [part.strip() for part in cookie.split(";")]
        name, value = first.split("=", 1)
        self.assertEqual(name, po_token.COOKIE_NAME)
        self.assertNotIn(self.token, cookie)
        self.assertEqual(value, po_token.cookie_value(self.token))
        self.assertIn("HttpOnly", attributes)
        self.assertIn("SameSite=Strict", attributes)
        self.assertIn("Path=/po", attributes)
        self.assertNotIn("Secure", attributes)

        page = self.request("GET", "/po", cookie=value)
        self.assertEqual(page.status, 200)
        self.assertEqual(self.po_calls(), ["po_overview"])

    def test_the_cookie_is_secure_when_the_request_came_through_the_tls_front(self) -> None:
        response = self.login(self.token, headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status, 303)
        self.assertIn("Secure", [part.strip() for part in response.headers["Set-Cookie"].split(";")])

    def test_a_replaced_token_file_invalidates_the_old_cookie(self) -> None:
        old = po_token.cookie_value(self.token)
        self.assertEqual(self.request("GET", "/po", cookie=old).status, 200)

        po_token.token_path(self.data).unlink()
        self.assertTrue(po_token.ensure_token(self.data))

        self.assertEqual(self.request("GET", "/po", cookie=old).status, 401)
        self.assertEqual(self.login(self.token).status, 401)
        fresh = self.login(po_token.read_token(self.data))
        self.assertEqual(fresh.status, 303)
        value = fresh.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertEqual(self.request("GET", "/po", cookie=value).status, 200)

    def test_a_missing_token_file_refuses_the_login_and_every_route_without_reaching_the_po_layer(
        self,
    ) -> None:
        cookie = po_token.cookie_value(self.token)
        po_token.token_path(self.data).unlink()

        self.assertEqual(self.login(self.token).status, 503)
        self.assertEqual(self.request("GET", "/po", cookie=cookie).status, 503)
        self.assertEqual(self.request("GET", "/po/api/sessions/s-1", cookie=cookie).status, 503)
        self.assertEqual(self.po.calls, [])

    def test_a_login_posted_from_another_origin_is_refused(self) -> None:
        response = self.login(
            self.token, headers={"Origin": "https://attacker.example", "Host": "front.example"}
        )
        self.assertEqual(response.status, 403)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_a_process_built_without_the_po_layers_does_not_serve_po(self) -> None:
        response = WebApp(*self.layers).handle("GET", "/po")
        self.assertEqual(response.status, 503)

    def test_the_gate_sees_the_cookie_a_real_request_arrives_with(self) -> None:
        server = build_server(self.web, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=10)
        self.addCleanup(connection.close)

        connection.request("GET", "/po")
        refused = connection.getresponse()
        refused.read()
        self.assertEqual(refused.status, 401)

        connection.request(
            "POST",
            "/po/login",
            body=urlencode([("token", self.token)]),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        admitted = connection.getresponse()
        admitted.read()
        self.assertEqual(admitted.status, 303)
        cookie = admitted.getheader("Set-Cookie").split(";")[0]

        connection.request("GET", "/po", headers={"Cookie": f"other=1; {cookie}"})
        page = connection.getresponse()
        page.read()
        self.assertEqual(page.status, 200)
        self.assertEqual(self.po_calls(), ["po_overview"])


class PoIndicatorTests(PoGateFixture):
    def test_the_dashboard_counts_running_po_turns_and_links_to_po_without_a_token(self) -> None:
        self.po.answers["po_running_count"] = {"kind": "po_running", "running": 2}

        response = self.request("GET", "/")

        self.assertEqual(response.status, 200)
        page = response.body.decode()
        self.assertIn('<a href="/po" id="po-indicator">2 PO turns running</a>', page)
        self.assertEqual(self.po_calls(), ["po_running_count"])

    def test_a_po_store_that_does_not_answer_leaves_the_dashboard_standing(self) -> None:
        self.po.answers["po_running_count"] = RuntimeUnavailable("the PO session store is not available")

        response = self.request("GET", "/")

        self.assertEqual(response.status, 200)
        page = response.body.decode()
        self.assertNotIn("po-indicator", page)
        self.assertIn("running turns could not be counted", page)
        self.assertIn("<h1>Dashboard</h1>", page)


class PoRequestIdCoverageTests(PoGateFixture):
    """Every /po route that takes a request id reaches `PoStore`'s one request transaction."""

    #: The /po routes carrying `request_id`, and the operation each hands it to:
    #: `po_create_session` -> `PoRunner.create_session_request` -> `PoStore.claim_session`, and
    #: `po_send` -> `PoRunner.send_request` -> `PoStore.claim_turn`.
    REQUEST_ROUTES: ClassVar[dict] = {
        ("POST", "/po/sessions"): "po.po_create_session",
        ("POST", "/po/sessions/{session}/messages"): "po.po_send",
    }
    BODIES: ClassVar[dict] = {
        "/po/sessions": [
            ("request_id", "form-create"),
            ("cli", "claude"),
            ("model", "opus"),
            ("effort", "high"),
        ],
        "/po/sessions/{session}/messages": [("request_id", "form-send"), ("text", "hello")],
    }

    def test_every_po_post_declares_its_fields_and_exactly_these_carry_a_request_id(self) -> None:
        posts = {
            route.handler: route
            for route in ROUTES
            if route.method == "POST" and (route.pattern == "/po" or route.pattern.startswith("/po/"))
        }
        self.assertEqual(set(posts), set(PO_FORM_FIELDS))
        carrying = {
            (route.method, route.pattern): route.operation
            for handler, route in posts.items()
            if "request_id" in PO_FORM_FIELDS[handler]
        }
        self.assertEqual(carrying, self.REQUEST_ROUTES)

    def test_each_request_id_route_hands_the_id_to_its_operation(self) -> None:
        self.po.answers["po_create_session"] = {"kind": "po_session_created", "session_id": "s-1"}
        cookie = po_token.cookie_value(self.token)
        for (method, pattern), operation in self.REQUEST_ROUTES.items():
            with self.subTest(route=pattern):
                fields = self.BODIES[pattern]
                response = self.request(
                    method, concrete(pattern), body=urlencode(fields).encode(), cookie=cookie
                )
                self.assertEqual(response.status, 303)
                name, arguments = self.po.calls[-1]
                self.assertEqual(name, operation.split(".", 1)[1])
                self.assertEqual(arguments["request_id"], dict(fields)["request_id"])

    def test_sessions_turns_and_request_rows_are_written_only_inside_the_request_transaction(self) -> None:
        root = Path(__file__).resolve().parents[1]
        store = root / "src" / "ummanu" / "po" / "store.py"
        tree = ast.parse(store.read_text(encoding="utf-8"))
        inserts: dict[str, set[str]] = {"po_sessions": set(), "po_turns": set(), "po_requests": set()}
        recorders: set[str] = set()
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for table in inserts:
                        if f"INSERT INTO {table} " in node.value:
                            inserts[table].add(function.name)
                if isinstance(node, ast.Attribute) and node.attr == "_record_request":
                    recorders.add(function.name)
        self.assertEqual(
            inserts,
            {
                "po_sessions": {"claim_session"},
                "po_turns": {"claim_turn"},
                "po_requests": {"_record_request"},
            },
        )
        self.assertEqual(recorders, {"claim_session", "claim_turn"})
        others = [
            str(path.relative_to(root))
            for path in (root / "src").rglob("*.py")
            if path != store
            and "migrations" not in path.parts
            and any(f"INSERT INTO {table}" in path.read_text(encoding="utf-8") for table in inserts)
        ]
        self.assertEqual(others, [])


class PoModelListTests(unittest.TestCase):
    INSTANCE: ClassVar[dict] = {
        "version": 1,
        "name": "n",
        "data_dir": "data",
        "offsite": {"instance_remote": "git@x:y.git"},
    }

    def test_the_default_offers_the_frontier_models_first(self) -> None:
        # The current catalogue, as the live installation's `po.models` lists it.
        self.assertEqual(
            DEFAULT_MODELS,
            {
                "claude": ("fable", "claude-opus-5-5"),
                "codex": ("gpt-6-astra", "gpt-6-sol", "gpt-5.6-terra"),
            },
        )

    def test_without_a_po_section_the_product_default_applies(self) -> None:
        self.assertEqual(models_from_instance(self.INSTANCE), DEFAULT_MODELS)
        self.assertEqual(models_from_instance({**self.INSTANCE, "po": {}}), DEFAULT_MODELS)

    def test_a_configured_list_replaces_one_cli_and_an_empty_one_offers_nothing(self) -> None:
        models = models_from_instance({**self.INSTANCE, "po": {"models": {"claude": ["opus"], "codex": []}}})
        self.assertEqual(models, {"claude": ("opus",), "codex": ()})
        only_claude = models_from_instance({**self.INSTANCE, "po": {"models": {"claude": ["sonnet"]}}})
        self.assertEqual(only_claude["codex"], DEFAULT_MODELS["codex"])

    def test_the_schema_takes_the_section_and_refuses_another_cli(self) -> None:
        good = {**self.INSTANCE, "po": {"models": {"claude": ["opus"], "codex": ["gpt-5.6-sol"]}}}
        self.assertEqual(validate(good, "instance", "instance.yaml"), [])
        for bad in (
            {"models": {"gemini": ["x"]}},
            {"models": {"claude": [""]}},
            {"models": {"claude": "opus"}},
            {"other": True},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(validate({**self.INSTANCE, "po": bad}, "instance", "instance.yaml"))

    def test_efforts_default_per_cli_and_a_configured_list_replaces_one_cli(self) -> None:
        self.assertEqual(efforts_from_instance(self.INSTANCE), DEFAULT_EFFORTS)
        # `default` is never offered, and the first entry, the preselection, is `high`.
        self.assertEqual(DEFAULT_EFFORTS["claude"], ("high", "low", "medium", "xhigh", "max"))
        self.assertEqual(DEFAULT_EFFORTS["codex"], ("high", "low", "medium", "xhigh"))
        efforts = efforts_from_instance(
            {**self.INSTANCE, "po": {"efforts": {"claude": ["high"], "codex": []}}}
        )
        self.assertEqual(efforts, {"claude": ("high",), "codex": ()})
        # An installation that still lists `default` stays valid; the offered list drops it.
        good = {**self.INSTANCE, "po": {"efforts": {"claude": ["default", "max"], "codex": ["xhigh"]}}}
        self.assertEqual(validate(good, "instance", "instance.yaml"), [])
        self.assertEqual(efforts_from_instance(good), {"claude": ("max",), "codex": ("xhigh",)})
        for bad in (
            {"efforts": {"claude": ["turbo"]}},
            {"efforts": {"codex": ["max"]}},
            {"efforts": {"gemini": []}},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(validate({**self.INSTANCE, "po": bad}, "instance", "instance.yaml"))


class _Ids(HTMLParser):
    """Every element with an id, and the ids of the elements it sits inside."""

    VOID = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str | None] = []
        self.inside: dict[str, tuple[str, ...]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        own = dict(attrs).get("id")
        if own:
            self.inside[own] = tuple(ancestor for ancestor in self.stack if ancestor)
        if tag not in self.VOID:
            self.stack.append(own)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.VOID and self.stack:
            self.stack.pop()


class PoSessionInPlaceTests(unittest.TestCase):
    """A turn's end updates the session page in place: the changing blocks are swapped, nothing reloads.

    No JS runtime runs here: the script is asserted by structure, the blocks by the markup.
    """

    SESSION: ClassVar[dict[str, Any]] = {
        "session_id": "abcdef0123456789",
        "cli": "claude",
        "model": "opus",
        "effort": "high",
        "state": "open",
    }

    def document(self, *, running: bool, queued: int = 0, turns: int = 1) -> dict[str, Any]:
        seqs = range(1, turns + 1)
        return {
            "session": dict(self.SESSION),
            "turns": [
                {"seq": seq, "state": "running" if running and seq == turns else "completed"} for seq in seqs
            ],
            "feed": [{"turn_seq": seq, "role": "owner", "text": f"question {seq}"} for seq in seqs],
            "queued": [{"text": f"later {n}", "queued_at": "now"} for n in range(queued)],
            "running": running,
            "running_seq": turns if running else None,
        }

    def ids(self, page: str) -> dict[str, tuple[str, ...]]:
        parser = _Ids()
        parser.feed(page)
        return parser.inside

    def test_the_changing_blocks_carry_stable_ids_and_the_composer_is_in_none_of_them(self) -> None:
        for draft in ("", "half a thought"):
            for running, queued in ((True, 0), (True, 2), (False, 0), (False, 1)):
                with self.subTest(draft=draft, running=running, queued=queued):
                    page = pages.po_session(
                        self.document(running=running, queued=queued), request_id="r", draft=draft
                    )
                    ids = self.ids(page)
                    for block in pages.PO_SESSION_BLOCKS:
                        self.assertEqual(page.count(f'id="{block}"'), 1, block)
                    self.assertIn("po-turn-state", ids)
                    self.assertIn("po-head", ids["po-turn-state"])
                    for composer in ("po-text", "po-send", "po-status"):
                        self.assertFalse(set(ids[composer]) & set(pages.PO_SESSION_BLOCKS), composer)
                    send = page.index('<button type="submit" form="po-send">')
                    for block in pages.PO_SESSION_BLOCKS:
                        start = page.index(f'id="{block}"')
                        self.assertFalse(start < send < start + len(self.block(page, block)), block)
                    if draft:
                        self.assertIn(f">{draft}</textarea>", page)

    def block(self, page: str, block: str) -> str:
        """The markup of one block, from its opening tag to its matching close."""
        start = page.rindex("<", 0, page.index(f'id="{block}"'))
        tag = re.match(r"<(\w+)", page[start:]).group(1)  # type: ignore[union-attr]
        depth, at = 0, start
        for found in re.finditer(rf"<(/?){tag}\b", page[start:]):
            depth += -1 if found.group(1) else 1
            if depth == 0:
                at = start + found.end()
                break
        return page[start : page.index(">", at) + 1]

    def test_the_blocks_say_what_the_turn_is_doing(self) -> None:
        running = pages.po_session(self.document(running=True, queued=1), request_id="r")
        state = self.block(running, "po-turn-state")
        self.assertIn("turn running", state)
        self.assertIn("1 queued", state)
        self.assertIn("/stop", self.block(running, "po-stop"))
        self.assertNotIn("<form", self.block(running, "po-close"))
        self.assertIn("later 0", self.block(running, "po-feed"))

        idle = pages.po_session(self.document(running=False), request_id="r")
        self.assertIn("idle", self.block(idle, "po-turn-state"))
        self.assertNotIn("<form", self.block(idle, "po-stop"))
        self.assertIn("/close", self.block(idle, "po-close"))

        empty = pages.po_session({**self.document(running=False, turns=0), "feed": []}, request_id="r")
        self.assertIn("nothing said yet", self.block(empty, "po-feed"))

    def test_the_script_swaps_the_blocks_of_the_page_read_again_and_never_reloads(self) -> None:
        script = pages._PO_SESSION_SCRIPT
        page = pages.po_session(self.document(running=True), request_id="r", draft="typed")
        self.assertNotIn("location.reload", script)
        self.assertIn("const BLOCKS = ['po-head', 'po-stop', 'po-close', 'po-feed'];", page)
        self.assertIn("fetch(window.location.pathname, { cache: 'no-store' })", script)
        self.assertIn("new DOMParser().parseFromString(await response.text(), 'text/html')", script)
        self.assertIn("here.outerHTML = there.outerHTML;", script)
        self.assertIn("document.getElementById('po-status')", script)
        # The composer is read for Enter, and never written: no value, selection or focus is set.
        for touch in ("draft.value =", ".focus(", ".blur(", "setSelectionRange", ".select("):
            self.assertNotIn(touch, script)
        # The draft is no longer a reason to take another path: the swap is the one path.
        self.assertNotIn("draft && draft.value", script)
        self.assertIn("say('the answer arrived');", script)

    def test_the_page_carries_its_own_polling_baseline(self) -> None:
        running = pages.po_session(self.document(running=True, queued=1, turns=2), request_id="r")
        state = self.block(running, "po-turn-state")
        for attribute in ('data-turns="2"', 'data-last="running"', 'data-queued="1"', 'data-running="true"'):
            self.assertIn(attribute, state)
        idle = pages.po_session(self.document(running=False, turns=3), request_id="r")
        state = self.block(idle, "po-turn-state")
        for attribute in (
            'data-turns="3"',
            'data-last="completed"',
            'data-queued="0"',
            'data-running="false"',
        ):
            self.assertIn(attribute, state)
        empty = pages.po_session({**self.document(running=False, turns=0), "feed": []}, request_id="r")
        self.assertIn('data-turns="0" data-last=""', self.block(empty, "po-turn-state"))
        # One source for the baseline: nothing about the turns is pasted into the script any more.
        for placeholder in ("__TURNS__", "__LAST__", "__QUEUED__", "__RUNNING__"):
            self.assertNotIn(placeholder, pages._PO_SESSION_SCRIPT)
        self.assertIn("let seen = shown(document);", pages._PO_SESSION_SCRIPT)
        self.assertIn("if (seen && (seen.running || seen.queued > 0)) {", pages._PO_SESSION_SCRIPT)

    def test_a_turn_that_starts_between_the_json_and_the_page_read_is_followed_to_its_end(self) -> None:
        """The JSON read says turn N ended and nothing waits; another tab's message then starts turn N+1
        before the page is read, so the page swapped in shows N+1 running. The baseline and the decision
        to keep polling are the page's, so the poll goes on; the idle JSON decides nothing after the swap.
        """
        script = pages._PO_SESSION_SCRIPT
        # The page read at that moment says N+1 runs, in the attributes the script reads.
        fresh = pages.po_session(self.document(running=True, turns=2), request_id="r")
        self.assertIn('data-turns="2" data-last="running" data-queued="0" data-running="true"', fresh)
        # The swap answers the fresh page's state, and only after reading it does it write anything.
        swap = script[script.index("async function swapBlocks()") : script.index("function waitingText")]
        self.assertIn("const state = shown(fresh);", swap)
        self.assertLess(swap.index("const state = shown(fresh);"), swap.index("outerHTML"))
        self.assertIn("return { state: state };", swap)
        tick = script[
            script.index("const swapped = await swapBlocks();") : script.index(
                "} catch (error) {\n      say("
            )
        ]
        # After the swap the JSON document is never read again: baseline and stop come from `seen` alone.
        self.assertNotIn("doc", tick)
        taken = tick.index("seen = swapped.state;")
        self.assertLess(tick.index("if (swapped.failed)"), taken)
        still = tick.index(
            "if (seen.running || seen.queued > 0) { say('the page is up to date; ' + waitingText(seen.running)); return; }"
        )
        self.assertLess(taken, still)
        self.assertLess(still, tick.index("window.clearInterval(timer);"))
        self.assertEqual(script.count("window.clearInterval(timer);"), 1)
        # Before the swap the JSON is compared with what the page shows, running included.
        detect = script[script.index("const changed =") : script.index("const swapped = await swapBlocks();")]
        for compared in ("!== seen.turns", "!== seen.last", "!== seen.queued", "!== seen.running"):
            self.assertIn(compared, detect)

    def test_a_failed_read_says_why_and_leaves_the_page_as_it_was(self) -> None:
        script = pages._PO_SESSION_SCRIPT
        swap = script[script.index("async function swapBlocks()") : script.index("function waitingText")]
        self.assertIn("if (!response.ok) return { failed: String(response.status) };", swap)
        self.assertIn(
            "catch (error) { return { failed: (error && error.message) || 'network error' }; }", swap
        )
        self.assertIn("if (missing) return { failed: 'the page has no #' + missing[2] };", swap)
        # A page without its turn state is a failed read too: no swap, the old baseline stays.
        self.assertIn("if (!state) return { failed: 'the page does not say its turn state' };", swap)
        # Every block and the state are checked before any is written: a page missing one changes nothing.
        self.assertLess(swap.index("if (missing)"), swap.index("outerHTML"))
        self.assertLess(swap.index("if (!state)"), swap.index("outerHTML"))
        shown = script[script.index("function shown(page)") : script.index("async function swapBlocks()")]
        for guard in ("if (!element) return null;", "Number.isInteger(turns)", "data.running !== 'true'"):
            self.assertIn(guard, shown)
        self.assertIn("say('could not refresh the answer (' + swapped.failed + ')'); return;", script)
        # The tick itself never throws: its whole body is guarded, and a busy tick is skipped.
        self.assertIn(
            "say('could not refresh the answer (' + ((error && error.message) || 'unexpected error') + ')');",
            script,
        )
        self.assertIn("if (busy) return;", script)
        self.assertIn("finally {\n      busy = false;", script)

    def test_send_works_again_after_the_turn_it_started(self) -> None:
        script = pages._PO_SESSION_SCRIPT
        taken = script.index("seen = swapped.state;")
        reset = script.index("submitted = false;\n      if (button) button.disabled = false;")
        self.assertLess(taken, reset)

    def test_no_page_tells_the_owner_to_reload_to_see_anything(self) -> None:
        web = Path(pages.__file__).parent
        for source in sorted(web.rglob("*.py")):
            with self.subTest(source=source.name):
                self.assertNotIn("reload to see", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
