"""The web preview serves every page from synthetic layers, writes nothing and survives a broken page."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from scripts import web_preview_fixtures as fixtures
from scripts.web_preview import PreviewApp
from ummanu.web.app import WebApp


class WebPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = PreviewApp()

    def get(self, target: str):
        path, _, query = target.partition("?")
        return self.app.handle("GET", path, query=query)

    def test_every_preview_page_renders(self) -> None:
        for target in fixtures.PAGES:
            with self.subTest(target=target):
                response = self.get(target)
                self.assertEqual(response.status, 200, response.body[-400:])
                self.assertIn(b"<html", response.body)

    def test_the_rendering_is_deterministic(self) -> None:
        self.assertEqual(self.get(f"/tasks/{fixtures.SHOWCASE}").body, self.get(f"/tasks/{fixtures.SHOWCASE}").body)

    def test_the_open_sprint_dod_interleaves_met_and_open_items(self) -> None:
        dod = fixtures.sprint_state("sprint:41")["sprint"]["value"]["dod"]
        states = [item["state"] for item in dod["items"]]
        self.assertEqual(len(states), 9)
        self.assertIn(["met", "open", "open", "met"], [states[index : index + 4] for index in range(6)])
        self.assertIn("in_main", [item["delivery"]["state"] for item in dod["items"] if item["state"] == "open"])

    def test_a_post_is_refused_before_any_layer_runs(self) -> None:
        with mock.patch.object(WebApp, "_handle") as routed:
            for path in ("/api/tasks/acme-api-231/comment", "/api/pause/drain", "/sprints", "/po/sessions"):
                with self.subTest(path=path):
                    response = self.app.handle("POST", path, body=b'{"body": "x"}')
                    self.assertEqual(response.status, 405)
                    self.assertIn(b"preview_read_only", response.body)
        routed.assert_not_called()

    def test_a_mutation_reaching_a_layer_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "changes nothing"):
            fixtures.PreviewOps().run_start("acme-api-231", request_id="r", profile="p", instruction="")

    def test_a_page_that_raises_is_an_error_page(self) -> None:
        with (
            mock.patch.object(fixtures, "task_snapshot", side_effect=RuntimeError("planted")),
            contextlib.redirect_stderr(io.StringIO()) as logged,
        ):
            response = self.get(f"/tasks/{fixtures.SHOWCASE}")
        self.assertEqual(response.status, 500)
        self.assertIn(b"RuntimeError: planted", response.body)
        self.assertIn("planted", logged.getvalue())
        self.assertEqual(self.get("/board").status, 200)


if __name__ == "__main__":
    unittest.main()
