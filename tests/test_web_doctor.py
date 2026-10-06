"""The doctor lamp on the bottom bar, the page behind it, and the rule that decides its colour.

Three questions, and each is asked of the real thing. The colour rule is asked of
:func:`~ummanu.webproto.reads.health_summary` and its severity table, because a colour is
decided from a code and not from a sentence. The cost is asked of :class:`
~ummanu.web.doctor.DoctorLayer` over a counting collector and a clock this test moves, because
the lamp is on every page and the collection behind it is not cheap. And "recorded state only" is
asked of the real read layer over a real instance, with the ways out of this process -- a
subprocess, a socket, an HTTP request, the `ummanu doctor` entry point -- taken away for the
span of the read, so the assertion is about the path taken rather than about a comment.
"""

from __future__ import annotations

import re
import threading
import time
import unittest
from typing import Any
from unittest import mock

from tests.web_fakes import Recording, system_snapshot
from tests.webproto_sprint_fixtures import SprintProtocolFixture
from ummanu.web import pages
from ummanu.web.app import ROUTES, WebApp
from ummanu.web.commands import health_layers
from ummanu.web.doctor import CACHE_SECONDS, DOCTOR_NOT_BUILT, DoctorLayer
from ummanu.webproto.errors import InstallationUnavailable
from ummanu.webproto.reads import (
    PROBLEM_SEVERITY,
    UNCLASSIFIED_SEVERITY,
    ReadLayer,
    health_summary,
    lamp_colour,
    problem_severity,
)

NOW = 1_800_000_000.0

#: A status document that trips every problem `health_summary` can report, so the suite can assert
#: over the whole set rather than over the handful somebody remembered.
EVERY_PROBLEM: dict[str, Any] = {
    "host": {
        "units": [
            {"name": "a.service", "kind": "service", "present": True, "active": "failed"},
            {"name": "b.service", "kind": "service", "present": False, "active": None},
        ],
        "inventory_errors": {"units": "systemctl timed out"},
    },
    "dispatcher": {"pause": {"paused": True, "mode": "drain"}, "divergences": {"open_count": 2}},
    "checkpoint": {
        "checkpoint_status": "failed",
        "checkpoint_last_failure_reason": "push refused",
        "blocked_reason": "no remote",
        "rpo_exceeded": True,
        "unpublished_minutes": 45,
        "rpo_reason": "checkpoint gate blocked since 2026-09-20T11:15:00Z: no remote",
    },
    "secret_store": {"installation_key": {"present": True, "usable": False}},
    "memory": {"index_present": False},
}

PAUSED_ONLY: dict[str, Any] = {"dispatcher": {"pause": {"paused": True, "mode": "drain"}}}

NOTHING_WRONG: dict[str, Any] = {
    "host": {"units": [{"name": "a.timer", "kind": "timer", "present": True, "active": "active"}]}
}


def available() -> dict[str, Any]:
    return {
        "state": "available",
        "reason": None,
        "data_age_seconds": 0.0,
        "observed_at": "2026-09-20T12:00:00Z",
    }


def health_snapshot(status: dict[str, Any]) -> dict[str, Any]:
    """What `ReadLayer.health_snapshot` answers with, for a collector that answered."""
    return {
        "schema_version": 1,
        "kind": "health",
        "observed_at": "2026-09-20T12:00:00Z",
        "health": {"source": available(), "status": health_summary(status), "doctor": {"state": "available", "findings": [], "run_at": "2026-09-20T11:59:30Z"}},
    }


def unreadable_snapshot(reason: str) -> dict[str, Any]:
    """What it answers with when the collector itself could not be read."""
    return {
        "schema_version": 1,
        "kind": "health",
        "observed_at": "2026-09-20T12:00:00Z",
        "health": {
            "source": {"state": "unavailable", "reason": reason, "data_age_seconds": None},
            "status": None,
        },
    }


# -- criteria 1 and 2: every problem carries a code, and the code decides the colour -------------


