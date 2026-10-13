"""The guarded front: what the rendered configuration says, and that no published route escapes it.

Hermetic on purpose. Not one test here starts Caddy, opens a socket or reads the live installation:
the two questions this card has to answer forever -- "is every route the transport publishes behind
authentication" and "does the front proxy anywhere but loopback" -- are questions about a text, and
a test that needed a running front would be a test nobody runs on the branch that breaks it.

The route list is never written out here. `ummanu.web.app.ROUTES` is the same table
`docs/PROTOCOLS.md` documents and the same one `tests/test_web_transport.py` pins, so a route added
there enters these assertions with it, and forgetting one is not a thing anybody can do. What is
written out here instead are the *counter*-examples: configurations that leave a route open, so the
predicate that has to catch them is proved able to fail.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from ummanu.host import (
    SHIPPED_PACKAGING_ROOT,
    SystemdLayout,
    build_plan,
    load_packaged_units,
    render_systemd_unit,
)
from ummanu.web.app import ROUTES, WebApp
from ummanu.web.server import DEFAULT_HOST, DEFAULT_PORT
from ummanu.webfront.caddyfile import (
    SESSION_COOKIE_MAX_AGE,
    SESSION_COOKIE_NAME,
    FrontConfig,
    FrontConfigError,
    render,
    session_cookie_value,
)
from ummanu.webfront.guard import CaddyfileSyntaxError, parse, unguarded_routes, upstreams

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A bcrypt hash of no password anybody holds: the shape `basicauth` checks, and nothing more.
SAMPLE_HASH = "$2a$14$" + "x" * 53
#: A fixture with more entropy-shaped text than the renderer's minimum; never a production secret.
SAMPLE_SESSION_SECRET = "fixture-session-secret-" + "x" * 32

SITES = ("https://front.example", "https://198.51.100.7")


def rendered(**overrides) -> str:
    fields = {
        "sites": SITES,
        "password_hash": SAMPLE_HASH,
        "session_secret": SAMPLE_SESSION_SECRET,
    }
    fields.update(overrides)
    return render(FrontConfig(**fields))


class GuardCoverageTests(unittest.TestCase):
    """Criterion 4: everything exposed is authenticated, and a new route cannot slip out of that."""

    def test_no_published_route_is_answered_without_owner_authentication(self) -> None:
        self.assertEqual(unguarded_routes(rendered(), ROUTES), ())

    def test_the_routes_asked_about_are_the_ones_the_app_answers(self) -> None:
        """Not a copy of the router: every route the guard is asked about is one the app dispatches to."""
        app = WebApp(None, None, None, None, None, None, None, None)
        self.assertTrue(ROUTES)
        for route in ROUTES:
            path = "/" + "/".join(
                f"{part[1:-1]}-1" if part.startswith("{") else part for part in route.segments
            )
            with self.subTest(route=f"{route.method} {route.pattern}"):
                self.assertIs(app.match(route.method, path)[0], route)
                self.assertTrue(callable(getattr(app, f"_{route.handler}", None)))
                self.assertEqual(unguarded_routes(rendered(), [path]), ())

    def test_the_guard_covers_pages_json_events_and_worker_output_alike(self) -> None:
        """The classes criterion 4 names, each one asked about by the path it is served on."""
        text = rendered()
        classes = {
            "dashboard page": "/",
            "card page, which carries worker and reviewer output": "/tasks/ummanu-1",
            "json": "/api/system",
            "events": "/api/tasks/ummanu-1/events",
            "run state": "/api/runs/pr-1",
            "start": "/api/runs/start",
        }
        for name, path in classes.items():
            with self.subTest(name):
                self.assertEqual(unguarded_routes(text, [path]), ())

    def test_the_derived_cookie_matcher_is_itself_an_authentication_guard(self) -> None:
        site = parse(rendered())[0]
        self.assertEqual(site.auth_matchers, frozenset({"@owner_session"}))

    def test_a_wildcard_cookie_value_is_not_treated_as_a_guard(self) -> None:
        expected = session_cookie_value(SAMPLE_SESSION_SECRET)
        forged = rendered().replace(expected, "*")
        self.assertEqual(len(unguarded_routes(forged, ROUTES)), len(ROUTES))

    def test_a_cookie_guard_does_not_hide_an_unguarded_no_cookie_fallback(self) -> None:
        session = session_cookie_value(SAMPLE_SESSION_SECRET)
        leaky = f"""
