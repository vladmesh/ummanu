"""`/po` over the transport: request ids, the model list and the dashboard indicator.

The PO layer is a recording fake here, so "reaches no PO layer" is an assertion about its calls. No database.
"""

from __future__ import annotations

import ast
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode

from tests import source_trees
from tests.web_fakes import Recording, system_snapshot
from ummanu.config import validate
from ummanu.po.models import DEFAULT_EFFORTS, DEFAULT_MODELS, efforts_from_instance, models_from_instance
from ummanu.web import page_assets, page_po, pages
from ummanu.web.app import PO_FORM_FIELDS, ROUTES, WebApp
from ummanu.webproto.errors import InstallationUnavailable, RuntimeUnavailable


class ServiceInputDisplayTests(unittest.TestCase):
    def test_explicit_queued_and_feed_inputs_are_closed_and_escaped_with_full_text(self):
        entry = {"role": "owner", "turn_seq": 1, "text": "<script>rights & comments; task show --ref ummanu-50</script>",
                 "metadata": {"source": "dispatcher", "summary": "Question <one>"}}
        for render in (page_po._po_entry, page_po._po_queued_entry):
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
        for render in (page_po._po_entry, page_po._po_queued_entry):
            for metadata in (None, {"source": "web"}):
                entry["metadata"] = metadata
                shown = render(entry)
                self.assertNotIn("<details", shown)
                self.assertIn(entry["text"], shown)
                self.assertIn("owner ·", shown)
        entry.update(role="agent", text="## Answer\nYes", metadata={"source": "dispatcher"})
        self.assertIn('<div class="md">', page_po._po_entry(entry))
        self.assertNotIn("<details", page_po._po_entry(entry))


def concrete(pattern: str) -> str:
    return pattern.replace("{session}", "s-1")


class PoGateFixture(unittest.TestCase):
    def setUp(self) -> None:
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
        self.web = WebApp(*self.layers, po=self.po)

    def request(self, method: str, path: str, *, body: bytes = b""):
        return self.web.handle(method, path, body=body, headers={})

    def po_calls(self) -> list[str]:
        return [name for name, _ in self.po.calls]


class PoIndicatorTests(PoGateFixture):
    def test_a_process_built_without_the_po_layers_does_not_serve_po(self) -> None:
        response = WebApp(*self.layers).handle("GET", "/po")
        self.assertEqual(response.status, 503)

    def test_the_dashboard_counts_running_po_turns_and_links_to_po(self) -> None:
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
        for (method, pattern), operation in self.REQUEST_ROUTES.items():
            with self.subTest(route=pattern):
                fields = self.BODIES[pattern]
                response = self.request(method, concrete(pattern), body=urlencode(fields).encode())
                self.assertEqual(response.status, 303)
                name, arguments = self.po.calls[-1]
                self.assertEqual(name, operation.split(".", 1)[1])
                self.assertEqual(arguments["request_id"], dict(fields)["request_id"])

    def test_sessions_turns_and_request_rows_are_written_only_inside_the_request_transaction(self) -> None:
        root = Path(__file__).resolve().parents[1]
        store = root / "src" / "ummanu" / "po" / "store.py"
        tree = source_trees.parse(store.read_text(encoding="utf-8"))
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
        # Rollover callers bind to the canonical successor in the same native
        # request transaction; this adds no alternate session/turn writer.
        self.assertEqual(recorders, {"claim_session", "claim_turn", "bind_sprint_session_request"})
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
                    for block in page_po.PO_SESSION_BLOCKS:
                        self.assertEqual(page.count(f'id="{block}"'), 1, block)
                    self.assertIn("po-turn-state", ids)
                    self.assertIn("po-head", ids["po-turn-state"])
                    for composer in ("po-text", "po-send", "po-status"):
                        self.assertFalse(set(ids[composer]) & set(page_po.PO_SESSION_BLOCKS), composer)
                    send = page.index('<button type="submit" form="po-send">')
                    for block in page_po.PO_SESSION_BLOCKS:
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
        script = page_assets._PO_SESSION_SCRIPT
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
            self.assertNotIn(placeholder, page_assets._PO_SESSION_SCRIPT)
        self.assertIn("let seen = shown(document);", page_assets._PO_SESSION_SCRIPT)
        self.assertIn("if (seen && (seen.running || seen.queued > 0)) {", page_assets._PO_SESSION_SCRIPT)

    def test_a_turn_that_starts_between_the_json_and_the_page_read_is_followed_to_its_end(self) -> None:
        """The JSON read says turn N ended and nothing waits; another tab's message then starts turn N+1
        before the page is read, so the page swapped in shows N+1 running. The baseline and the decision
        to keep polling are the page's, so the poll goes on; the idle JSON decides nothing after the swap.
        """
        script = page_assets._PO_SESSION_SCRIPT
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
        script = page_assets._PO_SESSION_SCRIPT
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
        script = page_assets._PO_SESSION_SCRIPT
        taken = script.index("seen = swapped.state;")
        reset = script.index("submitted = false;\n      if (button) button.disabled = false;")
        self.assertLess(taken, reset)


if __name__ == "__main__":
    unittest.main()
