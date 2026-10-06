"""Shared memory fact parsing and the descriptive data layout contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_memory_canon import ACTOR, CanonCase, write_index
from ummanu import memory_journal
from ummanu.data import export_memory, init_layout
from ummanu.memory import canon
from ummanu.memory.canon import parse_fact_text
from ummanu.memory_errors import MemoryValidationError
from ummanu.memory_journal import export_memory_snapshot, verify_memory_journal
from ummanu.memory_write import commit_memory_proposal, propose_memory_fact


class FactFrontmatterTests(unittest.TestCase):
    def test_delimiters_are_whole_lines_and_body_bytes_are_preserved(self):
        cases = (
            ("---\ntitle: a---b\n---\nBody\n---\nTail", {"title": "a---b"}, "Body\n---\nTail"),
            ("---\r\ntitle: a---b\r\n---\r\nBody\r\n", {"title": "a---b"}, "Body\r\n"),
            ("---\ntitle: empty body\n---", {"title": "empty body"}, ""),
            ("---\r\ntitle: empty body\r\n---", {"title": "empty body"}, ""),
            ("---\n---\nBody", {}, "Body"),
            ("---abc\nBody", {}, "---abc\nBody"),
            ("  ---\nBody", {}, "  ---\nBody"),
            ("Body\n---\nTail", {}, "Body\n---\nTail"),
        )
        for raw, metadata, body in cases:
            with self.subTest(raw=raw):
                self.assertEqual(canon.parse_frontmatter(raw), (metadata, body))

    def test_open_frontmatter_requires_a_closing_line_and_a_mapping(self):
        cases = (
            ("---", "not closed"),
            ("---\ntitle: unclosed\n", "not closed"),
            ("---\ntitle: value\n--- not a delimiter\n", "not closed"),
            ("---\ntitle: [\n---\nBody", "invalid YAML"),
            ("---\ncreated: 2026-13-01\n---\nBody", "invalid YAML"),
            ("---\n- value\n---\nBody", "must be a mapping"),
            ("---\n[]\n---\nBody", "must be a mapping"),
            ("---\nfalse\n---\nBody", "must be a mapping"),
        )
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(MemoryValidationError, message):
                canon.parse_frontmatter(raw)


class FactFormatRoundTripTests(CanonCase):
    def test_writer_export_and_index_reader_keep_metadata_with_inline_dashes(self):
        proposal = propose_memory_fact(
            self.data_dir,
            actor=ACTOR,
            scope="global",
            slug="inline-dashes",
            fact_file=self.fact_file(f"---\r\nsource: {ACTOR}\r\ntitle: a---b\r\n---\r\nBody\r\n---\r\nTail"),
        )
        commit_memory_proposal(self.data_dir, self.instance, actor=ACTOR, propose_id=proposal.propose_id)
        export_memory_snapshot(self.data_dir, self.instance)

        stored = (self.facts / "global" / "inline-dashes.md").read_bytes().decode("utf-8")
        rows = [json.loads(line) for line in (self.memory_dir / "export.ndjson").read_text().splitlines()]
        exported = next(row for row in rows if row["id"] == "global/inline-dashes")
        indexed = parse_fact_text(exported["text"], exported["path"])
        self.assertEqual(exported["text"], stored)
        self.assertEqual(exported["metadata"]["title"], "a---b")
        self.assertEqual(indexed["meta"]["title"], "a---b")
        self.assertEqual(indexed["source"], ACTOR)
        self.assertEqual(indexed["text"], "Body\n---\nTail")

    def test_export_reads_crlf_metadata_and_keeps_the_original_text(self):
        raw = f"---\r\nsource: {ACTOR}\r\ntitle: a---b\r\n---\r\nBody\r\n"
        self.seed({"global/crlf.md": raw})
        export_memory_snapshot(self.data_dir, self.instance)
        rows = [json.loads(line) for line in (self.memory_dir / "export.ndjson").read_text().splitlines()]
        exported = next(row for row in rows if row["id"] == "global/crlf")
        self.assertEqual(exported["text"].encode("utf-8"), raw.encode("utf-8"))
        self.assertEqual(exported["metadata"], {"source": ACTOR, "title": "a---b"})

    def test_bad_frontmatter_is_refused_by_writer_but_preserved_in_export(self):
        cases = (
            "---\r\nsource: unclosed\r\n",
            "---\n- value\n---\nBody",
            "---\ntitle: [\n---\nBody",
            "---\r\ncreated: 2026-13-01\r\n---\r\nBody\r\n",
        )
        for number, raw in enumerate(cases):
            with self.subTest(raw=raw):
                before = self.canon_files()
                with self.assertRaises(MemoryValidationError):
                    propose_memory_fact(
                        self.data_dir,
                        actor=ACTOR,
                        scope="global",
                        slug=f"bad-{number}",
                        fact_file=self.fact_file(raw),
                        source=ACTOR,
                    )
                self.assertEqual(self.canon_files(), before)
                self.assertFalse((self.memory_dir / ".staging").exists())
                self.seed({f"global/bad-{number}.md": raw})
        export_memory_snapshot(self.data_dir, self.instance)
        rows = {
            row["id"]: row
            for row in (
                json.loads(line) for line in (self.memory_dir / "export.ndjson").read_text().splitlines()
            )
        }
        for number, raw in enumerate(cases):
            exported = rows[f"global/bad-{number}"]
            self.assertEqual(exported["text"].encode("utf-8"), raw.encode("utf-8"))
            self.assertEqual(exported["metadata"], {})
            with self.assertRaises(MemoryValidationError):
                parse_fact_text(exported["text"], exported["path"])


class FactVerificationTests(CanonCase):
    def test_malformed_fact_is_a_verification_finding_after_export(self):
        write_index(
            self.memory_dir / "index.sqlite",
            {fact_id: path.read_bytes().decode("utf-8") for fact_id, path in canon.fact_files(self.facts)},
        )
        self.seed({"global/keep.md": "---\n- not a mapping\n---\nBody\n"})
        export_memory_snapshot(self.data_dir, self.instance)

        report = verify_memory_journal(self.data_dir, self.instance)

        self.assertFalse(report.ok)
        self.assertIn("memory canon facts the index cannot parse: global/keep", report.findings)


class DataManifestTests(unittest.TestCase):
    def test_manifest_describes_instance_facts_and_data_exports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            instance.mkdir()
            data_dir = root / "separate-data"
            layout = init_layout(data_dir)
            manifest = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
            memory = manifest["components"]["memory"]
            facts, _ = memory_journal.init_memory_journal(instance)
            self.assertEqual(instance / memory["facts"], facts)
            (facts / "global").mkdir()
            (facts / "global" / "one.md").write_text("One fact\n", encoding="utf-8")

            export_memory(data_dir, instance)

            self.assertTrue((data_dir / memory["path"]).is_dir())
            exported = [json.loads(line) for line in (data_dir / memory["export"]).read_text().splitlines()]
            self.assertEqual([row["id"] for row in exported], ["global/one"])
            self.assertFalse((data_dir / memory["facts"]).exists())
