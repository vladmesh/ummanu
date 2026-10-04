"""`scripts/measure_dashboard.py`: the one command this sprint's numbers are read off.

The script measures a running installation, so what can be pinned here is everything about it that
is not the installation: that it only ever reads, that the routes it asks for are this product's own
read routes, that the percentile it prints is the one it says it prints, and that its exit status
and its table say the same thing. The measurements themselves run against a stub server this test
binds on loopback — the same hermetic rule the transport suite works under (`tests/README.md`): no
live installation, no real Orca, no network.

Hermetic in one more sense that matters for a timing script: the stub answers instantly, so a case
that wants a slow answer says how slow, and no assertion here depends on how fast this host is.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import ClassVar
from unittest import mock

from ummanu.web.app import ROUTES

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_dashboard.py"


def _script() -> ModuleType:
    """The script, imported by path: `scripts/` is not an importable package (see `tests/broad.py`)."""
    spec = importlib.util.spec_from_file_location("ummanu_measure_dashboard", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing script is a broken tree
        raise RuntimeError(f"the measurement script is unavailable at {SCRIPT}")
    cached = sys.modules.get(spec.name)
    if cached is not None:
        return cached
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


measure = _script()


class _Stub(BaseHTTPRequestHandler):
    """A dashboard that answers as fast, as slowly and as badly as a case tells it to."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        """Quiet: this server's own journal is not what is under test."""

    def _answer(self, *, head: bool) -> None:
        plan = self.server.plan  # type: ignore[attr-defined]
        path = self.path.partition("?")[0]
        status, body, delay, location = plan(self.command, path)
        if delay:
            time.sleep(delay)
        payload = body.encode("utf-8")
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._answer(head=False)

    def do_HEAD(self) -> None:
        self._answer(head=True)