class TheColourRuleTests(unittest.TestCase):
    def codes(self, status: dict[str, Any]) -> list[str]:
        return [finding["code"] for finding in health_summary(status)["findings"]]

    def test_every_problem_the_summary_can_report_is_classified_by_its_code(self) -> None:
        codes = self.codes(EVERY_PROBLEM)
        self.assertEqual(
            codes,
            [
                "unit.failed",
                "unit.missing",
                "host.inventory_unreadable",
                "pipeline.paused",
                "dispatcher.divergences_open",
                "checkpoint.blocked",
                "checkpoint.last_failed",
                "checkpoint.rpo_exceeded",
                "secret_store.key_unusable",
                "memory.index_missing",
            ],
        )
        for code in codes:
            with self.subTest(code=code):
                self.assertIn(code, PROBLEM_SEVERITY, "a problem nobody classified")
                self.assertIn(PROBLEM_SEVERITY[code], {"red", "yellow"})

    def test_the_severity_of_each_code_is_the_one_the_card_names(self) -> None:
        red = {
            "unit.failed",
            "unit.missing",
            "checkpoint.blocked",
            "checkpoint.last_failed",
            "checkpoint.rpo_exceeded",
            # Reported by doctor only (ummanu-21): history on the snapshot branch nobody but the
            # exporter should have made.
            "snapshot.foreign_commit",
            "secret_store.key_unusable",
            "health.unreadable",
            "doctor.collection_stuck",
        }
        yellow = {
            "pipeline.paused",
            "dispatcher.divergences_open",
            "host.inventory_unreadable",
            "memory.index_missing",
            # ummanu-108: one subscription is red; its roles run on the other family meanwhile.
            "provider.red",
        }
        self.assertEqual({code for code, s in PROBLEM_SEVERITY.items() if s == "red"}, red)
        self.assertEqual({code for code, s in PROBLEM_SEVERITY.items() if s == "yellow"}, yellow)

    def test_nothing_wrong_is_green_and_a_yellow_problem_alone_is_yellow(self) -> None:
        self.assertEqual(health_summary(NOTHING_WRONG)["colour"], "green")
        self.assertEqual(health_summary(PAUSED_ONLY)["colour"], "yellow")

    def test_one_red_problem_makes_the_colour_red_however_many_yellow_ones_there_are(self) -> None:
        self.assertEqual(health_summary(EVERY_PROBLEM)["colour"], "red")
        just_red = {
            "memory": {"index_present": False},
            "secret_store": {"installation_key": {"present": True, "usable": False}},
        }
        self.assertEqual(health_summary(just_red)["colour"], "red")

    def test_the_colour_keys_on_the_code_and_not_on_the_wording(self) -> None:
        """A sentence may be reworded tomorrow; the colour must not move when it is."""
        reworded = [{"code": "unit.failed", "message": "something else entirely"}]
        self.assertEqual(lamp_colour(reworded), "red")
        self.assertEqual(lamp_colour([{"code": "pipeline.paused", "message": "x"}]), "yellow")

    def test_a_code_nobody_classified_is_never_green(self) -> None:
        self.assertNotEqual(UNCLASSIFIED_SEVERITY, "green")
        self.assertEqual(problem_severity("something.nobody.classified"), UNCLASSIFIED_SEVERITY)
        self.assertNotEqual(lamp_colour([{"code": "something.nobody.classified"}]), "green")


# -- criterion 3: the document this card adds to keeps every promise it already made --------------


class TheSummaryStaysAdditiveTests(unittest.TestCase):
    def test_state_and_problems_keep_their_shape_and_their_order(self) -> None:
        summary = health_summary(EVERY_PROBLEM)
        self.assertEqual(summary["state"], "attention")
        self.assertEqual(summary["problems"], [finding["message"] for finding in summary["findings"]])
        self.assertTrue(all(isinstance(problem, str) for problem in summary["problems"]))
        self.assertEqual(summary["problems"][0], "a.service is failed")
        self.assertEqual(health_summary(NOTHING_WRONG)["state"], "ok")
        self.assertEqual(health_summary(NOTHING_WRONG)["problems"], [])


# -- criterion 7: a test per colour, and the one that cannot be read ------------------------------


