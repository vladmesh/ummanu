"""The board page through its route: columns, tiles, filters, the links to it and a board that
cannot be read.

Hermetic. The real `ReadLayer` reads a fake board client that answers `board_cards` with the rows a
board store statement returns; which rows the store selects is `tests/test_web_board_store.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Any
from unittest import mock

from tests.support.instance import status_instance
from tests.web_fakes import Recording, system_snapshot
from ummanu.board import sprint_dod
from ummanu.board.owner_handover import WAITING_OWNER, WAITING_OWNER_BY, WAITING_OWNER_REASON
from ummanu.cli import main
from ummanu.config import validate
from ummanu.dispatch.gate_receipt import mint_gate_receipt
from ummanu.dispatch.state import REVIEW_REJECTION_REASON
from ummanu.web.app import WebApp
from ummanu.web.page_card import task as task_page
from ummanu.webproto.reads import ReadLayer

NOW = 1_800_000_000.0
HOUR = 3600.0


def row(ref: str, state: str, **values: Any) -> dict[str, Any]:
    """One `board_cards` row: a well-formed code card unless `values` says otherwise."""
    return {
        "ref": ref,
        "title": f"title of {ref}",
        "state": state,
        "type": "code",
        "project": "ummanu",
        "sprint": None,
        "moved": Decimal(str(NOW - HOUR)),
        "head_override": None,
        "worker_head": None,
        "review_head_override": None,
        "review_head": None,
        "extensions": {},
        "blocking_comment": None,
        "worker_report": None,
        **values,
    }


class BoardClient:
    """The board store's `board_cards`, answering the rows it was given, or failing as told."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        failure: Exception | None = None,
        open_sprints: list[str] | None = None,
    ) -> None:
        self.rows = rows or []
        self.failure = failure
        self.open_sprints = open_sprints or []
        self.asked: list[dict[str, Any]] = []

    def board_cards(self, **arguments: Any) -> list[dict[str, Any]]:
        self.asked.append(arguments)
        if self.failure is not None:
            raise self.failure
        return self.rows

    def open_sprint_refs(self) -> list[str]:
        if self.failure is not None:
            raise self.failure
        return self.open_sprints


def page(
    client: Any, path: str = "/board", *, instance: str = "/nonexistent/instance", data_dir: Path | None = None
) -> tuple[int, str]:
    reads = ReadLayer(instance, board_client=client, clock=lambda: NOW, data_dir=data_dir)
    app = WebApp(reads, *(Recording() for _ in range(7)))
    route, _mark, query = path.partition("?")
    response = app.handle("GET", route, query=query)
    return response.status, response.body.decode("utf-8")


def columns(html: str) -> list[tuple[str, str, list[str]]]:
    """Each column in page order: its title, its count, and its tiles' markup."""
    found = []
    for section in html.split('<section class="board-column')[1:]:
        title = re.search(r"<h2>(.*?)</h2>", section).group(1)
        count = re.search(r'<span class="count">(.*?)</span>', section).group(1)
        found.append((title, count, re.findall(r'<li class="tile.*?</li>', section, re.DOTALL)))
    return found


def tile_of(html: str, ref: str) -> str:
    return next(tile for _t, _c, tiles in columns(html) for tile in tiles if f'href="/tasks/{ref}"' in tile)