class StubDashboard(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AgainstAStubDashboard(unittest.TestCase):
    """The script against a dashboard whose answers this test decides.

    Everything here is the fixture: the stub, the run helpers, and the patches that keep the case
    off the host it happens to be running on. The cases are in the subclasses, which differ in one
    thing — whether they run the poll at its real three-second cadence or at a token one.
    """

    SESSION = "11111111-2222-3333-4444-555555555555"

    def serve(
        self,
        *,
        missing: tuple[str, ...] = (),
        listed: tuple[tuple[str, bool], ...] | None = None,
        session_body: str | None = None,
        running_reads: int | None = None,
        session_status: int = 200,
        overview_status: int = 200,
        delays: dict[str, float] | None = None,
        slow_get_after: tuple[str, int, float] | None = None,
        status_after: tuple[str, int, int] | None = None,
        redirect: tuple[str, str] | None = None,
    ) -> str:
        """A dashboard with a decided answer, and a decided cost, for every route.

        `slow_get_after` is `(path, nth, seconds)`: that path's GETs become slow from the nth one
        on. It is how a case makes one *round* of the concurrent scenario slow while the earlier
        one stays fast, which is the only way to see which round the threshold is judged on.

        `status_after` is `(path, nth, status)` and does the same to the *status*: the route answers
        cleanly for the warm phase and then starts failing, which is the reviewer's reproduction of
        a dashboard that breaks under the load being measured.

        `redirect` is `(path, location)`: that path answers 302 and points somewhere else, which is
        how a case models a front, a proxy or a wrong `--base-url` trying to send the script — and
        the PO cookie it carries — to another installation.

        `listed` is the `/po` overview's sessions as `(id, running)` pairs, in the order the page
        lists them; the default is the product's interesting case, one session with a turn running.
        `()` is an installation with no open session at all, and `session_body` replaces the JSON
        of every session with something a case decides, for the documents the script must refuse.
        `running_reads` ends a running turn: that many reads of a session answer `running: true`,
        and every read after them answers `false`, the way a turn that finishes mid-run looks.
        """
        catalogue = ((self.SESSION, True),) if listed is None else listed
        seen: list[tuple[str, str]] = []
        lock = threading.Lock()

        def plan(method: str, path: str) -> tuple[int, str, float, str]:
            with lock:
                seen.append((method, path))
                counted = sum(1 for verb, seen_path in seen if verb == "GET" and seen_path == path)
            delay = (delays or {}).get(path, 0.0)
            if slow_get_after is not None:
                slow_path, nth, seconds = slow_get_after
                if method == "GET" and path == slow_path and counted >= nth:
                    delay = seconds
            if redirect is not None and path == redirect[0]:
                return 302, "moved", delay, redirect[1]
            if status_after is not None:
                failing_path, nth, status = status_after
                if method == "GET" and path == failing_path and counted >= nth:
                    return status, "not an answer", delay, ""
            if path in missing:
                return 404, "no such route", delay, ""
            if path == measure.PO_OVERVIEW:
                links = "".join(
                    f'<a class="ref" href="/po/sessions/{session}">session</a>'
                    for session, _running in catalogue
                )
                return overview_status, f"<html>{links}</html>", delay, ""
            if path.startswith("/po/api/sessions/"):
                if session_body is not None:
                    return session_status, session_body, delay, ""
                # The product's own document shape (`webproto.po_ops.po_session`): `running` is a
                # top-level boolean, and it is what decides whether a page polls this session.
                asked = path.rsplit("/", 1)[-1]
                body = {
                    "kind": "po_session",
                    "session": {"session_id": asked},
                    "turns": [],
                    "running": bool(dict(catalogue).get(asked, False))
                    and (running_reads is None or counted <= running_reads),
                }
                return session_status, json.dumps(body), delay, ""
            return 200, "<html>dashboard</html>", delay, ""

        server = StubDashboard(("127.0.0.1", 0), _Stub)
        server.plan = plan  # type: ignore[attr-defined]
        server.seen = seen  # type: ignore[attr-defined]
        self.server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def setUp(self) -> None:
        """No case in this suite may read the host it happens to be running on.

        `tests/README.md`: no test needs a running installation. That is enforced here, once, for
        every case in the class — not by each case remembering to ask for it, which is the shape of
        mistake this card has already spent a round removing from the script itself. A case that
        forgets is no longer possible, and a case that somehow reaches past these patches finds an
        environment with no installation in it and fails the same way on every host instead of
        resolving whatever this one happens to have.

        It is not a hypothetical. `test_an_unreachable_installation_…` did not stub the resolution,
        passed on a developer host that has `~/secretary-instance`, and turned CI red on a runner
        that does not (run 35542829308, `unit`).
        """
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("UMMANU_DATA_DIR", None)
        os.environ.pop("UMMANU_INSTANCE", None)
        self.enterContext(mock.patch("ummanu.onboarding.DEFAULT_INSTANCE", "/nonexistent/instance"))
        self.enterContext(
            mock.patch.object(
                measure, "resolve_data_dir", return_value=(Path("/nonexistent/data"), "a test fixture")
            )
        )
        # What these two resolve to for real is exercised in `DataDirectoryResolutionTests`.
        self.enterContext(mock.patch.object(measure, "po_cookie", return_value="ummanu_po=deadbeef"))

    def run_main(self, base_url: str, *extra: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = measure.main(["--base-url", base_url, *extra])
        return code, out.getvalue() + err.getvalue()


class MeasurementScriptTests(AgainstAStubDashboard):
    """Everything about the script that is not the cadence itself.

    These cases are about statuses, routes, percentiles, verdicts and refusals, and every one of
    them runs the whole three-round scenario. At the real cadence that is nine seconds of waiting
    per case for a property none of them is asserting, so the interval is shortened here — which is
    exactly the shortcut the script refuses to take on a measured run, and is why the cadence is
    proved at its real three seconds, separately, in `CadenceAndLaunchTests`.
    """

    #: Short enough to be free, long enough that a poll is not a busy loop against the stub.
    TOKEN_INTERVAL_SECONDS = 0.05

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(measure, "POLL_INTERVAL_SECONDS", self.TOKEN_INTERVAL_SECONDS))

    # -- read-only ---------------------------------------------------------------------------

    def test_every_request_it_can_make_is_a_read_on_a_read_route_of_this_product(self) -> None:
        """Criterion 8, confirmed from the request list rather than from a run.

        Each entry is checked against the product's own route table, so a route that later became
        a write, or a pattern that stopped existing, fails here instead of on a live installation.
        A HEAD is checked against the GET route it is served by, because that is how the transport
        answers one (`_Handler.do_HEAD`) — there is no separate HEAD row to match.
        """
        installed = {(route.method, route.pattern) for route in ROUTES}
        for method, route in measure.READ_REQUESTS:
            with self.subTest(method=method, route=route):
                self.assertIn(method, measure.READ_METHODS)
                self.assertIn(("GET", route), installed)
        self.assertIn(("HEAD", "/"), measure.READ_REQUESTS, "the reachability probe is a request too")

    def test_the_fetcher_refuses_any_verb_that_is_not_a_read(self) -> None:
        self.assertEqual(set(measure.READ_METHODS), {"GET", "HEAD"})
        with self.assertRaises(ValueError):
            measure.fetch("http://127.0.0.1:1", "/", method="POST")

    def test_no_call_in_the_script_names_a_writing_verb(self) -> None:
        """A grep a reviewer would do, done here so it stays true."""
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
        verbs = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for forbidden in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(forbidden, verbs, f"{forbidden} appears as a literal in the script")

    def test_response_validity_lives_in_fetch_and_no_call_site_re_implements_it(self) -> None:
        """The structural half of the rule: a new request path cannot bypass it.

        Three rounds of review found three different call sites of the same missing status check,
        because the rule lived at call sites. It now lives in `fetch`, and this test is what keeps
        it there: the only transport call in the file is inside `fetch`, the only `Sample` is built
        inside `fetch`, and no code outside it reads a status off a response. A seventh call site
        added next month is therefore a request that went through the rule, or a red test here.

        The transport is the module-level opener, and there is one of it, built with redirects
        refused and with an empty `ProxyHandler`. A second opener — or a plain `urlopen`, which
        follows redirects and reads the proxy environment — would be a request that could leave the
        installation the caller named, so neither may exist anywhere in this file.
        """
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
        fetch = next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "fetch"
        )
        inside_fetch = set(map(id, ast.walk(fetch)))

        def calls(name: str) -> list[ast.Call]:
            return [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Attribute) and node.func.attr == name)
                    or (isinstance(node.func, ast.Name) and node.func.id == name)
                )
            ]

        transport = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(("urlopen", "_OPENER.open"))
        ]
        self.assertEqual(len(transport), 1, "a second transport call would be a request outside the rule")
        self.assertIn(id(transport[0]), inside_fetch, "the only request must be the one inside fetch")
        self.assertEqual(ast.unparse(transport[0].func), "_OPENER.open")

        # `urlopen` follows redirects and cannot be told not to, so the file may not name it at
        # all — not as a call, not as an imported name, not as an attribute of anything.
        named = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.Name) and node.id == "urlopen")
            or (isinstance(node, ast.Attribute) and node.attr == "urlopen")
            or (isinstance(node, ast.alias) and node.name.split(".")[-1] == "urlopen")
        ]
        self.assertEqual(named, [], "every request goes through the opener that refuses redirects")

        openers = calls("build_opener")
        self.assertEqual(len(openers), 1, "one opener, or the redirect rule is not the only rule")
        self.assertEqual(
            [ast.unparse(argument) for argument in openers[0].args],
            ["_RefuseRedirects()", "urllib.request.ProxyHandler({})"],
            "the one opener refuses redirects and reads no proxy environment",
        )

        built = calls("Sample")
        self.assertEqual(len(built), 1, "a Sample built elsewhere is a response nothing validated")
        self.assertIn(id(built[0]), inside_fetch)

        # `status` is read exactly where it is decided. Anywhere else is a call site quietly
        # making its own judgement about a response again.
        outside = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in {"status", "ok"}
            and id(node) not in inside_fetch
        ]
        self.assertEqual(
            [ast.unparse(node) for node in outside],
            [],
            "response status is judged in fetch and nowhere else",
        )

    def test_every_request_the_run_makes_is_refused_when_it_does_not_answer(self) -> None:
        """The behavioural half: each route on the inventory, made to 404, must exit 2.

        Sweeping every route rather than the two the reviewer happened to reproduce, because the
        defect this round closes was never about a particular route — it was about which call site
        remembered to look.
        """
        cases = {
            "/": "the warm and concurrent route",
            "/sprints": "a warm route",
            "/projects": "a warm route",
            measure.PO_OVERVIEW: "where the poll target is chosen",
        }
        for route, why in cases.items():
            with self.subTest(route=route, why=why):
                base = self.serve(missing=(route,))
                with mock.patch.object(measure, "WARM_REQUESTS", 2):
                    code, text = self.run_main(base)
                self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
                self.assertIn("404", text)
                self.assertNotIn("MEETS", text)

    def test_a_run_asks_for_nothing_but_the_method_and_route_pairs_on_the_list(self) -> None:
        """Pairs, not paths: `HEAD /` must not pass because `GET /` happens to be listed."""
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            self.run_main(base)

        listed = set(measure.READ_REQUESTS)
        observed = set()
        for method, path in self.server.seen:  # type: ignore[attr-defined]
            # The one templated route: the session id is chosen at run time, so the observed path
            # is folded back onto the template it came from rather than matched literally.
            canonical = measure.PO_SESSION_JSON if path.startswith("/po/api/sessions/") else path
            observed.add((method, canonical))
        self.assertEqual(observed - listed, set(), "a request was made that the inventory omits")
        # And the inventory is not padded either: every pair on it was actually asked for.
        self.assertEqual(listed - observed, set(), "the inventory lists a request the run never made")

    # -- the named installation, and nothing else --------------------------------------------

    def elsewhere(self) -> str:
        """A second installation on this host, which no request of a run may ever reach."""
        outside = self.serve()
        self.outside = self.server  # type: ignore[attr-defined]
        return outside

    def test_a_redirect_is_refused_and_the_po_cookie_never_leaves_the_named_installation(self) -> None:
        """The reviewer's two-server reproduction, as the acceptance criterion states it.

        `urllib.request.urlopen` follows redirects, so a 302 on `/po` used to be recorded as a 200
        for `/po` — a route that did not answer — and the derived PO cookie was delivered to
        whatever the `Location` named. Here the redirect target is a second local server, and the
        two things asserted are that the run exits 2 and that the second server was never spoken
        to at all.
        """
        outside = self.elsewhere()
        base = self.serve(redirect=(measure.PO_OVERVIEW, f"{outside}/outside-the-installation"))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, report = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, report)
        self.assertIn("302", report)
        self.assertIn("redirect", report)
        self.assertNotIn("MEETS", report)
        self.assertEqual(
            self.outside.seen,  # type: ignore[attr-defined]
            [],
            "the redirect target received a request, so the cookie left the named installation",
        )

    def test_a_redirect_on_any_route_of_the_inventory_is_unmeasurable(self) -> None:
        """Not only `/po`: a 3xx anywhere is a route that did not answer, and is refused.

        A front that redirects `/` to a login page would otherwise be timed as if it were the
        dashboard, which is the same defect wearing a different route.
        """
        for route in ("/", "/sprints", "/projects", measure.PO_OVERVIEW):
            with self.subTest(route=route):
                outside = self.elsewhere()
                base = self.serve(redirect=(route, f"{outside}/elsewhere"))
                with mock.patch.object(measure, "WARM_REQUESTS", 2):
                    code, report = self.run_main(base)
                self.assertEqual(code, measure.EXIT_UNMEASURABLE, report)
                self.assertIn("302", report)
                self.assertNotIn("MEETS", report)
                self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]

    def load_under(self, environment: dict[str, str]) -> ModuleType:
        """A fresh copy of the script, imported with `environment` already in place.

        The opener is built once, when the module is imported, and the standard library's
        `ProxyHandler` reads the proxy variables when it is *constructed*. A case that patched the
        environment after import would therefore pass no matter what the code did. This imports the
        script again with the variables already set, which is the operator's situation: a shell
        that had `http_proxy` in it before the command started.
        """
        name = f"measure_dashboard_under_{len(sys.modules)}"
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with mock.patch.dict(os.environ, environment, clear=False):
            spec.loader.exec_module(module)
        return module

    def test_a_proxy_in_the_environment_never_receives_the_request_or_the_cookie(self) -> None:
        """Criterion 2's other door: `build_opener` keeps the default `ProxyHandler`.

        With `http_proxy` set, that handler sends every request — and the `Cookie` header the `/po`
        reads carry — to the proxy, while the output still names the base URL. The reviewer
        reproduced it against a recording proxy on this interpreter. Here the second local server
        is the proxy, the script is imported with the variables already set, and the assertion is
        that the proxy is never spoken to at all while the installation answers normally.
        """
        proxy = self.elsewhere()
        base = self.serve()
        fresh = self.load_under(
            {"http_proxy": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "all_proxy": proxy}
        )

        sample = fresh.fetch(base, measure.PO_OVERVIEW, cookie="ummanu_po=probe")

        self.assertEqual(sample.status, 200)
        self.assertIn(("GET", measure.PO_OVERVIEW), self.server.seen)  # type: ignore[attr-defined]
        self.assertEqual(
            self.outside.seen,  # type: ignore[attr-defined]
            [],
            "the proxy received the request, and the PO cookie with it",
        )

    def test_a_whole_run_under_a_proxy_environment_stays_on_the_named_installation(self) -> None:
        """The same door, end to end: the command completes, and the proxy log is still empty."""
        proxy = self.elsewhere()
        base = self.serve()
        fresh = self.load_under({"http_proxy": proxy, "HTTP_PROXY": proxy})

        with (
            mock.patch.object(
                fresh, "resolve_data_dir", return_value=(Path("/nonexistent/data"), "a test fixture")
            ),
            mock.patch.object(fresh, "po_cookie", return_value="ummanu_po=deadbeef"),
            mock.patch.object(fresh, "WARM_REQUESTS", 2),
            mock.patch.object(fresh, "POLL_INTERVAL_SECONDS", self.TOKEN_INTERVAL_SECONDS),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = fresh.main(["--base-url", base])

        self.assertEqual(code, fresh.EXIT_MET)
        self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]

    def test_the_fetcher_reports_the_redirect_rather_than_the_page_behind_it(self) -> None:
        """`fetch` itself, with no run around it: the 3xx is the answer it saw."""
        outside = self.elsewhere()
        base = self.serve(redirect=("/", f"{outside}/elsewhere"))

        with self.assertRaises(measure.Unmeasurable) as refused:
            measure.fetch(base, "/", cookie="ummanu_po=deadbeef")

        self.assertIn("302", str(refused.exception))
        self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]
        # And not even an explicit tolerance can turn a redirect into a measurement.
        with self.assertRaises(measure.Unmeasurable):
            measure.fetch(base, "/", tolerate=frozenset({302}))
        self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]

    # -- the numbers -------------------------------------------------------------------------

    def test_p95_is_the_nearest_rank_over_the_twenty_samples(self) -> None:
        values = [float(index) for index in range(1, measure.WARM_REQUESTS + 1)]
        # Nearest rank over twenty samples is the nineteenth: the second slowest request.
        self.assertEqual(measure.p95(values), 19.0)
        self.assertEqual(measure.p95(list(reversed(values))), 19.0)
        self.assertEqual(measure.p95([5.0]), 5.0)

    def test_a_fast_installation_meets_every_threshold_and_exits_zero(self) -> None:
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 3):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertNotIn("EXCEEDS", text)
        self.assertEqual(text.count("MEETS"), len(measure.WARM_ROUTES) + measure.CONCURRENT_REQUESTS)
        self.assertIn(f"base URL: {base}", text)
        self.assertIn("threshold 1000 ms", text)
        self.assertIn("threshold 2000 ms", text)

    def test_a_slow_route_exits_one_and_still_prints_the_whole_table(self) -> None:
        # Just over the warm threshold on one page, and only on that page.
        base = self.serve(delays={"/sprints": (measure.WARM_P95_THRESHOLD_MS + 200) / 1000.0})
        with mock.patch.object(measure, "WARM_REQUESTS", 3):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_EXCEEDED, text)
        self.assertIn("warm p95 GET /sprints", text)
        self.assertIn("EXCEEDS", text)
        # Red prints as much as green: every route and every concurrent request is still there.
        for route in measure.WARM_ROUTES:
            self.assertIn(f"warm p95 GET {route}", text)
        for index in range(1, measure.CONCURRENT_REQUESTS + 1):
            self.assertIn(f"concurrent GET / #{index} of {measure.CONCURRENT_REQUESTS}", text)
        self.assertIn("1 of 7 measurements exceed their threshold", text)

    def test_the_poll_target_is_named_with_how_it_was_chosen(self) -> None:
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertIn(f"/po poll: GET /po/api/sessions/{self.SESSION}", text)
        self.assertIn(f"whose own JSON reports a running turn ({self.SESSION})", text)

    # -- the session a page would actually be polling ----------------------------------------

    def test_the_session_it_polls_is_one_whose_own_json_says_a_turn_is_running(self) -> None:
        """Criterion 4 and 5's fidelity: the page's poll exists only while a turn runs.

        `src/ummanu/web/pages.py` installs the 3000 ms `setInterval` inside `if (__RUNNING__)`
        and clears it when the turn ends, so an idle session is one no browser is polling. Here the
        overview lists an idle session first and a running one second, and the running one has to
        be the target — chosen by reading each candidate's own JSON at the route that would be
        polled, not by believing the overview's markup.
        """
        idle, running = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
        base = self.serve(listed=((idle, False), (running, True)))
        # Selection and the actual polling route are the subject here. A whole measurement's
        # exit also depends on this host meeting the token 50 ms schedule, which is unrelated.
        # CadenceAndLaunchTests and the verdict tests keep that separate contract intact.
        scenario = measure.prepare(base, None)
        self.assertEqual(scenario.poll_target, f"/po/api/sessions/{running}")
        self.assertIn(f"whose own JSON reports a running turn ({running})", scenario.poll_explanation)
        self.assertFalse(scenario.poll_substitute)
        answered = threading.Event()
        fetch = measure.fetch
        def observe_poll(*args, **kwargs):
            sample = fetch(*args, **kwargs)
            if args[1] == scenario.poll_target:
                answered.set()
            return sample
        with mock.patch.object(measure, "fetch", side_effect=observe_poll), measure.SessionPoll(
                base, scenario.poll_target, scenario.cookie) as poll:
            self.assertTrue(answered.wait(5), "the selected running session was never polled")
        poll.check()
        self.assertGreaterEqual(poll.successes, 1)
        polled = [path for _method, path in self.server.seen if path.startswith("/po/api/")]  # type: ignore[attr-defined]
        # The idle one was read once, to find out that it was idle, and never again.
        self.assertEqual(sum(1 for path in polled if path.endswith(idle)), 1, polled)
        self.assertGreater(sum(1 for path in polled if path.endswith(running)), 1, polled)

    def test_an_installation_with_only_idle_sessions_judges_nothing_and_exits_two(self) -> None:
        """The observer's ruling: do not substitute an idle session silently.

        Polling an idle session would put a request no `/po` page makes beside the four, and report
        it under the heading of the scenario the thresholds judge. The warm numbers are real and are
        printed; nothing is judged; the line says a turn was what was missing, not a session.
        """
        base = self.serve(listed=(("cccccccc-0000-0000-0000-000000000003", False),))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("/po poll: none", text)
        self.assertIn("none of them has a turn running", text)
        self.assertIn("NOT MEASURED", text)
        self.assertIn("NOT JUDGED", text)
        self.assertNotIn("MEETS", text)
        self.assertNotIn("EXCEEDS", text)
        self.assertNotIn("concurrent GET /", text)
        for route in measure.WARM_ROUTES:
            self.assertIn(f"warm p95 GET {route}", text)
        # It read that session once to find out, and then stopped: no poll cadence was started.
        polled = [path for _method, path in self.server.seen if path.startswith("/po/api/")]  # type: ignore[attr-defined]
        self.assertEqual(len(polled), 1, polled)

    def test_asked_for_it_an_idle_installation_is_measured_beside_a_labelled_substitute(self) -> None:
        """`--poll-idle-session`: the largest session JSON stands in, and every number says so.

        The flag is the operator's choice, never a default, so the ruling above still holds for a
        run that did not ask. The largest document is the heaviest read the page's poll makes on
        the installation; an idle session answers `running: false` throughout, and that is not
        reported as a turn ending.
        """
        small, large = "dddddddd-0000-0000-0000-000000000004", "eeeeeeee-0000-0000-0000-000000000005"
        base = self.serve(listed=((small, False), (large, False)))
        original = self.server.plan  # type: ignore[attr-defined]

        def padded(method: str, path: str) -> tuple[int, str, float, str]:
            status, body, delay, location = original(method, path)
            if path.endswith(large):
                body = json.dumps({**json.loads(body), "feed": ["x" * 4096]})
            return status, body, delay, location

        self.server.plan = padded  # type: ignore[attr-defined]
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base, "--poll-idle-session")

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertIn(f"/po poll: GET /po/api/sessions/{large}", text)
        self.assertIn("SUBSTITUTE", text)
        self.assertNotIn("no lighter than an open /po page", text)
        self.assertNotIn("the turn ended", text)
        concurrent = [line for line in text.splitlines() if "concurrent GET / #" in line]
        self.assertEqual(len(concurrent), measure.CONCURRENT_REQUESTS, text)
        self.assertTrue(all("substitute poll" in line for line in concurrent), concurrent)
        polled = [path for _method, path in self.server.seen if path.startswith("/po/api/")]  # type: ignore[attr-defined]
        self.assertEqual(sum(1 for path in polled if path.endswith(small)), 1, polled)
        self.assertGreater(sum(1 for path in polled if path.endswith(large)), measure.CONCURRENT_ROUNDS)

    def test_the_substitute_is_in_the_json_and_a_running_turn_still_wins(self) -> None:
        idle, running = "ffffffff-0000-0000-0000-000000000006", "abababab-0000-0000-0000-000000000007"
        for listed, target, substitute in (
            (((idle, False),), idle, True),
            (((idle, False), (running, True)), running, False),
        ):
            with self.subTest(substitute=substitute):
                base = self.serve(listed=listed)
                with mock.patch.object(measure, "WARM_REQUESTS", 2):
                    code, document = self.run_main(base, "--poll-idle-session", "--json")
                self.assertEqual(code, measure.EXIT_MET, document)
                parsed = json.loads(document)
                self.assertEqual(parsed["poll_target"], f"/po/api/sessions/{target}")
                self.assertIs(parsed["poll_substitute"], substitute)

    def test_the_flag_does_not_invent_a_session_where_none_is_open(self) -> None:
        base = self.serve(listed=())
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base, "--poll-idle-session")
        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("lists no open session to poll", text)
        self.assertNotIn("MEETS", text)

    def test_a_session_document_it_cannot_read_running_out_of_is_unmeasurable(self) -> None:
        """A guess about `running` would be a guess about whether the scenario was reproduced."""
        for body, why in (
            ('{"kind": "po_session", "session": {}}', "no running field"),
            ('{"kind": "po_session", "running": "yes"}', "running is not a boolean"),
            ("not json at all", "not a document"),
            ("[]", "not an object"),
        ):
            with self.subTest(why=why):
                base = self.serve(session_body=body)
                with mock.patch.object(measure, "WARM_REQUESTS", 2):
                    code, text = self.run_main(base)
                self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
                self.assertIn("/po/api/sessions/", text)
                self.assertNotIn("MEETS", text)
                self.assertNotIn("concurrent GET /", text)

    def test_no_session_prints_the_warm_half_judges_nothing_and_exits_two(self) -> None:
        """The concurrent scenario cannot be reproduced without a session, so it is not reported.

        An installation with no open PO session is not broken, and the warm numbers taken from it
        are real — so they are printed. What cannot happen is a verdict: four requests against an
        idle dashboard are a different scenario, and reporting them under the same heading, green,
        is the hole this whole round is about. Round 1's decision allowed it; round 3's withdrew it.
        """
        base = self.serve(listed=())
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("/po poll: none", text)
        self.assertIn("lists no open session to poll", text)
        self.assertIn("NOT MEASURED", text)
        self.assertNotIn("MEETS", text)
        self.assertNotIn("EXCEEDS", text)
        # The warm half is still there, unjudged, and no concurrent round was taken at all.
        for route in measure.WARM_ROUTES:
            self.assertIn(f"warm p95 GET {route}", text)
        self.assertIn("NOT JUDGED", text)
        self.assertNotIn("concurrent GET /", text)
        polled = [path for _method, path in self.server.seen if path.startswith("/po/api/")]  # type: ignore[attr-defined]
        self.assertEqual(polled, [])

    def test_the_concurrent_scenario_is_repeated_and_every_round_is_printed(self) -> None:
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertEqual(measure.CONCURRENT_ROUNDS, 3)
        for round_number in range(1, measure.CONCURRENT_ROUNDS + 1):
            self.assertIn(f"  round {round_number}: ", text)
        # Exactly one round is the judged one, and the four judged rows say which.
        self.assertEqual(text.count("<- judged"), 1)
        self.assertIn(f"(worst of {measure.CONCURRENT_ROUNDS} rounds)", text)

    def test_the_threshold_is_judged_on_the_worst_round_not_the_first(self) -> None:
        """A scenario that breaches 2.0 s in one round of three has not met it.

        The observer's finding on the first submission: the same unchanged installation produced
        17 s, 27 s and 38 s on three runs, so a single round could have reported either verdict.
        Here round one is fast and the later rounds are slow, and the run must come out red.
        """
        # Raw GET counts include discarded rounds, so they cannot decide which *accepted* round
        # is fast. Supply measured rounds here; the real polling/collection mechanism is covered
        # by CadenceAndLaunchTests and RoundsUnderThePollTests. Judge, rendering and exit stay real.
        def measured_rounds(scenario, report):
            for index, duration_ms in enumerate((10, measure.CONCURRENT_THRESHOLD_MS + 200,
                                                 measure.CONCURRENT_THRESHOLD_MS + 400)):
                started = float(index * 10)
                report.concurrent.append(measure.Round(
                    samples=[measure.Sample(route="/", status=200, started_at=started,
                                            ended_at=started + duration_ms / 1000)
                             for _ in range(measure.CONCURRENT_REQUESTS)],
                    launched_at=started, polls_in_flight=1,
                    in_flight_at_start=[1] * measure.CONCURRENT_REQUESTS))
            measure._judge_worst_round(scenario, report)

        base = self.serve()
        with mock.patch.object(measure, "_measure", side_effect=measured_rounds):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_EXCEEDED, text)
        rounds = re.findall(r"^  round (\d+): (.*?) ms \[([^];]+); polls in flight at each start: ([\d, ]+); \d+ during the round\]( <- judged)?$", text, re.MULTILINE)
        self.assertEqual(len(rounds), measure.CONCURRENT_ROUNDS, text)
        fastest = max(float(value) for value in rounds[0][1].split(", "))
        self.assertLess(fastest, measure.CONCURRENT_THRESHOLD_MS, "round one was meant to be fast")
        self.assertNotEqual(rounds[0][4], " <- judged", "the fast first round must not be the judged one")
        self.assertEqual(rounds[-1][4], " <- judged", "the slowest round must be judged")
        for index in range(1, measure.CONCURRENT_REQUESTS + 1):
            self.assertIn(f"concurrent GET / #{index} of {measure.CONCURRENT_REQUESTS}", text)
        self.assertIn("EXCEEDS", text)

    def test_a_po_overview_that_does_not_answer_is_unmeasurable_not_no_session(self) -> None:
        """The reviewer's first reproduction: a 404 from `/po` used to exit 0 as "no session"."""
        base = self.serve(missing=(measure.PO_OVERVIEW,))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn(measure.PO_OVERVIEW, text)
        self.assertIn("404", text)
        self.assertNotIn("MEETS", text)
        self.assertNotIn("WITHOUT the poll", text)

    def test_a_selected_session_that_does_not_answer_is_unmeasurable(self) -> None:
        """The reviewer's second reproduction: `/po` lists a session whose JSON then 404s.

        The four requests would still have been made, and would still have produced numbers — but
        not the numbers this scenario is defined as, because nothing was being polled beside them.
        """
        base = self.serve(session_status=404)
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("/po/api/sessions/", text)
        self.assertIn("404", text)
        self.assertNotIn("MEETS", text)

    def test_every_round_says_it_was_launched_by_a_poll_and_what_was_in_flight(self) -> None:
        """The two per-round facts, and both are conditions now.

        Launched by a due poll is program order inside the poll thread. The four requests are
        released at that poll's start, so each of them starts while it is in flight, even against
        this stub, which answers a session read in a millisecond: the count at every start is at
        least one on every counted round. (It used to be read after the poll had answered, and was
        0 here; review 4 of secretary-1746 refused that as a lighter load than the scenario.)
        """
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        rounds = re.findall(r"^  round (\d+): (.*?) ms \[([^];]+); polls in flight at each start: ([\d, ]+); \d+ during the round\]( <- judged)?$", text, re.MULTILINE)
        self.assertEqual(len(rounds), measure.CONCURRENT_ROUNDS, text)
        for number, _durations, launched, at_start, _judged in rounds:
            with self.subTest(round=number):
                self.assertEqual(launched, "launched by a due poll", text)
                counts = [int(value) for value in at_start.split(", ")]
                self.assertEqual(len(counts), measure.CONCURRENT_REQUESTS)
                self.assertGreaterEqual(min(counts), 1, text)
        self.assertIn("a round counts only when a poll was in flight at every request's start", text)
        polled = re.search(r"polled successfully (\d+) time\(s\)", text)
        self.assertIsNotNone(polled, text)
        self.assertGreaterEqual(int(polled.group(1)), measure.CONCURRENT_ROUNDS)

    def test_the_launch_rule_refuses_a_round_no_poll_was_issued_for_and_allows_an_empty_flight(
        self,
    ) -> None:
        """The rule, tested apart from the mechanism that satisfies it.

        This replaces an assertion this card is changing: `require_overlap` used to refuse a round
        with `overlapping_polls == 0`. `require_launch` is about the launch alone, which is program
        order: a round nothing launched is refused, and one with nothing in flight is not refused
        *here* — `collect_rounds` has already set such a round aside and taken another
        (`RoundsUnderThePollTests`).
        """
        sample = measure.Sample(route="/", status=200, started_at=10.0, ended_at=11.0)
        launched = measure.Round(samples=[sample], launched_at=9.5, polls_in_flight=1)
        alone = measure.Round(samples=[sample], launched_at=9.5, polls_in_flight=0)
        barren = measure.Round(samples=[sample], launched_at=None, polls_in_flight=0)

        measure.require_launch([launched, alone])
        with self.assertRaises(measure.Unmeasurable) as refused:
            measure.require_launch([launched, barren, alone])
        self.assertIn("round(s) 2", str(refused.exception))
        self.assertIn("without a selected-session poll being issued", str(refused.exception))

    def test_a_poll_counts_as_in_flight_only_while_it_actually_was(self) -> None:
        """Interval overlap, from recorded windows: started before the end, ended after the start."""
        poll = measure.SessionPoll("http://127.0.0.1:1", "/po/api/sessions/x", "c")
        poll.windows = [(0.0, 1.0), (5.0, 6.0), (9.5, 10.5), (20.0, 21.0)]

        # A round from 10.0 to 12.0: the third poll was still in flight when it began.
        self.assertEqual(poll.overlapping(10.0, 12.0), 1)
        # One that finished before the round started does not count, nor one that began after it.
        self.assertEqual(poll.overlapping(7.0, 9.0), 0)
        self.assertEqual(poll.overlapping(0.5, 21.0), 4)

        # A poll still under way has no recorded end yet, and counts from the moment it started:
        # it cannot have finished before a window it has not finished at all.
        poll._pending = {1: 11.5}
        self.assertEqual(poll.overlapping(11.0, 12.0), 1)
        # A round that had closed before that poll started still does not count it.
        self.assertEqual(poll.overlapping(11.0, 11.4), 0)

    def test_a_late_404_on_the_concurrent_reads_exits_two_after_clean_warm_reads(self) -> None:
        """The reviewer's reproduction: 21 clean warm reads, then 404 on every concurrent read.

        `measure_concurrent` was the one call site that never had the status rule at all, so those
        four failures were recorded as four very fast durations and judged as a green result.
        """
        warm = 2
        base = self.serve(status_after=("/", 1 + warm + 1, 404))
        with mock.patch.object(measure, "WARM_REQUESTS", warm):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("404", text)
        self.assertNotIn("MEETS", text)
        # The warm half did succeed, and is shown — with no verdict beside it.
        self.assertIn("warm p95 GET /", text)
        self.assertIn("NOT JUDGED", text)

    def test_a_late_contained_500_on_the_concurrent_reads_exits_two(self) -> None:
        """The other half of the same reproduction: the transport's own contained 500."""
        warm = 2
        base = self.serve(status_after=("/", 1 + warm + 1, 500))
        with mock.patch.object(measure, "WARM_REQUESTS", warm):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("500", text)
        self.assertNotIn("MEETS", text)

    # -- what it cannot measure --------------------------------------------------------------

    def test_an_unreachable_installation_exits_two_rather_than_reporting_a_number(self) -> None:
        code, text = self.run_main("http://127.0.0.1:1")

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("not reachable", text)
        self.assertNotIn("MEETS", text)

    def test_a_missing_route_exits_two_rather_than_timing_a_404(self) -> None:
        base = self.serve(missing=("/projects",))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("/projects", text)
        self.assertIn("404", text)