class TheLayerColoursTests(unittest.TestCase):
    def layer(self, answer: Any) -> DoctorLayer:
        def read() -> dict[str, Any]:
            if isinstance(answer, Exception):
                raise answer
            return answer

        return DoctorLayer(read, now=lambda: NOW)

    def test_green(self) -> None:
        document = self.layer(health_snapshot(NOTHING_WRONG)).doctor_snapshot()
        self.assertEqual(document["colour"], "green")
        self.assertEqual(document["problems"], [])
        self.assertTrue(document["readable"])

    def test_yellow(self) -> None:
        document = self.layer(health_snapshot(PAUSED_ONLY)).doctor_snapshot()
        self.assertEqual(document["colour"], "yellow")
        self.assertEqual([problem["code"] for problem in document["problems"]], ["pipeline.paused"])

    def test_red(self) -> None:
        document = self.layer(health_snapshot(EVERY_PROBLEM)).doctor_snapshot()
        self.assertEqual(document["colour"], "red")
        self.assertIn("unit.failed", [problem["code"] for problem in document["problems"]])

    def test_health_that_cannot_be_read_is_red_and_says_so(self) -> None:
        document = self.layer(
            InstallationUnavailable("this instance config does not validate")
        ).doctor_snapshot()
        self.assertEqual(document["colour"], "red")
        self.assertFalse(document["readable"])
        self.assertEqual([problem["code"] for problem in document["problems"]], ["health.unreadable"])
        self.assertIn("does not validate", document["reason"])

    def test_a_collector_that_refused_inside_the_section_is_red_too(self) -> None:
        document = self.layer(unreadable_snapshot("the production state is unreadable")).doctor_snapshot()
        self.assertEqual(document["colour"], "red")
        self.assertFalse(document["readable"])
        self.assertEqual(document["reason"], "the production state is unreadable")


# -- the transport: the lamp, the page, the cost --------------------------------------------------


class TransportFixture(unittest.TestCase):
    """The application over recording layers, with a doctor layer this test decides the answer of."""

    def setUp(self) -> None:
        self.collected = 0
        self.status: dict[str, Any] | Exception = NOTHING_WRONG
        self.clock = NOW
        self.doctor = DoctorLayer(self.read_health, now=lambda: self.clock)

    def read_health(self) -> dict[str, Any]:
        self.collected += 1
        if isinstance(self.status, Exception):
            raise self.status
        return health_snapshot(self.status)

    def app(self, *, doctor: Any = ...) -> WebApp:
        unreadable = InstallationUnavailable("not part of this test")
        return WebApp(
            Recording(system_snapshot=system_snapshot()),
            Recording(run_list={"items": []}),
            Recording(sprint_list={"kind": "sprint_list", "sprints": {"source": available(), "items": []}}),
            Recording(),
            Recording(pause_state=unreadable),
            Recording(),
            Recording(
                command_history={
                    "kind": "command_history",
                    "observed_at": "2026-09-20T12:00:00Z",
                    "limit": 25,
                    "commands": {"source": available(), "items": [], "has_more": False, "next_cursor": None},
                }
            ),
            Recording(),
            doctor=self.doctor if doctor is ... else doctor,
        )

    def get(self, path: str, *, app: WebApp | None = None) -> str:
        response = (app or self.app()).handle("GET", path)
        self.assertEqual(response.status, 200, f"{path} answered {response.status}")
        return response.body.decode("utf-8")

    def lamp(self, page: str) -> str:
        found = re.findall(r'<a class="lamp lamp-\w+" href="/doctor"[^>]*>.*?</a>', page)
        self.assertEqual(len(found), 1, "a page carries exactly one lamp")
        return found[0]