def text(markup: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", markup)).split())


class ColumnTests(unittest.TestCase):
    def test_columns_in_board_order_with_counts_and_the_oldest_move_first(self) -> None:
        client = BoardClient(
            [
                row("u-1", "ready", moved=Decimal(str(NOW - HOUR))),
                row("u-2", "ready", moved=Decimal(str(NOW - 5 * HOUR))),
                row("u-3", "ready", moved=None),
                row("u-4", "done"),
                row("u-5", "issues"),
                row("u-6", "assessment"),
                row("u-7", "ci"),
                row("u-8", "review"),
                row("u-9", "post_merge_ci"),
            ]
        )
        status, html = page(client)
        self.assertEqual(status, 200)
        shown = columns(html)
        self.assertEqual(
            [(title, count) for title, count, _tiles in shown],
            [
                ("Issues", "1"),
                ("Ready", "3"),
                ("In progress", "0"),
                ("Validate", "0"),
                ("CI", "1"),
                ("Review", "1"),
                ("Assessment", "1"),
                ("Post-merge CI", "1"),
                ("Blocked", "0"),
                ("Done", "1"),
            ],
        )
        ready = [re.search(r'href="/tasks/([^"]+)"', tile).group(1) for tile in shown[1][2]]
        self.assertEqual(ready, ["u-3", "u-2", "u-1"])

    def test_a_column_without_cards_is_marked_empty_and_a_column_with_cards_is_not(self) -> None:
        _status, html = page(BoardClient([row("u-1", "ready"), row("u-2", "done")]))
        marked = re.findall(r'<section class="board-column( board-column-empty)?" aria-label="([^:]+):', html)
        self.assertEqual(
            [(title, bool(empty)) for empty, title in marked],
            [
                ("Issues", True),
                ("Ready", False),
                ("In progress", True),
                ("Validate", True),
                ("CI", True),
                ("Review", True),
                ("Assessment", True),
                ("Post-merge CI", True),
                ("Blocked", True),
                ("Done", False),
            ],
        )

    def test_the_board_page_alone_fits_the_viewport(self) -> None:
        body = re.compile(r"<body[^>]*>")
        self.assertEqual(body.findall(page(BoardClient())[1]), ['<body class="fit-viewport">'])
        self.assertEqual(body.findall(page(BoardClient(), "/sprints")[1]), ["<body>"])

    def test_the_read_asks_for_done_since_the_window_and_the_header_links_the_board(self) -> None:
        client = BoardClient()
        _status, html = page(client)
        self.assertEqual(client.asked[0]["done_since"], datetime.fromtimestamp(NOW - 48 * HOUR, UTC))
        self.assertIn('<a href="/board" aria-current="page">Board</a>', html)


class TileTests(unittest.TestCase):
    def test_a_tile_names_its_card_title_type_project_sprint_and_age_in_the_column(self) -> None:
        client = BoardClient(
            [
                row("u-1", "ready", sprint="sprint:7", moved=Decimal(str(NOW - 3 * HOUR))),
                row("u-2", "ready", sprint=None, type="research", project="codegen"),
            ]
        )
        _status, html = page(client)
        linked = tile_of(html, "u-1")
        self.assertIn('<a class="ref" href="/tasks/u-1" title="u-1">u-1</a>', linked)
        self.assertIn('<p class="tile-title" title="title of u-1">title of u-1</p>', linked)
        self.assertIn('<a href="/sprints/sprint%3A7">sprint:7</a>', linked)
        self.assertIn(">3h</span>", linked)
        outside = text(tile_of(html, "u-2"))
        self.assertIn("research · codegen · вне спринта", outside)

    def test_each_metadata_item_is_one_unbreakable_item(self) -> None:
        client = BoardClient([row("u-1", "ready", project="codegen-orchestrator", sprint=None, type="code")])
        _status, html = page(client)
        meta = re.search(r'<p class="tile-meta">.*?</p>', tile_of(html, "u-1")).group(0)
        items = re.findall(r'<span class="meta-item">(.*?)</span>(?: · |</p>)', meta)
        self.assertEqual([text(item) for item in items], ["code", "codegen-orchestrator", "вне спринта", "—"])

    def test_the_head_is_the_worker_in_progress_the_reviewer_in_validate_and_none_elsewhere(self) -> None:
        heads = {
            "head_override": "claude-opus-high",
            "worker_head": "codex-terra-high",
            "review_head_override": "codex-review",
            "review_head": "claude-review",
        }
        client = BoardClient(
            [
                row("u-1", "in_progress", **heads),
                row("u-2", "in_progress", worker_head="codex-terra-high"),
                row("u-3", "validate", **heads),
                row("u-4", "validate", review_head="claude-review"),
                row("u-5", "ready", **heads),
                row("u-6", "assessment", **heads),
            ]
        )
        _status, html = page(client)
        shown = {
            ref: re.search(r'<span title="current head">(.*?)</span>', tile_of(html, ref)).group(1)
            for ref in ("u-1", "u-2", "u-3", "u-4", "u-5", "u-6")
        }
        self.assertEqual(
            shown,
            {
                "u-1": "claude-opus-high",
                "u-2": "codex-terra-high",
                "u-3": "codex-review",
                "u-4": "claude-review",
                "u-5": "—",
                "u-6": "—",
            },
        )

    def test_a_blocked_tile_shows_the_first_content_line_of_its_blocking_comment_or_says_none_was_recorded(
        self,
    ) -> None:
        client = BoardClient(
            [
                row("u-1", "blocked", blocking_comment="[dispatcher]\n\n  red gate on   the same SHA \nlog tail"),
                row("u-2", "blocked", blocking_comment=None),
                row("u-3", "blocked", blocking_comment="[observer]\n   \n"),
                row("u-4", "ready", blocking_comment="[dispatcher]\nnot a Blocked card"),
            ]
        )
        _status, html = page(client)
        reason = re.compile(r'<p class="tile-reason" title="([^"]*)">(.*?)</p>')
        self.assertEqual(
            reason.findall(tile_of(html, "u-1")), [("red gate on the same SHA", "red gate on the same SHA")]
        )
        self.assertEqual(reason.findall(tile_of(html, "u-2")), [("reason not recorded", "reason not recorded")])
        self.assertEqual(reason.findall(tile_of(html, "u-3")), [("reason not recorded", "reason not recorded")])
        self.assertEqual(reason.findall(tile_of(html, "u-4")), [])

    def test_a_blocked_tile_reason_is_the_first_line_that_says_something_and_looks_behind_the_relay(
        self,
    ) -> None:
        report = (
            "[report:blocked]\nclassification: external_fact\n\n## Blocker\n\n"
            "Classification: external_fact. There is no authorised session.\n## Places checked"
        )
        cases = [
            ("a labelled heading", {"blocking_comment": "[report:blocked]\n## Blocker: external_fact, a kit defect"},
             "external_fact, a kit defect"),
            ("a label heading and a classification clause", {"blocking_comment": report},
             "There is no authorised session."),
            ("the relay of a worker report", {"blocking_comment": "[dispatcher]\nworker report:blocked",
                                              "worker_report": report}, "There is no authorised session."),
            ("the relay with no worker report", {"blocking_comment": "[dispatcher]\nworker report:blocked"},
             "reason not recorded"),
            ("a reason that names the relay", {"blocking_comment": "[dispatcher]\nworker report:blocked twice",
                                               "worker_report": report}, "worker report:blocked twice"),
            ("only labels", {"blocking_comment": "[observer]\nclassification: other\n# Blocker\nBlocker:"},
             "reason not recorded"),
            ("a worker report that is not text", {"blocking_comment": "[dispatcher]\nworker report:blocked",
                                                  "worker_report": 7}, "reason not recorded"),
        ]
        reason = re.compile(r'<p class="tile-reason" title="[^"]*">(.*?)</p>')
        for name, values, expected in cases:
            with self.subTest(name):
                _status, html = page(BoardClient([row("u-1", "blocked", **values)]))
                self.assertEqual(reason.findall(tile_of(html, "u-1")), [expected])

    def test_a_card_handed_to_the_owner_has_a_marked_tile_with_a_text_label(self) -> None:
        mark = {
            WAITING_OWNER: "2026-10-16T10:00:00Z",
            WAITING_OWNER_REASON: "choose the deploy window",
            WAITING_OWNER_BY: "po",
        }
        client = BoardClient(
            [row("u-1", "in_progress", extensions={"extra": mark}), row("u-2", "in_progress")]
        )
        _status, html = page(client)
        waiting = tile_of(html, "u-1")
        self.assertTrue(waiting.startswith('<li class="tile tile-owner">'))
        self.assertIn('title="choose the deploy window">waits for the owner</p>', waiting)
        self.assertTrue(tile_of(html, "u-2").startswith('<li class="tile">'))
        self.assertNotIn("waits for the owner", tile_of(html, "u-2"))


class ReadySkipTests(unittest.TestCase):
    """The dispatcher's skip reason on the Ready tiles it skipped, read from its production state."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        streak = {"since": NOW - 2 * HOUR, "until": 0.0, "noticed": True}
        (self.data_dir / "dispatcher").mkdir()
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps(
                {
                    "records": {},
                    "ready_skips": {
                        "u-1": {**streak, "reason": "blocked_by task is not Done"},
                        "u-3": {**streak, "reason": "code slot taken by u-9"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.client = BoardClient([row("u-1", "ready"), row("u-2", "ready"), row("u-3", "blocked")])

    def tiles(self, document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {tile["ref"]: tile for column in document["columns"] for tile in column["cards"]}

    def test_a_skipped_ready_card_carries_its_reason_and_since_and_no_other_card_does(self) -> None:
        document = ReadLayer("/nonexistent/instance", board_client=self.client, clock=lambda: NOW,
                             data_dir=self.data_dir).board_view()

        self.assertEqual(document["ready_skips"]["source"]["state"], "available")
        tiles = self.tiles(document)
        self.assertEqual(
            tiles["u-1"]["ready_skip"], {"reason": "blocked_by task is not Done", "since": "2027-01-15T06:00:00Z"}
        )
        self.assertIsNone(tiles["u-2"]["ready_skip"])
        self.assertIsNone(tiles["u-3"]["ready_skip"])
        _status, html = page(self.client, data_dir=self.data_dir)
        self.assertIn("not taken since 2027-01-15T06:00:00Z: blocked_by task is not Done", text(tile_of(html, "u-1")))
        self.assertNotIn("not taken", tile_of(html, "u-2"))

    def test_an_unreadable_production_state_leaves_the_board_standing_without_skips(self) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text("{", encoding="utf-8")

        document = ReadLayer("/nonexistent/instance", board_client=self.client, clock=lambda: NOW,
                             data_dir=self.data_dir).board_view()

        self.assertEqual(document["source"]["state"], "available")
        self.assertEqual(document["ready_skips"]["source"]["state"], "unavailable")
        self.assertIsNone(self.tiles(document)["u-1"]["ready_skip"])


class PipelineDisplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = status_instance(self.root).instance_path
        self.data_dir = self.root / "data"
        self.path = self.data_dir / "dispatcher" / "production-state.json"
        self.path.parent.mkdir(parents=True)
        self.client = BoardClient()
        self.cards = {}
        self.enterContext(mock.patch("ummanu.task_commands.card_client", return_value=self.client))
        self.show = self.enterContext(mock.patch("ummanu.tasks.TaskReader.show", side_effect=lambda ref: self.cards[ref]))
        self.audit = mock.Mock(spec=["events"])
        self.audit.events.return_value = []
        self.enterContext(mock.patch("ummanu.tasks.task_audit_for", return_value=self.audit))
        self.enterContext(mock.patch("ummanu.webproto.reads.task_audit_for", return_value=self.audit))
        self.enterContext(mock.patch("time.time", return_value=NOW))
        self.reads = ReadLayer(self.instance, board_client=self.client, clock=lambda: NOW, data_dir=self.data_dir)

    def write(self, cards, records) -> None:
        self.cards = {card["ref"]: card for card in cards}
        self.client.rows = cards
        self.path.write_text(json.dumps({"records": records}), encoding="utf-8")

    def native(self, ref="sample-1"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["task", "show", "--ref", ref, "--instance", str(self.instance)])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_stage_facts_agree_in_all_consumers_and_render_on_board_and_card(self) -> None:
        sha = "a" * 40
        receipt = mint_gate_receipt(
            validated_sha=sha, base_sha="b" * 40, gate_mode="github",
            required_checks=[{"name": "unit", "conclusion": "SUCCESS"}], check_set_identity="unit",
        )
        red = {"rejected_sha": sha, "rejected_failure_class": "substantive",
               "rejected_failure_reason": "unit failed <reason>",
               "rejected_gate_evidence": [{"run_id": "42", "suite": "unit", "artifact": "suite.log"}]}
        publication = {**red, "rejected_failure_class": "publication",
                       "rejected_failure_reason": "branch lease refused <reason>", "rejected_gate_evidence": []}
        green = {"gate_state": "green", "gate_attestation": receipt, "review_commit": sha}
        cases = [
            ("waiting", "ci", "code", {}, "waiting", "CI waiting", None),
            ("pending", "ci", "code", {"gate_pending_since": 1}, "pending", "CI pending", None),
            ("returning", "ci", "code", {**red, "worker_continuation": {
                "stage": "red_transition_pending", "phase": "gate", "verdict_outcome": "gate_red",
            }}, "red", "CI red; returning to worker", None),
            ("rework", "in_progress", "code", red, "unknown", "In progress; reworking after CI red", "red"),
            ("new report", "ci", "code", red, "waiting", "CI waiting", "red"),
            ("new pending", "ci", "code", {**red, "gate_pending_since": 1,
                "gate_published_ref": {"branch": "pipeline/sample-1", "sha": "c" * 40}},
                "pending", "CI pending", "red"),
            ("review", "review", "code", green, "green", "Review; CI passed", None),
            ("review missing receipt", "review", "code", {"gate_state": "green"},
                "unknown", "Review; CI status unknown", None),
            ("review mismatched receipt", "review", "code", {**green, "review_commit": "c" * 40},
                "unknown", "Review; CI status unknown", None),
            ("stale green in new pending", "ci", "code", {**green, "gate_pending_since": 1},
                "pending", "CI pending", None),
            ("publication", "in_progress", "code", publication,
                "unknown", "In progress; publication failed, CI did not run", "not_run"),
            ("publication returning", "ci", "code", {**publication, "worker_continuation": {
                "stage": "red_transition_pending", "phase": "gate", "verdict_outcome": "gate_red",
            }}, "not_run", "Publication failed; CI did not run", None),
            ("transport", "ci", "code", {"gate_pending_since": 1, "gate_transport_error": "API <unavailable>"},
                "unknown", "CI status unknown", None),
            ("non-code", "in_progress", "research", green, "not_applicable", "In progress", None),
            ("finished", "done", "code", None, "unknown", "Done", None),
            ("missing", "ci", "code", None, "unknown", "CI status unknown", None),
            ("review rejection", "in_progress", "code", {**red, "rejected_failure_reason": REVIEW_REJECTION_REASON},
                "unknown", "In progress", None),
        ]
        for name, stage, kind, record, status, label, previous in cases:
            with self.subTest(name=name):
                card = row("sample-1", stage, type=kind, moved=NOW - HOUR)
                self.write([card], {} if record is None else {"sample-1": record})
                document = self.reads.board_view()
                tile = next(tile for col in document["columns"] for tile in col["cards"])
                snapshot = self.reads.task_snapshot("sample-1")
                native = self.native()
                summary = tile["pipeline"]
                self.assertEqual(summary, snapshot["pipeline"])
                self.assertEqual(summary, native["pipeline"])
                self.assertEqual((summary["ci"]["status"], summary["label"]), (status, label))
                if previous:
                    self.assertEqual(summary["previous"]["status"], previous)
                    self.assertEqual(summary["previous"]["sha"], sha)
                    self.assertEqual(summary["previous"]["evidence"], record["rejected_gate_evidence"])
                else:
                    self.assertIsNone(summary["previous"])
                self.assertEqual(summary["ci"]["sha"], sha if status in {"green", "red", "not_run"} else None)
                self.assertEqual(validate(snapshot, "web-read", "task"), [])
                board_html = page(self.client, instance=str(self.instance))[1]
                card_html = task_page(snapshot, runs={})
                self.assertIn(label, text(tile_of(board_html, "sample-1")))
                hero = card_html.split('<div class="grid">')[0]
                self.assertIn(label, text(hero))
                if previous or status in {"red", "not_run"}:
                    self.assertIn(sha, tile_of(board_html, "sample-1"))
                    self.assertIn(sha, hero)
                    self.assertIn("&lt;reason&gt;", hero)
                if stage == "ci":
                    self.assertIsNone(tile["head"])

    def test_board_batches_production_read_and_never_reads_individual_cards(self) -> None:
        cards = [row(f"sample-{index}", "ci", moved=NOW - HOUR) for index in range(1, 51)]
        self.write(cards, {card["ref"]: {"gate_pending_since": 1} for card in cards})
        read_text = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text) as reads:
            document = self.reads.board_view()
        self.assertEqual([call.args[0] for call in reads.call_args_list], [self.path])
        self.assertEqual(len(self.client.asked), 1)
        self.show.assert_not_called()
        self.audit.events.assert_not_called()
        summaries = [tile["pipeline"] for col in document["columns"] for tile in col["cards"]]
        self.assertEqual(len(summaries), 50)
        self.assertEqual({summary["ci"]["status"] for summary in summaries}, {"pending"})

    def test_unavailable_records_preserve_cards_and_explain_unknown_ci(self) -> None:
        card = row("sample-1", "ci", moved=NOW - HOUR)
        for content in (None, "{", json.dumps({"records": {"sample-1": {"attempt_round": "bad"}}}),
                        json.dumps({"records": {"sample-1": None}})):
            with self.subTest(content=content):
                self.write([card], {})
                if content is None:
                    self.path.unlink()
                else:
                    self.path.write_text(content, encoding="utf-8")
                document = self.reads.board_view()
                tile = next(tile for col in document["columns"] for tile in col["cards"])
                snapshot = self.reads.task_snapshot("sample-1")
                native = self.native()
                self.assertEqual(document["source"]["state"], "available")
                self.assertEqual(snapshot["card"]["value"]["title"], card["title"])
                for summary in (tile["pipeline"], snapshot["pipeline"], native["pipeline"]):
                    self.assertEqual(summary["source"]["state"], "unavailable")
                    self.assertEqual(summary["ci"]["status"], "unknown")
                    self.assertIn("could not be read", summary["ci"]["reason"])
                    self.assertEqual(summary["label"], "CI status unknown")
                self.assertIn("CI status unknown", text(tile_of(page(self.client, instance=str(self.instance))[1], "sample-1")))
                self.assertIn("could not be read", text(task_page(snapshot, runs={})))

    def test_task_show_preserves_card_and_committed_head_changes_with_one_record_read(self) -> None:
        card = row("sample-1", "ci", moved=NOW - HOUR)
        self.write([card], {"sample-1": {"gate_pending_since": 1}})
        read_text = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text) as reads:
            self.reads.task_snapshot("sample-1")
        self.assertEqual([call.args[0] for call in reads.call_args_list].count(self.path), 1)
        payload = {"role": "worker", "change": "replaced", "reason": "stalled", "before": {"run_id": "old"}}
        self.audit.events.return_value = [{
            "kind": "head_changed", "occurred_at": "2026-01-01T00:00:00Z", "request_id": "change-1",
            "payload": payload,
        }]
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text) as reads:
            result = self.native()
        self.assertEqual({key: result[key] for key in card}, card)
        self.assertEqual(result["head_changes"], [{"at": "2026-01-01T00:00:00Z", "request_id": "change-1", **payload}])
        self.assertEqual([call.args[0] for call in reads.call_args_list].count(self.path), 1)
        self.assertEqual(result["pipeline"]["ci"]["status"], "pending")


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "projects").mkdir()
        for project in ("codegen", "ummanu"):
            (root / "projects" / f"{project}.yaml").write_text(
                f"id: {project}\nrepo: {root}\nenabled: true\nadapter: ummanu\ndefault_branch: main\n",
                encoding="utf-8",
            )
        self.instance = str(status_instance(root).instance_path)

    def form(self, html: str) -> str:
        return re.search(r'<form class="inline board-filter".*?</form>', html, re.DOTALL).group(0)

    def selected(self, form: str, name: str) -> list[tuple[str, str, bool]]:
        """Each option of the named select: its value, its label and whether it is selected."""
        select = re.search(rf'<select id="board-{name}" name="{name}">(.*?)</select>', form).group(1)
        return [
            (unescape(value), unescape(label), bool(mark))
            for value, mark, label in re.findall(r'<option value="([^"]*)"( selected)?>(.*?)</option>', select)
        ]

    def test_each_filter_reaches_the_board_statement_and_none_asks_for_cards_of_no_sprint(self) -> None:
        cases = {
            "/board": {"project": None, "sprint": None, "no_sprint": False},
            "/board?project=codegen": {"project": "codegen", "sprint": None, "no_sprint": False},
            "/board?sprint=sprint%3A7": {"project": None, "sprint": "sprint:7", "no_sprint": False},
            "/board?sprint=none": {"project": None, "sprint": None, "no_sprint": True},
            "/board?project=codegen&sprint=sprint%3A7": {"project": "codegen", "sprint": "sprint:7", "no_sprint": False},
            "/board?project=&sprint=": {"project": None, "sprint": None, "no_sprint": False},
        }
        for path, expected in cases.items():
            with self.subTest(path):
                client = BoardClient()
                status, _html = page(client, path, instance=self.instance)
                self.assertEqual(status, 200)
                self.assertEqual(len(client.asked), 1)
                asked = {name: client.asked[0][name] for name in expected}
                self.assertEqual(asked, expected)
                self.assertEqual(client.asked[0]["done_since"], datetime.fromtimestamp(NOW - 48 * HOUR, UTC))

    def test_the_form_offers_the_registered_projects_no_sprint_and_the_open_sprints_with_the_active_ones_selected(
        self,
    ) -> None:
        client = BoardClient([row("u-1", "ready")], open_sprints=["sprint:9", "sprint:7"])
        _status, html = page(client, "/board?project=codegen&sprint=none", instance=self.instance)
        form = self.form(html)
        self.assertIn('method="get" action="/board"', form)
        self.assertIn("showing: project codegen · sprint вне спринта", text(form))
        self.assertEqual(
            self.selected(form, "project"),
            [("", "all projects", False), ("codegen", "codegen", True), ("ummanu", "ummanu", False)],
        )
        self.assertEqual(
            self.selected(form, "sprint"),
            [
                ("", "all sprints", False),
                ("none", "вне спринта", True),
                ("sprint:9", "sprint:9", False),
                ("sprint:7", "sprint:7", False),
            ],
        )
        self.assertIn('<a href="/board">whole board</a>', form)

    def test_the_whole_board_is_shown_as_such_with_all_projects_and_all_sprints_selected(self) -> None:
        _status, html = page(BoardClient(open_sprints=["sprint:7"]), instance=self.instance)
        form = self.form(html)
        self.assertIn("showing: the whole board", text(form))
        self.assertEqual([label for _value, label, mark in self.selected(form, "project") if mark], ["all projects"])
        self.assertEqual([label for _value, label, mark in self.selected(form, "sprint") if mark], ["all sprints"])

    def test_a_filter_no_card_matches_is_an_empty_board_with_that_filter_shown_and_offered(self) -> None:
        status, html = page(
            BoardClient(open_sprints=["sprint:7"]),
            "/board?project=gone%3Cb%3E&sprint=sprint%3A404",
            instance=self.instance,
        )
        self.assertEqual(status, 200)
        self.assertEqual({count for _title, count, _tiles in columns(html)}, {"0"})
        self.assertEqual(len(columns(html)), 10)
        form = self.form(html)
        self.assertIn("showing: project gone<b> · sprint sprint:404", text(form))
        self.assertNotIn("<b>", form)
        self.assertIn(("gone<b>", "gone<b>", True), self.selected(form, "project"))
        self.assertIn(("sprint:404", "sprint:404", True), self.selected(form, "sprint"))

    def test_choices_that_cannot_be_read_leave_the_form_and_the_board_standing_with_their_reasons(self) -> None:
        client = BoardClient([row("u-1", "ready")])
        client.open_sprint_refs = lambda: (_ for _ in ()).throw(RuntimeError("no sprints table"))
        status, html = page(client, "/board?sprint=sprint%3A7")
        self.assertEqual(status, 200)
        self.assertTrue(tile_of(html, "u-1"))
        form = text(self.form(html))
        self.assertIn("the registered projects could not be read: ", form)
        self.assertIn("the open sprints could not be read: no sprints table", form)
        self.assertIn(("sprint:7", "sprint:7", True), self.selected(self.form(html), "sprint"))

    def test_an_unreadable_board_still_shows_the_filter_it_was_asked_for(self) -> None:
        client = BoardClient(failure=RuntimeError("the board store did not answer"))
        _status, html = page(client, "/board?project=codegen", instance=self.instance)
        self.assertIn("board not readable:", html)
        self.assertIn("showing: project codegen", text(self.form(html)))


class LinkTests(unittest.TestCase):
    def app(self, *, reads: Recording, sprint_reads: Recording | None = None) -> WebApp:
        return WebApp(reads, Recording(), sprint_reads or Recording(), *(Recording() for _ in range(5)))

    def test_the_overview_links_the_whole_board_beside_all_sprints(self) -> None:
        response = self.app(reads=Recording(system_snapshot=system_snapshot())).handle("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(
            '<a class="more" href="/board">board</a><a class="more" href="/sprints">all sprints</a>',
            response.body.decode("utf-8"),
        )

    def test_the_project_page_links_the_board_filtered_to_that_project(self) -> None:
        snapshot = system_snapshot()
        snapshot["projects"]["items"] = [{"id": "team a&b", "repo": "/srv/a", "enabled": True}]
        response = self.app(reads=Recording(system_snapshot=snapshot)).handle("GET", "/projects/team%20a%26b")
        self.assertEqual(response.status, 200)
        self.assertIn('<a href="/board?project=team+a%26b">board</a>', response.body.decode("utf-8"))

    def test_the_sprint_page_links_the_board_filtered_to_that_sprint(self) -> None:
        sprint = {
            "ref": "sprint:7",
            "sprint": {"source": {"state": "available"}, "value": {"ref": "sprint:7", "status": "open",
                                                                      "dod": sprint_dod.section(None)}},
            "observer": {},
            "work": {},
        }
        response = self.app(reads=Recording(), sprint_reads=Recording(sprint_state=sprint)).handle(
            "GET", "/sprints/sprint:7"
        )
        self.assertEqual(response.status, 200)
        self.assertIn('<a href="/board?sprint=sprint%3A7">board</a>', response.body.decode("utf-8"))


class TotalityTests(unittest.TestCase):
    def test_a_failing_board_read_is_a_notice_with_its_reason_not_an_error_status(self) -> None:
        for name, client, said in (
            (
                "the store refuses",
                BoardClient(failure=RuntimeError("the board store did not answer")),
                "the board store did not answer",
            ),
            ("a row in no column", BoardClient([row("u-1", "archived")]), "which no board column shows"),
            ("no store can be built", None, ""),
        ):
            with self.subTest(name):
                status, html = page(client)
                self.assertEqual(status, 200)
                notice = re.search(r'<p class="unavailable"><b>board not readable:</b> (.*?)</p>', html)
                self.assertTrue(notice.group(1).startswith("the board could not be read: "), html)
                self.assertIn(said, unescape(notice.group(1)))
                self.assertNotIn('<section class="board-column', html)

    def test_hostile_rows_render_a_tile_with_honest_placeholders(self) -> None:
        cases: list[tuple[str, dict[str, Any], list[str]]] = [
            ("missing title", {"title": None}, ["no readable title"]),
            ("non-string title", {"title": 42}, ["no readable title"]),
            ("blank title", {"title": " \n "}, ["no readable title"]),
            (
                "title with HTML and newlines",
                {"title": "<script>alert(1)</script>\nsecond line"},
                ["&lt;script&gt;alert(1)&lt;/script&gt; second line"],
            ),
            ("missing project", {"project": None}, ["project unknown"]),
            ("non-string project", {"project": ["ummanu"]}, ["project unknown"]),
            ("missing type", {"type": None}, ["type unknown"]),
            ("non-string type", {"type": {"code": 1}}, ["type unknown"]),
            ("missing sprint", {"sprint": None}, ["вне спринта"]),
            ("non-string sprint", {"sprint": 1494}, ["sprint unknown"]),
            ("missing date_moved", {"moved": None}, ['<span class="age">unknown</span>']),
            ("date_moved in the future", {"moved": Decimal(str(NOW + HOUR))}, ['<span class="age">unknown</span>']),
            ("date_moved infinite", {"moved": Decimal("Infinity")}, ['<span class="age">unknown</span>']),
            ("date_moved before any datetime", {"moved": Decimal("-1e20")}, ['<span class="age">unknown</span>']),
            ("date_moved not a number", {"moved": "yesterday"}, ['<span class="age">unknown</span>']),
            ("extensions not an object", {"extensions": "not json"}, ['<li class="tile">']),
        ]
        for name, values, expected in cases:
            with self.subTest(name):
                status, html = page(BoardClient([row("u-1", "ready", **values)]))
                self.assertEqual(status, 200)
                tile = tile_of(html, "u-1")
                for fragment in expected:
                    self.assertIn(fragment, tile)
                self.assertNotIn("<script>", html.split("<main>")[1].split("</main>")[0])


if __name__ == "__main__":
    unittest.main()