class CadenceAndLaunchTests(AgainstAStubDashboard):
    """Criterion 7: the cadence and the launch, proved together, at the real three seconds.

    This is the class the four previous rounds of this work did not have. The properties it holds
    are the two that survive on any installation, and the history of this file is the history of
    trying to hold something stronger. The cadence went first: a version that woke the poll thread
    when a round was armed ran its rounds back to back, fired the polls 2 ms apart, and still
    printed "every 3 s". The next version fixed that by releasing the poll and the round from one
    barrier together — and bought a proof that is a coin toss, because with a 2 ms poll and a 3 ms
    round whether the two HTTP requests are genuinely simultaneous is up to the scheduler. One run
    in five refused a perfectly good measurement on it.

    So what is asserted here is what is deterministic, as the rework decision of this round set it:
    no interval shorter than the cadence, and every round launched by a poll that fell due and was
    issued before the round was released. How much of a round genuinely had a poll in flight is
    measured and reported, and asserted only where it is a consequence rather than a hope — in the
    slow case, where a poll necessarily falls due inside the round.

    Both are taken off one report, at `POLL_INTERVAL_SECONDS` itself rather than a convenient
    stand-in, and twice over: against a dashboard that answers in milliseconds, which is the case
    the mechanism has to survive, and against one slow enough that a round outlasts an interval,
    which is the installation this sprint starts from.

    These cases are slow by construction. A round waits for the poll that falls due next, so three
    rounds cost about two intervals of waiting however fast the dashboard is. That waiting is the
    proof, not an overhead to be tuned away; every other case in this file shortens the interval
    instead, and says so.
    """

    def assert_on_schedule(self, report: object) -> None:
        """Every start against its own schedule, both ways, and every gap between starts too."""
        offsets = report.poll_offsets  # type: ignore[attr-defined]
        self.assertGreaterEqual(
            len(offsets), measure.CONCURRENT_ROUNDS, "each round is released by its own due poll"
        )
        for index, offset in enumerate(offsets, start=1):
            with self.subTest(poll=index):
                self.assertLessEqual(
                    abs(offset),
                    measure.CADENCE_TOLERANCE_SECONDS,
                    f"a poll started {offset * 1000:+.1f} ms against its schedule",
                )
        # Two-sided on the gaps as well: an absolute schedule keeps every gap within two
        # tolerances of the interval, so neither the old early-wake defect (gaps of ~2 ms) nor the
        # stretch this round removes (gaps as long as a slow read) could pass.
        for index, spacing in enumerate(report.poll_intervals, start=1):  # type: ignore[attr-defined]
            with self.subTest(interval=index):
                self.assertAlmostEqual(
                    spacing,
                    measure.POLL_INTERVAL_SECONDS,
                    delta=2 * measure.CADENCE_TOLERANCE_SECONDS,
                    msg=f"poll {index + 1} started {spacing:.4f} s after poll {index}",
                )
        self.assertTrue(measure.cadence_holds(offsets), offsets)

    def assert_cadence_and_launch(self, report: object) -> None:
        """Both properties, off one report, with every observed start checked individually."""
        self.assertEqual(measure.POLL_INTERVAL_SECONDS, 3.0, "the cadence under test is the /po page's own")
        self.assert_on_schedule(report)

        self.assertEqual(len(report.concurrent), measure.CONCURRENT_ROUNDS)  # type: ignore[attr-defined]
        for index, item in enumerate(report.concurrent, start=1):  # type: ignore[attr-defined]
            with self.subTest(round=index):
                self.assertIsNotNone(
                    item.launched_at, f"round {index} ran without a poll being issued for it"
                )
                # The ordering, read back off the recorded times rather than trusted: the poll this
                # round was released by started before the round's first request did.
                self.assertLess(
                    item.launched_at,
                    item.started_at,
                    f"round {index} started before the poll that was supposed to launch it",
                )
        measure.require_launch(list(report.concurrent))  # type: ignore[attr-defined]

    def test_a_dashboard_answering_in_milliseconds_keeps_the_cadence_and_the_launch(self) -> None:
        """The fast case. The mechanism has to hold here, because this is where the sprint is going.

        The whole run is taken once and asserted on twice: the intervals and the launches come off
        the same report, so there is no arrangement in which one of them was true at a different
        moment than the other. The four requests are released at the launching poll's start, so
        even here, where a session read takes a millisecond, each of them starts under the poll.
        """
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            report = measure.run(base, None)

        slowest = max(sample.duration_ms for item in report.concurrent for sample in item.samples)
        self.assertLess(slowest, 1000.0, "this stub is meant to answer in milliseconds")
        self.assert_cadence_and_launch(report)
        self.assertTrue(all(measurement.met for measurement in report.measurements))
        # Released at the poll's start: every request started while it was in flight.
        for item in report.concurrent:
            self.assertGreaterEqual(min(item.in_flight_at_start), 1, item.in_flight_at_start)
            self.assertGreaterEqual(item.polls_in_flight, 1)
        text = "\n".join(measure.render(report))
        self.assertIn("the poll started every 3 s", text)
        self.assertIn("observed spacing:", text)
        self.assertIn("[launched by a due poll; polls in flight at each start: 1, 1, 1, 1;", text)

    def test_a_round_that_outlasts_the_interval_keeps_both_properties_too(self) -> None:
        """The slow case: a round longer than one interval, which is where this sprint starts.

        A poll necessarily falls due in the middle of every round here, so this is where an
        in-flight count above zero is a consequence rather than a hope, and it is asserted — and
        the spacing is still the cadence, because a poll's schedule is anchored on the previous
        poll rather than on whatever the dashboard is doing.
        """
        warm = 1
        # GETs on `/`: one warm-up, `warm` timed ones, then the rounds. The rounds are the slow
        # ones, so the warm half of this case costs nothing.
        first_concurrent_get = 1 + warm + 1
        base = self.serve(slow_get_after=("/", first_concurrent_get, measure.POLL_INTERVAL_SECONDS + 0.2))
        with mock.patch.object(measure, "WARM_REQUESTS", warm):
            report = measure.run(base, None)

        for index, item in enumerate(report.concurrent, start=1):
            with self.subTest(round=index):
                self.assertGreater(
                    item.ended_at - item.started_at,
                    measure.POLL_INTERVAL_SECONDS,
                    "this round was supposed to outlast one poll interval",
                )
                self.assertGreaterEqual(
                    item.polls_in_flight,
                    1,
                    "a poll fell due inside this round, so one was genuinely in flight during it",
                )
        self.assert_cadence_and_launch(report)
        self.assertIn("the poll started every 3 s", "\n".join(measure.render(report)))

    def test_a_run_whose_polls_were_faster_than_the_cadence_is_refused_by_the_command(self) -> None:
        """The first defect put back deliberately, to show what the command now does with it.

        `arm` used to set the event the poll thread was waiting on, so polls fired back to back.
        The seam that decides when a poll starts is `_sleep_until`; making it return after two
        milliseconds instead of at the schedule reproduces that run. What is asserted is the
        outcome: exit 2, the numbers printed and unjudged, and no sentence claiming a cadence.
        """

        def hurried(poll: object, _due: float) -> bool:
            time.sleep(0.002)
            return not poll._stop.is_set()  # type: ignore[attr-defined]

        base = self.serve()
        with (
            mock.patch.object(measure.SessionPoll, "_sleep_until", hurried),
            mock.patch.object(measure, "WARM_REQUESTS", 2),
        ):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("early against its 3 s schedule", text)
        self.assertIn("the 3 s cadence did NOT hold", text)
        self.assertNotIn("the poll started every", text)
        self.assertNotIn("MEETS", text)
        self.assertIn("NOT JUDGED", text)

    def test_the_cadence_rule_is_two_sided_on_every_start(self) -> None:
        """The rule itself, apart from the mechanism that satisfies it.

        This replaces an assertion this card changed this round. The rule used to be a lower bound
        on intervals only — `min(intervals) >= interval - tolerance` — which accepted any stretch,
        and a stretch is a lighter load than the page's. The rework decision made it two-sided on
        starts, so both directions are asserted here: early is refused, late is refused, and a run
        with fewer polls than rounds is not a cadence at all.
        """
        tolerance = measure.CADENCE_TOLERANCE_SECONDS
        measure.require_cadence([0.0, 0.004, -0.001, tolerance])

        with self.assertRaises(measure.Unmeasurable) as late:
            measure.require_cadence([0.001, 0.002, 0.120 + tolerance])
        self.assertIn("late", str(late.exception))

        with self.assertRaises(measure.Unmeasurable) as early:
            measure.require_cadence([0.0, -2.998, -5.996])
        self.assertIn("early", str(early.exception))

        with self.assertRaises(measure.Unmeasurable) as few:
            measure.require_cadence([0.0])
        self.assertIn("fewer than one per round", str(few.exception))

    def test_the_output_claims_the_cadence_only_where_the_starts_show_it(self) -> None:
        """Criterion 4's second half: the sentence and the refusal read the same predicate."""
        interval = measure.POLL_INTERVAL_SECONDS
        held = "\n".join(measure.cadence_lines([interval, interval + 0.01], [0.001, 0.002, 0.011]))
        self.assertIn("the poll started every 3 s", held)
        self.assertIn("observed spacing:", held)
        self.assertIn("start against schedule: furthest +11.0 ms", held)

        stretched = "\n".join(measure.cadence_lines([3.5, 3.5], [0.0, 0.5, 1.0]))
        self.assertNotIn("the poll started every", stretched)
        self.assertIn("did NOT hold", stretched)

        self.assertNotIn("every 3 s", "\n".join(measure.cadence_lines([], [])))


