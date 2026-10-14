"""Real HTML rendering with file counters; process identity is an injected observation only."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tests.support.card_run_fixtures import record
from tests.web_fakes import Recording
from ummanu.web import commands, pages
from ummanu.web.app import WebApp
from ummanu.webproto import commands as run_commands, run_events, run_state, store_io
from ummanu.webproto.errors import ReadError
from ummanu.webproto.ops import OperationLayer
from ummanu.webproto.runs import ProductRun, RunStore

_COUNTING = None


def _audit(event, args):
    if _COUNTING is None:
        return
    root, counts = _COUNTING
    if event == "open" and isinstance(args[0], (str, os.PathLike)):
        path = Path(args[0])
        if not path.is_relative_to(root):
            return
        mode, flags = args[1:3]
        if (mode and any(c in mode for c in "wax+")) or flags & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
        ):
            counts["mutations"].append((event, str(path)))
        elif path.parent == root / "webproto/runs" and path.suffix == ".json":
            counts["canonical"].append(path.name)
        elif path.is_relative_to(root / "webproto/runs/refs"):
            counts["lookup"].append(path.name)
        else:
            counts["other"].append(str(path))
    elif event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.chmod"):
        counts["mutations"].append((event, str(args)))


sys.addaudithook(_audit)


@contextmanager
def counted(root):
    global _COUNTING
    counts = {key: [] for key in ("canonical", "lookup", "other", "mutations")}
    _COUNTING = root, counts
    try:
        yield counts
    finally:
        _COUNTING = None


def snapshot(ref):
    return {
        "ref": ref,
        "observed_at": "2026-10-14T00:00:00Z",
        "card": {"value": {"title": "The target card", "state": "issues", "type": "code"}},
        "project": {"id": "ummanu"},
        "events": {"items": []},
        "agents": {"items": []},
    }


class CardRunRenderTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = RunStore(self.data)
        self.ops = OperationLayer(self.data / "instance", data_dir=self.data, clock=lambda: 100)

    def app(self, ref):
        return WebApp(Recording(task_snapshot=snapshot(ref)), self.ops, *[Recording() for _ in range(6)])

    def legacy(self, *records):
        self.store.root.mkdir(parents=True, exist_ok=True)
        for run in records:
            self.store._run_path(run.run_id).write_text(json.dumps(run.to_json()), encoding="utf-8")

    def guarded_render(self, ref):
        with (
            mock.patch.object(OperationLayer, "run_list", side_effect=AssertionError("mutating listing")),
            mock.patch.object(OperationLayer, "run_state", side_effect=AssertionError("lifecycle read")),
            mock.patch.object(OperationLayer, "_lifecycle", side_effect=AssertionError("lifecycle")),
            mock.patch.object(OperationLayer, "_runtime", side_effect=AssertionError("runtime")),
            mock.patch.object(OperationLayer, "_audit", side_effect=AssertionError("audit lookup")),
            mock.patch.object(OperationLayer, "_republish", side_effect=AssertionError("publication")),
            mock.patch.object(run_events, "publish_started", side_effect=AssertionError("started event")),
            mock.patch.object(run_events, "publish_finished", side_effect=AssertionError("finished event")),
            mock.patch.object(RunStore, "save", side_effect=AssertionError("save")),
            mock.patch.object(RunStore, "settle", side_effect=AssertionError("settle")),
            mock.patch.object(store_io, "write_text_atomic", side_effect=AssertionError("write")),
        ):
            return self.app(ref).handle("GET", f"/tasks/{ref}")

    def test_cold_and_repeated_get_read_target_only_at_n_and_2n_foreign_records(self):
        target = [record("pr-c", started_at=3), record("pr-b", started_at=1), record("pr-a", started_at=1)]
        metrics = []
        for n in (16, 32):
            with self.subTest(foreign=n):
                self.legacy(*target, *[record(f"foreign-{i}", f"foreign-card-{i}") for i in range(n)])
                with counted(self.data) as bootstrap:
                    self.ops.prepare_card_runs()
                self.assertEqual(len(bootstrap["canonical"]), n + len(target))
                self.assertGreater(len(bootstrap["mutations"]), 0)
                self.assertEqual(
                    set(bootstrap["canonical"]),
                    {r.run_id + ".json" for r in target} | {f"foreign-{i}.json" for i in range(n)},
                )
                for ref, expected in (("ummanu-1", ["pr-a", "pr-b", "pr-c"]), ("no-target", [])):
                    per_get = []
                    for _ in range(2):
                        rendered = []
                        real_page = pages.task

                        def page(document, rendered=rendered, real_page=real_page, **kwargs):
                            rendered.extend(kwargs["runs"]["items"])
                            return real_page(document, **kwargs)

                        with (
                            counted(self.data) as counts,
                            mock.patch.object(pages, "task", side_effect=page),
                            mock.patch.object(ProductRun, "from_json", wraps=ProductRun.from_json) as decoded,
                        ):
                            response = self.guarded_render(ref)
                        self.assertEqual(response.status, 200)
                        self.assertIn(b"The target card", response.body)
                        self.assertEqual([item["run"]["run_id"] for item in rendered], expected)
                        self.assertEqual(sorted(counts["canonical"]), [rid + ".json" for rid in expected])
                        self.assertEqual(decoded.call_count, len(expected))
                        self.assertEqual(counts["mutations"], [])
                        self.assertEqual(counts["other"], [])
                        self.assertEqual(len(counts["lookup"]), 4 if expected else 2)
                        per_get.append((counts["canonical"], counts["lookup"]))
                    self.assertEqual(per_get[0], per_get[1])
                    metrics.append((n, ref, len(per_get[0][0]), len(per_get[0][1])))
        # Capture concrete read counts for this N/2N fixture, separately from preparation.
        self.assertEqual(metrics[0][2:], metrics[2][2:])
        self.assertEqual(metrics[1][2:], metrics[3][2:])
        print(f"card-runs N/2N read counts (foreign, ref, canonical, lookup): {metrics}")

    def test_observation_states_and_stored_result_survive_repeated_render_without_mutation(self):
        result = self.data / "result.json"
        result.write_text('{"status":"done","summary":"<result>"}', encoding="utf-8")
        corrupt_result = self.data / "corrupt-result.json"
        corrupt_result.write_text("{", encoding="utf-8")
        cases = (
            (record(), "not-yet-written", "unknown", False),
            (record(phase="raised"), "identity-mismatch", "unknown", False),
            (record(phase="raised"), "live-match", "running", False),
            (record(phase="raised", deadline_at=1), "live-match", "running", False),
            (record(phase="raised", result_path=str(result)), "live-match", "running", False),
            (record(phase="raised", result_path=str(result)), "dead", "finished", True),
            (record(phase="raised"), "dead", "process_failed", True),
            (record(phase="raised", result_path=str(corrupt_result)), "live-match", "running", False),
            (record(phase="raised"), "unreadable", "source_unavailable", False),
            (
                record(phase="unresolved", unresolved_reason="ownership unknown"),
                "live-match",
                "unknown",
                False,
            ),
            (
                record(
                    phase="settled",
                    ended=True,
                    settled_state="finished",
                    settled_reason="stored",
                    settled_at=2,
                    settled_result={
                        "present": True,
                        "value": {"verdict": "green", "summary": "persisted"},
                        "reason": None,
                    },
                ),
                "unreadable",
                "finished",
                True,
            ),
        )
        for run, heartbeat, value, ended in cases:
            with self.subTest(
                phase=run.phase, heartbeat=heartbeat, deadline=run.deadline_at, result=run.result_path
            ):
                self.legacy(run)
                self.store.prepare_ref_lookup()
                before = {p: p.read_bytes() for p in self.store.root.rglob("*") if p.is_file()}
                for _ in range(2):
                    items = []
                    real_page = pages.task

                    def page(document, items=items, real_page=real_page, **kwargs):
                        items.extend(kwargs["runs"]["items"])
                        return real_page(document, **kwargs)

                    with (
                        counted(self.data) as counts,
                        mock.patch.object(pages, "task", side_effect=page),
                        mock.patch.object(
                            run_state, "head_process_status", return_value={"state": heartbeat}
                        ),
                    ):
                        response = self.guarded_render(run.ref)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(items[0]["state"]["value"], value)
                    self.assertEqual(items[0]["state"]["ended"], ended)
                    self.assertEqual(counts["canonical"], ["pr-one.json"])
                    self.assertEqual(counts["mutations"], [])
                    if run.ended:
                        self.assertEqual(items[0]["state"]["result"]["verdict"], "green")
                        self.assertIn(b"persisted", response.body)
                    if run.result_path:
                        self.assertTrue(items[0]["state"]["result"]["present"])
                        if run.result_path == str(result):
                            self.assertIn(b"&lt;result&gt;", response.body)
                        else:
                            self.assertIn("not JSON", items[0]["state"]["result"]["reason"])
                self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_missing_dirty_or_corrupt_lookup_keeps_card_and_unavailable_runs_section(self):
        self.legacy(record())
        for fault in ("missing", "dirty", "corrupt"):
            with self.subTest(fault=fault):
                self.store.prepare_ref_lookup()
                if fault == "missing":
                    self.store.refs.marker.unlink()
                else:
                    self.store.refs.marker.write_text(
                        "{" if fault == "corrupt" else '{"complete":false}', encoding="utf-8"
                    )
                before = {p: p.read_bytes() for p in self.store.root.rglob("*") if p.is_file()}
                for _ in range(2):
                    with counted(self.data) as counts:
                        response = self.guarded_render("ummanu-1")
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"The target card", response.body)
                    self.assertIn(b"ref lookup", response.body)
                    self.assertEqual(counts["canonical"], [])
                    self.assertEqual(counts["mutations"], [])
                self.assertEqual({p: p.read_bytes() for p in before}, before)
        with self.assertRaises(ReadError) as refused:
            self.ops.card_runs("ummanu-1")
        self.assertEqual(refused.exception.code, "backend_unavailable")

    def test_unreadable_target_record_names_its_path_and_preserves_the_rest_of_the_page(self):
        self.legacy(record("pr-broken"), record("pr-foreign", "foreign-card"))
        self.ops.prepare_card_runs()
        self.store._run_path("pr-broken").write_text("{not json", encoding="utf-8")
        self.store._run_path("pr-foreign").write_text("{also not json", encoding="utf-8")
        before = {p: p.read_bytes() for p in self.store.root.rglob("*") if p.is_file()}
        for _ in range(2):
            with counted(self.data) as counts:
                response = self.guarded_render("ummanu-1")
            self.assertEqual(response.status, 200)
            self.assertIn(b"The target card", response.body)
            self.assertIn(b"could not find out this card's product runs:", response.body)
            self.assertIn(b"pr-broken.json", response.body)
            self.assertNotIn(b"pr-foreign.json", response.body)
            self.assertEqual(counts["canonical"], ["pr-broken.json"])
            self.assertEqual(len(counts["lookup"]), 3)
            self.assertEqual(counts["mutations"], [])
        with counted(self.data) as counts:
            response = self.guarded_render("no-target")
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"could not find out this card's product runs:", response.body)
        self.assertEqual(counts["canonical"], [])
        self.assertEqual(len(counts["lookup"]), 2)
        self.assertEqual(counts["mutations"], [])
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_web_startup_prepares_history_before_serve_and_refuses_failed_bootstrap(self):
        self.legacy(record())
        args = Namespace(
            instance=str(self.ops.instance),
            data_dir=str(self.data),
            heads_registry=None,
            offline=True,
            host="127.0.0.1",
            port=0,
        )
        served = []

        def serve(app, **kwargs):
            self.assertEqual(app.ops.card_runs("ummanu-1")["items"][0]["run"]["run_id"], "pr-one")
            served.append(app)
            return 0

        with (
            mock.patch.object(commands, "hold_store_exclusion", return_value=None),
            mock.patch.object(commands, "serve", side_effect=serve),
        ):
            self.assertEqual(commands.run_web_serve(args), 0)
            self.assertEqual(len(served), 1)
            self.store._run_path("pr-one").write_text("{", encoding="utf-8")
            with mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(commands.run_web_serve(args), 2)
            self.assertEqual(len(served), 1)

    def test_api_and_cli_listing_keep_the_supported_mutating_entry(self):
        with mock.patch.object(self.ops, "run_list", return_value={"items": []}) as listing:
            response = self.app("ummanu-1").handle("GET", "/api/tasks/ummanu-1/runs")
            with (
                mock.patch.object(run_commands, "_ops_layer", return_value=self.ops),
                mock.patch.object(sys, "stdout", io.StringIO()) as output,
            ):
                self.assertEqual(run_commands.run_web_run_list(Namespace(ref="ummanu-1", json=True)), 0)
                self.assertEqual(json.loads(output.getvalue()), {"items": []})
        self.assertEqual(response.status, 200)
        self.assertEqual(listing.call_args_list, [mock.call("ummanu-1"), mock.call("ummanu-1")])
        with mock.patch.object(self.ops, "run_state", return_value={"kind": "product_run"}) as state:
            response = self.app("ummanu-1").handle("GET", "/api/runs/pr-one")
        self.assertEqual(response.status, 200)
        state.assert_called_once_with("pr-one")
        self.legacy(record())
        with mock.patch.object(self.ops, "run_state", return_value={"kind": "product_run"}) as state:
            self.assertEqual(self.ops.run_list("ummanu-1")["items"], [{"kind": "product_run"}])
        state.assert_called_once_with("pr-one")