class TheDoctorPageTests(TransportFixture):
    def test_the_page_lists_every_problem_with_its_code_and_marks_what_makes_it_red(self) -> None:
        self.status = EVERY_PROBLEM
        page = self.get("/doctor")
        self.assertIn("Red — the installation cannot be trusted to run work", page)
        self.assertIn("Yellow — running, but a person should look", page)
        red = page.index("Red — the installation")
        yellow = page.index("Yellow — running")
        self.assertLess(red, yellow, "what makes the lamp red is read first")
        for code, message in (
            ("unit.failed", "a.service is failed"),
            ("secret_store.key_unusable", "the secret store&#x27;s installation key is not usable"),
            ("pipeline.paused", "the pipeline is paused (drain)"),
            ("memory.index_missing", "the memory index is missing"),
        ):
            with self.subTest(code=code):
                self.assertIn(f"<code>{code}</code>", page)
                self.assertIn(message, page)
        # The red codes are inside the red group and not merely somewhere on the page.
        self.assertLess(page.index("unit.failed"), yellow)
        self.assertGreater(page.index("pipeline.paused"), yellow)

    def test_no_problem_at_all_is_said_plainly(self) -> None:
        page = self.get("/doctor")
        self.assertIn("no problem is recorded for this installation", page)
        self.assertIn("lamp lamp-green", page)

    def test_health_that_could_not_be_read_says_so_with_the_reason_and_is_not_a_clean_page(self) -> None:
        self.status = InstallationUnavailable("the dispatcher state is unreadable")
        page = self.get("/doctor")
        self.assertIn("this installation's health could not be read.", page)
        self.assertIn("the dispatcher state is unreadable", page)
        self.assertIn("health.unreadable", page)
        self.assertNotIn("no problem is recorded", page)
        self.assertIn("lamp lamp-red", page)

    def test_a_process_built_without_the_doctor_layer_says_that_rather_than_drawing_green(self) -> None:
        app = self.app(doctor=None)
        page = self.get("/doctor", app=app)
        self.assertIn(DOCTOR_NOT_BUILT, page)
        self.assertNotIn("no problem is recorded", page)
        self.assertIn("lamp lamp-red", self.lamp(page))

    def test_a_doctor_layer_that_refuses_is_the_page_content_and_never_the_page_status(self) -> None:
        """Why `/doctor` is exempt from the transport's code-to-status walk (`test_web_transport`).

        Every other route answers a refused read with the status of its code. This one cannot: a
        503 refusal page can say that something refused, but not that the *installation's health*
        is unknown, which is the one thing this page exists to say -- and it would leave the reader
        with no page at the exact moment the lamp went red.
        """
        app = self.app(doctor=Recording(doctor_snapshot=InstallationUnavailable("no instance here")))
        response = app.handle("GET", "/doctor")
        self.assertEqual(response.status, 200)
        page = response.body.decode("utf-8")
        self.assertIn("no instance here", page)
        self.assertIn("health.unreadable", page)
        self.assertIn("lamp lamp-red", page)
        self.assertNotIn("no problem is recorded", page)

    def test_the_page_says_what_it_reads_and_what_it_does_not(self) -> None:
        page = self.get("/doctor")
        self.assertIn("latest periodically recorded", page)
        self.assertIn("opens no SSH and touches no provider", page)


class TheLampTests(TransportFixture):
    def test_the_lamp_carries_the_colour_of_the_reading_and_links_to_the_page(self) -> None:
        for status, colour in ((NOTHING_WRONG, "green"), (PAUSED_ONLY, "yellow"), (EVERY_PROBLEM, "red")):
            with self.subTest(colour=colour):
                self.status = status
                self.doctor = DoctorLayer(self.read_health, now=lambda: self.clock)
                lamp = self.lamp(self.get("/"))
                self.assertIn(f'class="lamp lamp-{colour}"', lamp)
                self.assertIn('href="/doctor"', lamp)

    def test_the_lamp_counts_the_problems_behind_the_colour_and_counts_nothing_when_green(self) -> None:
        self.status = PAUSED_ONLY
        self.assertIn('<span class="lamp-count">1</span>', self.lamp(self.get("/")))
        self.status = NOTHING_WRONG
        self.doctor = DoctorLayer(self.read_health, now=lambda: self.clock)
        self.assertNotIn("lamp-count", self.lamp(self.get("/history")))


class TheLampCostsOneCollectionTests(TransportFixture):
    def page_paths(self) -> list[str]:
        placeholders = {"ref": "ummanu-9", "project": "ummanu", "session": "s-1", "run_id": "r-1"}
        paths = []
        for route in ROUTES:
            if not (route.page and route.method == "GET") or route.pattern.startswith("/po"):
                continue
            paths.append(re.sub(r"\{([a-z_]+)\}", lambda m: placeholders[m.group(1)], route.pattern))
        return paths

    def test_many_pages_across_many_routes_collect_once_per_cache_window(self) -> None:
        app = self.app()
        paths = [path for path in self.page_paths() if path in {"/", "/history", "/sprints", "/doctor"}]
        for _ in range(6):
            for path in paths:
                self.get(path, app=app)
        self.assertEqual(len(paths) * 6, 24, "the walk really did render many pages")
        self.assertEqual(self.collected, 1, "one collection served every one of them")

        self.clock += CACHE_SECONDS - 1
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(self.collected, 1, "still inside the window the layer decided")

        self.clock += 2
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(self.collected, 2, "one more collection, not one per page")

    def test_a_json_route_costs_no_health_collection_at_all(self) -> None:
        app = self.app()
        for path in ("/api/system", "/api/pause", "/api/history"):
            app.handle("GET", path)
        self.assertEqual(self.collected, 0)

    def test_the_doctor_page_and_its_own_lamp_are_one_collection_rather_than_two(self) -> None:
        self.get("/doctor")
        self.assertEqual(self.collected, 1)


# -- criterion 6: recorded state only, over the real read layer ----------------------------------