class SlowReadsAndEndedTurnsTests(AgainstAStubDashboard):
    """The two things a real `/po` page does that a quiet stub never shows.

    Both are run at a scaled cadence: the property is about the timer's control flow, not about
    three seconds, and the real interval is already proved in `CadenceAndLaunchTests`.
    """

    def test_a_session_read_slower_than_the_interval_does_not_stretch_the_cadence(self) -> None:
        """The reviewer's reproduction, which the previous version failed.

        The page's `setInterval(async () => { await fetch(...) }, 3000)` starts a read every
        interval whether or not the last one has answered. A poll loop that waited for each answer
        started its next poll only after a slow read came back, so every gap became as long as the
        read and the load fell below the page's while the output said the cadence held. Here the
        session endpoint is slower than the interval, and the starts still have to be on schedule —
        which means the reads overlap each other, and that is asserted too.
        """
        for interval, read in ((0.05, 0.12), (0.2, 0.5)):
            with self.subTest(interval=interval, read=read):
                base = self.serve(delays={f"/po/api/sessions/{self.SESSION}": read})
                with (
                    mock.patch.object(measure, "POLL_INTERVAL_SECONDS", interval),
                    mock.patch.object(measure, "WARM_REQUESTS", 2),
                ):
                    report = measure.run(base, None)

                self.assertTrue(measure.cadence_holds(report.poll_offsets), report.poll_offsets)
                # The discriminating number: the old loop's gaps were at least one read long.
                self.assertLess(max(report.poll_intervals), read, report.poll_intervals)
                # Reads overlapped each other, as they do under the page's timer.
                windows = report.poll_windows
                self.assertTrue(
                    any(later[0] < earlier[1] for earlier, later in zip(windows, windows[1:], strict=False)),
                    "no read was still outstanding when the next one started",
                )
                for index, item in enumerate(report.concurrent, start=1):
                    self.assertIsNotNone(item.launched_at, f"round {index}")
                    self.assertLess(item.launched_at, item.started_at, f"round {index}")

    def test_a_turn_that_ends_mid_run_is_reported_and_the_run_still_stands(self) -> None:
        """The observer's ruling on an ended turn: detect it, say so, do not abort.

        When the turn ends the real page clears its timer, which is zero load. This keeps polling
        on schedule, which is more than zero, so the load stays at or above the page's and a MEETS
        is still sound — refusing would only turn a sound result into an unmeasurable one. What is
        required is that the run notices: every poll's `running` is read, and one line names the
        round the turn ended in and says polling continued.
        """
        # Read 1 is the selection probe; read 2 is the first poll; from read 3 on the turn is over.
        base = self.serve(running_reads=2)
        with (
            mock.patch.object(measure, "POLL_INTERVAL_SECONDS", 0.05),
            mock.patch.object(measure, "WARM_REQUESTS", 2),
        ):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertRegex(
            text,
            r"the turn ended (before|during|after) round \d \(a poll answered running: false\); "
            r"polling continued on schedule, at least as heavy as the page",
        )
        self.assertEqual(text.count("the turn ended"), 1, text)

    def test_a_turn_that_stays_running_prints_no_ended_line(self) -> None:
        base = self.serve()
        with (
            mock.patch.object(measure, "POLL_INTERVAL_SECONDS", 0.05),
            mock.patch.object(measure, "WARM_REQUESTS", 2),
        ):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertNotIn("the turn ended", text)
        self.assertIn(
            "this is a load no lighter than an open /po page on a running turn, so a number that "
            "meets its threshold under it is sound, and one that exceeds it may be conservative",
            text,
        )

    def test_where_the_turn_ended_is_named_against_the_rounds(self) -> None:
        sample = measure.Sample(route="/", status=200, started_at=10.0, ended_at=11.0)
        later = measure.Sample(route="/", status=200, started_at=20.0, ended_at=21.0)
        rounds = [
            measure.Round(samples=[sample], launched_at=9.9, polls_in_flight=0),
            measure.Round(samples=[later], launched_at=19.9, polls_in_flight=0),
        ]
        self.assertEqual(measure.turn_ended_phrase(None, rounds), "")
        self.assertEqual(measure.turn_ended_phrase(5.0, rounds), "before round 1")
        self.assertEqual(measure.turn_ended_phrase(10.5, rounds), "during round 1")
        self.assertEqual(measure.turn_ended_phrase(15.0, rounds), "before round 2")
        self.assertEqual(measure.turn_ended_phrase(30.0, rounds), "after round 2")


