from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu._fsutil import ndjson_line, ndjson_lines
from ummanu.board.analytics import _read_ndjson as read_analytics_ndjson
from ummanu.board.checkpoint_layout import open_checkpoint_board, publish_split_board
from ummanu.board.normalized_checkpoint import validated_normalized_cards
from ummanu.checkpoint import (
    AnalyticsCheckpoint,
    _analytics_line_count,
    _canonical_run_journals,
    _count_lines,
    _read_ndjson,
    _validate_board,
    _validate_runs,
)
from ummanu.data import export_board, export_runs
from ummanu.installation.materialize import (
    _live_run_journals,
    _render_restored_journal,
    _restored_run_journals,
    materialize_checkpoint,
)
from ummanu.restore import (
    _namespace_is_exported,
    _normalized_cards,
    _normalized_sprints,
    _restore_board_history,
)

SEPARATORS = ("\u2028", "\u2029", "\u0085")
TEXT = "café\u2028line\u2029paragraph\u0085next"


class NdjsonTests(unittest.TestCase):
    def assert_escaped(self, text: str) -> None:
        for character in SEPARATORS:
            self.assertNotIn(character, text)
            self.assertIn(f"\\u{ord(character):04x}", text)
        self.assertIn("café", text)

    def test_writer_preserves_values_and_other_utf8(self) -> None:
        row = {TEXT: [TEXT, "literal \\u2028", "\n\r\t\v\f\x1c"]}
        line = ndjson_line(row)
        self.assert_escaped(line)
        self.assertEqual(line.count("\n"), 1)
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(json.loads(line), row)

    def test_reader_preserves_blank_records_and_accepts_lf_crlf_and_no_final_lf(self) -> None:
        for text, expected in (
            ("", []),
            ("\n", [""]),
            ("{}\n", ["{}"]),
            ("{}\r\n\r\n{}\r\n", ["{}", "", "{}"]),
            ("{}\n\n{}", ["{}", "", "{}"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(ndjson_lines(text), expected)

    def test_old_files_keep_raw_separators_in_one_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "old.ndjson"
            for character in SEPARATORS:
                for ending in ("", "\n", "\r\n"):
                    with self.subTest(character=repr(character), ending=repr(ending)):
                        row = {"text": f"before{character}after"}
                        path.write_bytes((json.dumps(row, ensure_ascii=False) + ending).encode("utf-8"))
                        lines = ndjson_lines(path.read_text(encoding="utf-8"))
                        self.assertEqual(len(lines), 1)
                        self.assertEqual(json.loads(lines[0]), row)
                        self.assertEqual(_count_lines(path, "old export"), 1)
                        self.assertEqual(_analytics_line_count(path.read_bytes()), 1)
                        self.assertEqual(_read_ndjson(path, "old export"), [row])

    def test_board_and_runs_export_checkpoint_restore_round_trip(self) -> None:
        # Only the database inputs are seams. Staging, serialization, publication,
        # checkpoint validation, materialization and restore parsing are real.
        card = {
            "reference": "ummanu-1",
            "title": TEXT,
            "description": TEXT,
            "column": "Ready",
            "metadata": {"record_type": "task"},
            "comments": [{"ts": "1", "text": TEXT}],
        }
        sprint = {
            "ref": "sprint:1",
            "goal": TEXT,
            "definition_of_done": TEXT,
            "status": "open",
            "comments": [{"created_at": "1", "body": TEXT}],
        }
        event = {"request_id": "restore:old:request", "event_id": "event-1", "text": TEXT}
        run = {"text": TEXT}
        reader = mock.Mock()
        reader.export.return_value = [card]
        audit_source = mock.Mock()
        audit_source.status.return_value = {"ok": True}
        audit_source.events.return_value = [event]

        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                instance = root / "instance"
                exported = instance / "state"
                state = root / "source-runs"
                state.mkdir()
                journal = state / "run.jsonl"
                # Old journals may already contain raw separators and blank physical lines.
                journal.write_text("\n" + json.dumps(run, ensure_ascii=False) + "\n", encoding="utf-8")
                with (
                    mock.patch("ummanu.data.task_audit_for", return_value=audit_source),
                    mock.patch("ummanu.sprints.SprintReader.export", return_value=[sprint]),
                ):
                    export_board(exported, instance_dir=instance, reader=reader, sprint_client=mock.Mock())
                export_runs(exported, state_dir=state)
                board = exported / "board"
                runs = exported / "runs"
                cards = validated_normalized_cards(board)
                self.assertEqual(cards[0]["description"].encode("utf-8"), TEXT.encode("utf-8"))
                records = {}
                for path, key in (
                    (board / "cards.ndjson", "cards"),
                    (board / "sprints.ndjson", "sprints"),
                    (board / "audit.ndjson", "events"),
                    (runs / "runs.ndjson", None),
                ):
                    text = path.read_text(encoding="utf-8")
                    self.assert_escaped(text)
                    rows = _read_ndjson(path, path.name)
                    if key is not None:
                        self.assertEqual(rows, json.loads(path.with_suffix(".json").read_text())[key])
                    records[path.name] = rows
                    if legacy:
                        path.write_text(
                            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                            encoding="utf-8",
                        )
                    self.assertEqual(_count_lines(path, path.name), 1)
                    self.assertEqual(_analytics_line_count(path.read_bytes()), 1)
                    self.assertEqual(_read_ndjson(path, path.name), rows)
                self.assertEqual(validated_normalized_cards(board), cards)
                _validate_board(board)
                _validate_runs(runs)
                watermarks = json.loads((runs / "watermarks.json").read_text())
                self.assertEqual(watermarks["files"][0]["lines"], 2)
                self.assertEqual(records["runs.ndjson"], [{"source": "run.jsonl", "line": 2, "record": run}])
                canonical = _canonical_run_journals(runs / "runs.ndjson", "runs")
                self.assertEqual(json.loads(canonical["run.jsonl"][0]), records["runs.ndjson"][0])

                # Exercise the published split checkpoint as well as the flat export.
                split_board = root / "split-board"
                (board / "events.ndjson").write_text("", encoding="utf-8")
                publish_split_board(board, split_board)
                checkpoint = AnalyticsCheckpoint(
                    checkpoint_id="test", directory=split_board, board=open_checkpoint_board(split_board)
                )
                for name in ("cards.ndjson", "sprints.ndjson"):
                    self.assertEqual(read_analytics_ndjson(checkpoint, name), [(1, records[name][0])])

                restored = root / "restored"
                self.assertEqual(materialize_checkpoint(instance, restored), (1, 1))
                self.assertEqual(_normalized_cards(restored), cards)
                self.assertEqual(_normalized_sprints(restored), records["sprints.ndjson"])
                self.assertEqual(_normalized_sprints(restored)[0]["goal"].encode("utf-8"), TEXT.encode("utf-8"))
                # Materialization carries audit.ndjson only, exercising restore's NDJSON fallback.
                self.assertFalse((restored / "board" / "audit.json").exists())
                audit_target = mock.Mock()
                _restore_board_history(restored, audit_target)
                # History is replayed as history: it owes no origin return (secretary-1792).
                audit_target.append.assert_called_once_with(event["request_id"], event, restoring=True)
                self.assertEqual(audit_target.append.call_args.args[1]["text"].encode("utf-8"), TEXT.encode("utf-8"))
                self.assertTrue(_namespace_is_exported(restored, "old"))
                self.assertFalse(_namespace_is_exported(restored, "new"))

                journals = _restored_run_journals(restored / "runs")
                rendered = _render_restored_journal(journals[Path("run.jsonl")])
                self.assert_escaped(rendered)
                self.assertEqual(ndjson_lines(rendered)[0], "")
                restored_run = json.loads(ndjson_lines(rendered)[1])
                self.assertEqual(restored_run["text"].encode("utf-8"), TEXT.encode("utf-8"))
                self.assertEqual(_live_run_journals(state)[Path("run.jsonl")], [journals[Path("run.jsonl")][0][1]])