https://front.example {{
	tls internal
	@owner_session header Cookie *{SESSION_COOKIE_NAME}={session}*
	handle @owner_session {{
		reverse_proxy 127.0.0.1:8787
	}}
	handle /proof-only {{
		basicauth {{
			owner {SAMPLE_HASH}
		}}
		respond 204
	}}
	handle {{
		reverse_proxy 127.0.0.1:8787
	}}
}}
"""
        self.assertEqual(len(unguarded_routes(leaky, ROUTES)), len(ROUTES))

    def test_a_route_left_outside_the_guard_is_reported(self) -> None:
        """The predicate can fail. Without this, a green run above would mean nothing."""
        leaky = f"""
https://front.example {{
	tls internal
	handle /api/tasks/* {{
		reverse_proxy 127.0.0.1:8787
	}}
	basicauth * {{
		owner {SAMPLE_HASH}
	}}
	reverse_proxy 127.0.0.1:8787
}}
"""
        findings = unguarded_routes(leaky, ROUTES)
        self.assertTrue(findings)
        self.assertTrue(all("/api/tasks/" in finding for finding in findings))
        self.assertIn("https://front.example", findings[0])

    def test_a_guard_narrowed_to_some_paths_leaves_the_rest_reported(self) -> None:
        narrow = f"""
https://front.example {{
	@pages path /
	basicauth @pages {{
		owner {SAMPLE_HASH}
	}}
	reverse_proxy 127.0.0.1:8787
}}
"""
        findings = unguarded_routes(narrow, ROUTES)
        self.assertEqual(len(findings), len(ROUTES) - 1)
        self.assertNotIn("GET / is", " ".join(findings))

    def test_a_site_with_no_guard_at_all_reports_every_route(self) -> None:
        open_site = """
https://front.example {
	tls internal
	reverse_proxy 127.0.0.1:8787
}
"""
        self.assertEqual(len(unguarded_routes(open_site, ROUTES)), len(ROUTES))

    def test_an_http_block_that_only_redirects_is_not_a_finding(self) -> None:
        """Criterion 2: http either redirects or does not listen. A redirect serves no content."""
        both = """
http://front.example {
	redir https://front.example{uri} permanent
}
""" + rendered()
        self.assertEqual(unguarded_routes(both, ROUTES), ())

    def test_an_http_block_that_serves_content_is_a_finding(self) -> None:
        leaky = """
http://front.example {
	reverse_proxy 127.0.0.1:8787
}
""" + rendered()
        self.assertEqual(len(unguarded_routes(leaky, ROUTES)), len(ROUTES))

    def test_a_matcher_that_was_never_defined_is_refused_rather_than_assumed(self) -> None:
        with self.assertRaises(CaddyfileSyntaxError):
            unguarded_routes(
                f"https://front.example {{\n\tbasicauth @nowhere {{\n\t\towner {SAMPLE_HASH}\n\t}}\n}}\n",
                ROUTES,
            )


class RenderedConfigTests(unittest.TestCase):
    """Criterion 3: the shape of the file, and the boundary it may not cross."""

    def test_the_only_upstream_is_the_loopback_transport(self) -> None:
        self.assertEqual(set(upstreams(rendered())), {f"{DEFAULT_HOST}:{DEFAULT_PORT}"})

    def test_a_front_that_proxied_off_the_host_is_refused_before_a_file_exists(self) -> None:
        with self.assertRaises(FrontConfigError) as refused:
            rendered(upstream_host="198.51.100.7")
        self.assertIn("loopback", str(refused.exception))

    def test_tls_is_on_and_the_internal_issuer_is_named(self) -> None:
        site = parse(rendered())[0]
        self.assertIn(("tls", ("internal",)), [(d.name, d.args) for d in site.directives])

    def test_a_plain_http_site_address_is_refused(self) -> None:
        with self.assertRaises(FrontConfigError):
            rendered(sites=("http://front.example",))

    def test_a_front_with_no_password_hash_is_refused(self) -> None:
        for value in ("", "not-a-bcrypt-hash", "$2a$14$has space"):
            with self.subTest(value=value), self.assertRaises(FrontConfigError):
                rendered(password_hash=value)

    def test_a_front_with_no_real_session_secret_is_refused(self) -> None:
        for value in ("", "short", "contains whitespace and is long enough to otherwise pass"):
            with self.subTest(value=value), self.assertRaises(FrontConfigError):
                rendered(session_secret=value)

    def test_successful_basic_auth_mints_a_30_day_host_cookie(self) -> None:
        session = session_cookie_value(SAMPLE_SESSION_SECRET)
        text = rendered()
        self.assertIn(f"@owner_session header Cookie *{SESSION_COOKIE_NAME}={session}*", text)
        self.assertIn("handle @owner_session", text)
        self.assertIn("basicauth {", text)
        self.assertIn(
            f'header +Set-Cookie "{SESSION_COOKIE_NAME}={session}; Path=/; '
            f'Max-Age={SESSION_COOKIE_MAX_AGE}; HttpOnly; Secure; SameSite=Strict"',
            text,
        )

    def test_session_secret_rotation_changes_the_browser_session_value(self) -> None:
        other_secret = "fixture-session-secret-" + "y" * 32
        self.assertNotEqual(
            session_cookie_value(SAMPLE_SESSION_SECRET), session_cookie_value(other_secret)
        )

    def test_no_credential_and_no_admin_surface_is_rendered(self) -> None:
        text = rendered()
        self.assertIn("admin off", text)
        self.assertIn("skip_install_trust", text)
        self.assertNotIn("admin localhost", text)

    def test_a_bind_restricts_the_listener_and_is_absent_when_not_asked_for(self) -> None:
        """The rehearsal handle: the same file, listening on loopback only."""
        self.assertNotIn("\tbind ", rendered())
        self.assertIn("\tbind 127.0.0.1", rendered(bind=("127.0.0.1",)))

    def test_the_repository_holds_no_password_and_no_hash(self) -> None:
        """Criterion 6: auth values reach the file at render time and live in the store."""
        for path in sorted((REPO_ROOT / "src" / "ummanu" / "webfront").rglob("*.py")):
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("$2a$", body)
                self.assertNotIn("$2b$", body)


class ShippedUnitTests(unittest.TestCase):
    """Criterion 8 and 3: what the units this installation runs actually start."""

    LAYOUT = SystemdLayout(
        product_root=Path("/opt/product"),
        instance_path=Path("/opt/instance"),
        data_dir=Path("/opt/data"),
        runtime_user="runner",
        runtime_home=Path("/opt/home"),
    )

    def unit(self, name: str) -> str:
        return render_systemd_unit((SHIPPED_PACKAGING_ROOT / name).read_bytes(), self.LAYOUT).decode()

    def test_the_transport_unit_binds_loopback_and_the_documented_port(self) -> None:
        text = self.unit("ummanu-web.service")
        self.assertIn(f"--host {DEFAULT_HOST} --port {DEFAULT_PORT}", text)
        self.assertIn("[Install]", text)
        self.assertIn("Restart=always", text)

    def test_the_front_unit_runs_the_packaged_caddy_against_the_rendered_file(self) -> None:
        text = self.unit("ummanu-web-front.service")
        self.assertIn("ExecStart=/usr/bin/caddy run --adapter caddyfile", text)
        self.assertIn("/opt/data/webfront/Caddyfile", text)
        self.assertIn("Restart=always", text)
        self.assertIn("[Install]", text)

    def test_the_front_gets_the_one_capability_it_needs_and_no_more(self) -> None:
        text = self.unit("ummanu-web-front.service")
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", text)
        self.assertIn("CapabilityBoundingSet=CAP_NET_BIND_SERVICE", text)
        self.assertIn("NoNewPrivileges=true", text)
        self.assertNotIn("User=root", text)

    def test_both_halves_are_planned_for_an_installation_that_does_not_opt_out(self) -> None:
        """What makes `ummanu status` list them: they are components of the shipped catalogue."""
        instance = {
            "host": {"unit_prefix": "ummanu-", "components": {"curator": {"enabled": False}}},
        }
        packaged = load_packaged_units(SHIPPED_PACKAGING_ROOT, "ummanu-", self.LAYOUT)
        planned = {
            resource.name
            for resource in build_plan(instance, [], packaged=packaged)
            if resource.kind == "unit"
        }
        self.assertIn("ummanu-web.service", planned)
        self.assertIn("ummanu-web-front.service", planned)

    def test_an_installation_can_opt_out_of_the_front_and_keep_the_rest(self) -> None:
        """The documented rollback: the components leave the desired state and reconcile removes them."""
        instance = {
            "host": {
                "unit_prefix": "ummanu-",
                "components": {"web": {"enabled": False}, "web-front": {"enabled": False}},
            },
        }
        packaged = load_packaged_units(SHIPPED_PACKAGING_ROOT, "ummanu-", self.LAYOUT)
        planned = {
            resource.name
            for resource in build_plan(instance, [], packaged=packaged)
            if resource.kind == "unit"
        }
        self.assertNotIn("ummanu-web.service", planned)
        self.assertNotIn("ummanu-web-front.service", planned)
        self.assertIn("ummanu-memory.service", planned)

    def test_the_two_halves_start_and_stop_as_one(self) -> None:
        text = self.unit("ummanu-web-front.service")
        self.assertIn("Requires=ummanu-web.service", text)
        self.assertIn("PartOf=ummanu-web.service", text)
        self.assertIn("After=network-online.target ummanu-web.service", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