class RecordedStateOnlyTests(SprintProtocolFixture):
    """The read path, driven for real, with every way out of this process taken away."""

    def test_the_lamp_reads_the_recorded_collector_and_nothing_else(self) -> None:
        asked: list[str] = []

        def status_reader() -> dict[str, Any]:
            asked.append("collect_status")
            return EVERY_PROBLEM

        reads = ReadLayer(self.instance, data_dir=self.data_dir, status_reader=status_reader, offline=True)
        layer = DoctorLayer(reads.health_snapshot, now=lambda: NOW)

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the doctor lamp left this process")

        with (
            mock.patch("subprocess.run", refuse),
            mock.patch("subprocess.Popen", refuse),
            mock.patch("subprocess.check_output", refuse),
            mock.patch("socket.socket", refuse),
            mock.patch("urllib.request.urlopen", refuse),
            mock.patch("ummanu.cli.run_doctor", refuse),
            mock.patch("ummanu.cli.collect_doctor_inspection", refuse),
        ):
            document = layer.doctor_snapshot()
            rendered = pages.doctor({"available": True, "reason": None, "document": document})

        self.assertEqual(asked, ["collect_status"], "one recorded-state read, and no second source")
        self.assertEqual(document["colour"], "red")
        self.assertIn("unit.failed", rendered)

    def test_the_read_layer_asks_its_collector_for_recorded_state_and_not_for_the_live_host(self) -> None:
        """`health_snapshot` is `collect_status` over this host's files: no sprints, no probes."""
        seen: list[dict[str, Any]] = []

        def collect(report: Any, **kwargs: Any) -> dict[str, Any]:
            seen.append(kwargs)
            return NOTHING_WRONG

        reads = ReadLayer(self.instance, data_dir=self.data_dir, offline=True)
        with mock.patch("ummanu.webproto.reads.collect_status", collect):
            snapshot = reads.health_snapshot()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["offline"])
        self.assertFalse(seen[0]["sprints"])
        self.assertFalse(seen[0]["probe_panels"])
        self.assertEqual(snapshot["health"]["status"]["colour"], "green")


# -- one cached reading: the dashboard's panel and the lamp ---------------------------------------