class RoundsUnderThePollTests(AgainstAStubDashboard):
    """secretary-1746 review 4: the four requests start while a poll is in flight, or do not count.

    The token interval keeps these cases short; the cadence itself is proved at its real three
    seconds in `CadenceAndLaunchTests`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(measure, "POLL_INTERVAL_SECONDS", 0.05))
        self.enterContext(mock.patch.object(measure, "WARM_REQUESTS", 2))

    def test_the_requests_start_before_the_launching_poll_has_answered(self) -> None:
        """The reviewer's case: a session read that takes a while is still under way at every start."""
        base = self.serve(delays={f"/po/api/sessions/{self.SESSION}": 0.3})
        report = measure.run(base, None)

        self.assertEqual(len(report.concurrent), measure.CONCURRENT_ROUNDS)
        windows = dict(report.poll_windows)
        for number, item in enumerate(report.concurrent, start=1):
            with self.subTest(round=number):
                launch_end = windows[item.launched_at]
                for sample in item.samples:
                    self.assertLess(item.launched_at, sample.started_at)
                    self.assertLess(sample.started_at, launch_end, "the request started after the poll answered")
                self.assertGreaterEqual(min(item.in_flight_at_start), 1)

    def test_a_round_whose_requests_started_with_no_poll_in_flight_is_taken_again(self) -> None:
        base = self.serve()
        original = measure.SessionPoll.in_flight_at
        calls = {"count": 0}

        def first_round_alone(poll: object, moment: float) -> int:
            calls["count"] += 1
            if calls["count"] <= measure.CONCURRENT_REQUESTS:
                return 0
            return original(poll, moment)  # type: ignore[arg-type]

        with mock.patch.object(measure.SessionPoll, "in_flight_at", first_round_alone):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertEqual(len(re.findall(r"^  round \d+: ", text, re.MULTILINE)), measure.CONCURRENT_ROUNDS, text)
        not_counted = re.findall(r"^  not counted: .*polls in flight at each start: 0, 0, 0, 0;", text, re.MULTILINE)
        self.assertEqual(len(not_counted), 1, text)

    def test_a_run_that_never_gets_a_round_under_the_poll_is_unmeasurable(self) -> None:
        base = self.serve()
        with mock.patch.object(measure.SessionPoll, "in_flight_at", lambda _poll, _moment: 0):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn(f"of {measure.MAX_CONCURRENT_ATTEMPTS} rounds had a poll in flight", text)
        self.assertNotIn("MEETS", text)
        self.assertEqual(
            len(re.findall(r"^  not counted: ", text, re.MULTILINE)), measure.MAX_CONCURRENT_ATTEMPTS, text
        )

    def test_the_no_poll_baseline_asks_nothing_of_po_and_says_so(self) -> None:
        base = self.serve()
        with mock.patch.object(measure, "po_cookie", side_effect=AssertionError("no token is read")):
            code, text = self.run_main(base, "--no-poll")

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertIn("/po poll: none", text)
        self.assertIn("--no-poll", text)
        rounds = re.findall(r"^  round \d+: .* ms \[no poll\]", text, re.MULTILINE)
        self.assertEqual(len(rounds), measure.CONCURRENT_ROUNDS, text)
        concurrent = [line for line in text.splitlines() if "concurrent GET / #" in line]
        self.assertEqual(len(concurrent), measure.CONCURRENT_REQUESTS, text)
        self.assertTrue(all("no poll" in line for line in concurrent), concurrent)
        asked = [path for _method, path in self.server.seen if path.startswith("/po")]  # type: ignore[attr-defined]
        self.assertEqual(asked, [])

    def test_the_json_carries_the_in_flight_counts_and_the_rounds_not_counted(self) -> None:
        base = self.serve()
        code, document = self.run_main(base, "--json")
        self.assertEqual(code, measure.EXIT_MET, document)
        parsed = json.loads(document)
        self.assertIs(parsed["no_poll"], False)
        self.assertEqual(len(parsed["concurrent_polls_in_flight_at_start"]), measure.CONCURRENT_ROUNDS)
        for counts in parsed["concurrent_polls_in_flight_at_start"]:
            self.assertEqual(len(counts), measure.CONCURRENT_REQUESTS)
            self.assertGreaterEqual(min(counts), 1)
        self.assertIsInstance(parsed["concurrent_discarded_rounds_ms"], list)