class OneReadingTests(SprintProtocolFixture):
    """The dashboard's health panel reads the lamp's cache, wired as `web-serve` wires it.

    One cache, one window: the real read layer and doctor layer from `health_layers`, over the real
    instance, with the collector counted and its answer changed inside the window.
    """

    def setUp(self) -> None:
        super().setUp()
        self.clock = NOW
        from ummanu.infra.doctor_record import RESULT_PATH, identity, publish, utc
        (self.data_dir / RESULT_PATH).parent.mkdir(parents=True)
        publish(self.data_dir / RESULT_PATH, {
            "schema_version": 1, "installation": identity(self.instance, self.data_dir),
            "run_at": utc(NOW), "completed_at": utc(NOW), "exit_code": 0,
            "outcome": "result", "reason": None, "mode": "live",
            "result": {"schema_version": 1, "ok": True, "findings": []},
        })
        self.collected = 0
        self.status: dict[str, Any] = NOTHING_WRONG

        def collect(report: Any, **kwargs: Any) -> dict[str, Any]:
            self.collected += 1
            return self.status

        self.enterContext(mock.patch("ummanu.webproto.reads.collect_status", collect))
        self.reads, self.doctor = health_layers(
            str(self.instance), data_dir=str(self.data_dir), offline=True, now=lambda: self.clock
        )

    def stored(self, *, findings=None, **changes):
        import json

        from ummanu.infra.doctor_record import RESULT_PATH, publish

        path = self.data_dir / RESULT_PATH
        document = json.loads(path.read_text())
        if findings is not None:
            document["result"]["findings"] = findings
            document["result"]["ok"] = not findings
            document["exit_code"] = 1 if findings else 0
        document.update(changes)
        publish(path, document)
        self.doctor._cached = None

    def test_union_preserves_status_and_doctor_findings_identity_and_the_recorded_run_time(self):
        from ummanu.infra.doctor_record import utc
        finding = {"code": "recovery_bypass", "message": "ambient Git credential configuration exists",
                   "capability": "checkpoint-git-authentication", "kind": "credential-helper"}
        self.stored(findings=[finding])
        doctor_only = self.doctor.doctor_snapshot()
        self.assertEqual(doctor_only["colour"], "yellow")
        self.assertEqual(doctor_only["problems"][0]["capability"], finding["capability"])
        for path in ("/", "/doctor"):
            page = self.app().handle("GET", path).body.decode()
            self.assertIn("lamp lamp-yellow", page)
            self.assertIn(utc(NOW), page)
        self.assertIn(finding["capability"], self.app().handle("GET", "/doctor").body.decode())
        self.status = EVERY_PROBLEM
        self.clock += CACHE_SECONDS + 1
        combined = self.doctor.doctor_snapshot()
        self.assertEqual(combined["colour"], "red")
        self.assertIn("unit.failed", [problem["code"] for problem in combined["problems"]])
        self.assertIn("recovery_bypass", [problem["code"] for problem in combined["problems"]])
        self.assertEqual(combined["doctor_run_at"], utc(NOW))
        self.assertNotEqual(combined["observed_at"], combined["doctor_run_at"])
        self.assertEqual(self.panel()["combined"]["findings"], combined["problems"])
        self.stored(findings=[{"code": "a_future_doctor_code", "message": "a new finding", "name": "thing"}])
        self.assertEqual(self.doctor.doctor_snapshot()["colour"], "red")
        self.status = NOTHING_WRONG
        self.doctor._cached = None
        self.assertEqual(self.doctor.doctor_snapshot()["colour"], "yellow")

    def test_missing_corrupt_expired_failed_wrong_installation_and_unavailable_are_explicit(self):
        from ummanu.infra.doctor_record import RESULT_PATH, utc
        self.status = EVERY_PROBLEM
        path = self.data_dir / RESULT_PATH
        original = path.read_bytes()
        cases = (
            ("unknown", None), ("malformed", b"{broken"),
            ("stale", {"run_at": utc(NOW - 181), "completed_at": utc(NOW - 180)}),
            ("failed", {"outcome": "failed", "reason": "deadline exceeded", "result": None, "exit_code": None}),
            ("unknown", {"outcome": "collecting", "reason": "unfinished attempt", "completed_at": None, "result": None, "exit_code": None}),
            ("wrong_installation", {"installation": {"instance": "another", "data_dir": "another"}}),
            ("unavailable", {"outcome": "unavailable", "reason": "bus unavailable", "exit_code": 2}),
        )
        for expected, value in cases:
            with self.subTest(expected=expected):
                path.write_bytes(original)
                if value is None:
                    path.unlink()
                elif isinstance(value, bytes):
                    path.write_bytes(value)
                else:
                    self.stored(**value)
                self.doctor._cached = None
                document = self.doctor.doctor_snapshot()
                self.assertEqual(document["doctor"]["state"], expected)
                self.assertEqual(document["colour"], "red")
                self.assertIn("unit.failed", [item["code"] for item in document["problems"]])
                if expected == "unknown":
                    self.assertNotIn("health.unreadable", [item["code"] for item in document["problems"]])
                    self.assertIn("unknown / not yet collected", self.app().handle("GET", "/doctor").body.decode())
                else:
                    self.assertIn("recorded doctor is " + expected, self.app().handle("GET", "/doctor").body.decode())

    def test_periodic_producer_preserves_baseline_and_cached_pages_through_completion(self):
        from ummanu.infra import doctor_record as records

        finding = {"code": "recovery_bypass", "message": "ambient credential configuration",
                   "capability": "checkpoint-git-authentication"}
        self.stored(findings=[finding])
        app = self.app()
        before = self.doctor.doctor_snapshot()
        self.clock += CACHE_SECONDS + 1

        def collect(command, **kwargs):
            during = self.doctor.doctor_snapshot()
            self.assertEqual(during["colour"], "yellow")
            self.assertEqual(during["problems"], before["problems"])
            self.assertEqual(during["doctor_run_at"], before["doctor_run_at"])
            self.assertEqual(self.panel()["combined"]["findings"], before["problems"])
            for route in ("/", "/doctor"):
                page = app.handle("GET", route).body.decode()
                self.assertIn("lamp lamp-yellow", page)
                self.assertIn("checkpoint-git-authentication", page)
                self.assertIn("run in progress since " + records.utc(self.clock), page)
                self.assertNotIn("health.unreadable", page)
            self.clock += 2
            return 1, {"schema_version": 1, "ok": False,
                       "findings": [{"code": "unit.failed", "message": "a.service is failed"}]}

        with mock.patch.object(records.time, "time", side_effect=lambda: self.clock), mock.patch.object(records, "collect", collect):
            self.assertEqual(records.record(self.instance, data_dir=self.data_dir), 0)
        self.assertEqual(self.doctor.doctor_snapshot()["problems"], before["problems"])
        self.clock += CACHE_SECONDS + 1
        for route in ("/", "/doctor"):
            page = app.handle("GET", route).body.decode()
            self.assertIn("lamp lamp-red", page)
            self.assertIn("unit.failed", page)
            self.assertNotIn("run in progress since", page)
        self.assertEqual(self.panel()["combined"]["findings"], self.doctor.doctor_snapshot()["problems"])
        self.assertEqual(self.collected, 3)

    def test_initial_and_stuck_collecting_states_are_shared_by_dashboard_lamp_and_doctor(self):
        from ummanu.infra import doctor_record as records

        path = self.data_dir / records.RESULT_PATH
        path.unlink()
        app = self.app()
        self.assertEqual(self.doctor.doctor_snapshot()["colour"], "unknown")
        start = self.clock
        records.publish(path, {
            "schema_version": 2, "installation": records.identity(self.instance, self.data_dir),
            "completed": None, "collecting": {"run_at": records.utc(start), "mode": "live"},
        })
        for elapsed in (0, records.STUCK_SECONDS, records.STUCK_SECONDS + .01):
            self.clock = start + elapsed
            self.doctor._cached = None
            stuck = elapsed > records.STUCK_SECONDS
            colour = "red" if stuck else "unknown"
            document = self.doctor.doctor_snapshot()
            self.assertEqual(document["colour"], colour)
            self.assertEqual([item["code"] for item in document["problems"]], ["doctor.collection_stuck"] if stuck else [])
            self.assertEqual(self.panel()["combined"]["colour"], colour)
            for route in ("/", "/doctor"):
                page = app.handle("GET", route).body.decode()
                self.assertIn("lamp lamp-" + colour, page)
                self.assertIn("unknown / not yet collected", page)
                self.assertIn("run in progress since " + records.utc(start), page)
                self.assertNotIn("health.unreadable", page)
                self.assertNotIn("nothing needs attention", page)

    def panel(self) -> dict[str, Any]:
        return self.reads.system_snapshot()["installation"]["health"]

    def app(self) -> WebApp:
        unreadable = InstallationUnavailable("not part of this test")
        return WebApp(
            self.reads,
            Recording(run_list={"items": []}),
            Recording(sprint_list={"kind": "sprint_list", "sprints": {"source": available(), "items": []}}),
            Recording(),
            Recording(pause_state=unreadable),
            Recording(),
            Recording(),
            Recording(),
            doctor=self.doctor,
        )

    def test_the_panel_and_the_lamp_are_one_reading_within_a_window_and_move_together(self) -> None:
        panel = self.panel()
        self.assertEqual(panel, self.doctor.health_snapshot()["health"])
        self.assertEqual(self.doctor.doctor_snapshot()["colour"], "green")
        self.assertEqual(self.collected, 1, "the dashboard and the lamp shared one collection")

        self.status = EVERY_PROBLEM
        self.clock += CACHE_SECONDS - 1
        self.assertEqual(self.panel(), panel, "inside the window the panel keeps the lamp's reading")
        self.assertEqual(self.doctor.doctor_snapshot()["colour"], "green")
        self.assertEqual(self.collected, 1)

        self.clock += 2
        # The lamp is asked first this time: whichever reader opens the window, both see its reading.
        self.assertEqual(self.doctor.doctor_snapshot()["colour"], "red")
        self.assertEqual(self.panel()["status"], health_summary(EVERY_PROBLEM))
        self.assertEqual(self.panel(), self.doctor.health_snapshot()["health"])
        self.assertEqual(self.collected, 2)

    def test_the_rendered_dashboard_and_its_lamp_cannot_disagree(self) -> None:
        app = self.app()
        first = app.handle("GET", "/").body.decode("utf-8")
        self.assertIn("nothing needs attention.", first)
        self.assertIn("lamp lamp-green", first)

        self.status = EVERY_PROBLEM
        self.clock += CACHE_SECONDS - 1
        for path in ("/", "/doctor", "/projects"):
            app.handle("GET", path)
        page = app.handle("GET", "/").body.decode("utf-8")
        self.assertIn("nothing needs attention.", page)
        self.assertNotIn("a.service is failed", page)
        self.assertIn("lamp lamp-green", page)
        self.assertEqual(self.collected, 1, "every page of the window was one collection")

        self.clock += 2
        page = app.handle("GET", "/").body.decode("utf-8")
        self.assertIn("a.service is failed", page)
        self.assertIn("lamp lamp-red", page)
        self.assertEqual(self.collected, 2)

    def test_concurrent_requests_over_an_expired_cache_share_exactly_one_collection(self) -> None:
        """The reviewer's shape: a fixed clock, an expired cache, requests racing a blocked collector.

        Each collection answers with a status of its own, so a second collection would show as a
        page carrying a different reading than the others.
        """
        app = self.app()
        app.handle("GET", "/")
        self.assertEqual(self.collected, 1)
        self.clock += CACHE_SECONDS + 1  # expired, and fixed from here on

        entered = threading.Event()
        release = threading.Event()
        answers = [PAUSED_ONLY, EVERY_PROBLEM, NOTHING_WRONG, NOTHING_WRONG]

        def blocked(report: Any, **kwargs: Any) -> dict[str, Any]:
            self.collected += 1
            answer = answers[min(self.collected - 2, len(answers) - 1)]
            entered.set()
            self.assertTrue(release.wait(10), "the collector was never released")
            return answer

        pages_seen: dict[int, str] = {}

        def request(index: int) -> None:
            pages_seen[index] = app.handle("GET", "/").body.decode("utf-8")

        with mock.patch("ummanu.webproto.reads.collect_status", blocked):
            first = threading.Thread(target=request, args=(0,))
            first.start()
            self.assertTrue(entered.wait(10), "the first request never reached the collector")
            others = [threading.Thread(target=request, args=(index,)) for index in range(1, 4)]
            for thread in others:
                thread.start()
            # Give the others time to reach the lock while the collection is still in flight.
            time.sleep(0.2)
            release.set()
            for thread in (first, *others):
                thread.join(10)
                self.assertFalse(thread.is_alive())

        self.assertEqual(self.collected, 2, "one collection at start, and exactly one for the expiry")
        self.assertEqual(len(pages_seen), 4)
        for index, page in pages_seen.items():
            with self.subTest(request=index):
                # Every request received the one reading: paused only, a yellow lamp, one problem.
                self.assertIn("the pipeline is paused (drain)", page)
                self.assertNotIn("a.service is failed", page)
                self.assertIn("lamp lamp-yellow", page)

    def test_one_response_renders_its_panel_and_its_lamp_from_one_reading_across_an_expiry(
        self,
    ) -> None:
        """The window expires mid-request, after the panel was read and before the lamp is drawn."""
        test = self

        class ExpiringPause:
            """The pause read, which the dashboard makes between its snapshot and its page."""

            def pause_state(self) -> dict[str, Any]:
                test.clock += CACHE_SECONDS + 1
                test.status = EVERY_PROBLEM
                raise InstallationUnavailable("not part of this test")

        app = self.app()
        app.pause_reads = ExpiringPause()

        page = app.handle("GET", "/").body.decode("utf-8")

        self.assertEqual(self.collected, 1, "the lamp did not look the reading up a second time")
        self.assertIn("nothing needs attention.", page)
        self.assertIn("lamp lamp-green", page)
        self.assertNotIn("a.service is failed", page)

        # The next request is a new response, and it takes the new window's reading for both.
        app.pause_reads = Recording(pause_state=InstallationUnavailable("not part of this test"))
        page = app.handle("GET", "/").body.decode("utf-8")
        self.assertEqual(self.collected, 2)
        self.assertIn("a.service is failed", page)
        self.assertIn("lamp lamp-red", page)

    def test_a_published_reading_is_not_changed_by_what_a_reader_does_with_it(self) -> None:
        self.doctor.health_snapshot()["health"]["status"]["problems"].append("scribbled")
        self.doctor.doctor_snapshot()["problems"].append({"code": "scribbled"})

        self.assertNotIn("scribbled", self.doctor.health_snapshot()["health"]["status"]["problems"])
        self.assertEqual(self.doctor.doctor_snapshot()["problems"], [])
        self.assertEqual(self.collected, 1)

    def test_health_that_cannot_be_read_is_unavailable_in_the_panel_and_red_in_the_lamp(self) -> None:
        def refuse(report: Any, **kwargs: Any) -> dict[str, Any]:
            self.collected += 1
            raise OSError("production state is unreadable")

        with mock.patch("ummanu.webproto.reads.collect_status", refuse):
            panel = self.panel()
            lamp = self.doctor.doctor_snapshot()
        self.assertEqual(panel["source"]["state"], "unavailable")
        self.assertIn("production state is unreadable", panel["source"]["reason"])
        self.assertEqual(lamp["colour"], "red")
        self.assertEqual(self.collected, 1)


if __name__ == "__main__":
    unittest.main()