class DataDirectoryResolutionTests(unittest.TestCase):
    """Where the documented command finds the installation, with nothing in the environment.

    The reviewer ran `python3 scripts/measure_dashboard.py` in an ordinary checkout shell on this
    host. That shell does not inherit the service unit's `UMMANU_DATA_DIR` or
    `UMMANU_INSTANCE`, so the script resolved no data directory, could not read the PO token,
    and measured the concurrency without the poll while an open session existed. It now falls
    through to the instance the CLI itself defaults to, read with the product's own resolution.
    """

    def instance(self) -> tuple[Path, Path]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = root / "data"
        data.mkdir()
        instance = root / "instance"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {data}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        return instance, data

    def test_with_neither_variable_set_it_falls_through_to_the_default_instance(self) -> None:
        instance, data = self.instance()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("ummanu.onboarding.DEFAULT_INSTANCE", str(instance)),
        ):
            resolved, source = measure.resolve_data_dir(None)

        self.assertEqual(resolved, data)
        self.assertIn("the default instance", source)
        self.assertIn(str(instance), source)

    def test_the_documented_order_is_the_argument_then_the_two_variables(self) -> None:
        instance, data = self.instance()
        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(instance)}, clear=True):
            resolved, source = measure.resolve_data_dir(None)
            self.assertEqual(resolved, data)
            self.assertIn("UMMANU_INSTANCE", source)

        with mock.patch.dict(os.environ, {"UMMANU_DATA_DIR": "/from/env"}, clear=True):
            self.assertEqual(measure.resolve_data_dir(None), (Path("/from/env"), "UMMANU_DATA_DIR"))
            # The argument still wins over both.
            self.assertEqual(measure.resolve_data_dir("/from/flag"), (Path("/from/flag"), "--data-dir"))

    def test_an_unresolvable_installation_is_refused_rather_than_silently_dropped(self) -> None:
        """`except Exception: return None` was the same quiet downgrade a third time."""
        with (
            mock.patch.dict(os.environ, {"UMMANU_INSTANCE": "/nowhere/at/all"}, clear=True),
            self.assertRaises(measure.Unmeasurable) as refused,
        ):
            measure.resolve_data_dir(None)

        self.assertIn("/nowhere/at/all", str(refused.exception))
        self.assertIn("--data-dir", str(refused.exception))


class MeasurementScriptDocumentationTests(unittest.TestCase):
    """The command an operator is told to run is the command that exists."""

    COMMAND: ClassVar[str] = "python3 scripts/measure_dashboard.py"

    def test_operations_names_the_command_and_the_three_places_the_durations_land(self) -> None:
        text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

        self.assertIn(self.COMMAND, text)
        self.assertIn("journalctl -u ummanu-web.service", text)
        self.assertIn("ummanu status", text)

    def test_the_script_adds_no_dependency(self) -> None:
        """Criterion 9: standard library only, and nothing new in `pyproject.toml`."""
        source = SCRIPT.read_text(encoding="utf-8")
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(ast.parse(source, filename=str(SCRIPT)))
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(source, filename=str(SCRIPT)))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        # `ummanu` is this repository, not a dependency: the PO cookie rule is imported from the
        # product rather than copied. Everything else has to be in the standard library.
        outside = imported - set(sys.stdlib_module_names) - {"ummanu", ""}
        self.assertEqual(outside, set())


if __name__ == "__main__":
    unittest.main()
