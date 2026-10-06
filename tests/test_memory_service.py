import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.memory_errors import MemoryValidationError

try:
    import numpy as np

    from ummanu import memory_reindex, memory_service
except ImportError:  # The base install deliberately excludes the heavy memory extra.
    np = None
    memory_reindex = None
    memory_service = None


@unittest.skipIf(memory_service is None, "ummanu[memory] is not installed")
class IncrementalMemoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.canon = self.root / "facts"
        self.canon.mkdir()
        self.export = self.root / "export.ndjson"
        self.db = self.root / "index.sqlite"
        self.calls = []

    def tearDown(self):
        if memory_service is not None:
            memory_service.mark_search_not_ready()
        self.temp.cleanup()

    def embed(self, text):
        self.calls.append(text)
        if text == "explode":
            raise RuntimeError("forced embedding failure")
        raw = sum(text.encode("utf-8")) or 1
        return np.asarray([raw % 11, raw % 13, raw % 17, raw % 19], dtype=np.float32)

    def write_export(self, facts):
        with self.export.open("w", encoding="utf-8") as handle:
            for fact_id, text in facts:
                raw = "---\nsource: test\ncreated: 2026-07-17\n---\n" + text + "\n"
                handle.write(
                    json.dumps(
                        {
                            "id": fact_id,
                            "path": f"{fact_id}.md",
                            "text": raw,
                        }
                    )
                    + "\n"
                )

    def rows(self):
        conn = memory_service.db(self.db)
        try:
            return {
                fact_id: (rowid, text)
                for rowid, fact_id, text in conn.execute(
                    "SELECT id, fact_id, text FROM memories ORDER BY fact_id"
                )
            }
        finally:
            conn.close()

    def update(self):
        return memory_service.incremental_update(
            self.canon,
            self.export,
            self.db,
            "test-model",
            4,
            document_embed=self.embed,
            allow_empty=True,
        )

    def test_add_update_delete_reuses_unchanged_embedding(self):
        self.write_export([("global/a", "alpha"), ("global/b", "bravo")])
        first = self.update()
        self.assertEqual(first["mode"], "rebuild")
        before = self.rows()
        self.calls.clear()

        self.write_export([("global/a", "alpha"), ("global/b", "bravo changed"), ("global/c", "charlie")])
        second = self.update()
        middle = self.rows()
        self.assertEqual(
            {key: second[key] for key in ("added", "updated", "deleted", "reused")},
            {"added": 1, "updated": 1, "deleted": 0, "reused": 1},
        )
        self.assertCountEqual(self.calls, ["bravo changed", "charlie"])
        self.assertEqual(before["global/a"][0], middle["global/a"][0])

        self.calls.clear()
        self.write_export([("global/a", "alpha"), ("global/c", "charlie")])
        third = self.update()
        self.assertEqual(third["deleted"], 1)
        self.assertEqual(third["reused"], 2)
        self.assertEqual(self.calls, [])
        self.assertNotIn("global/b", self.rows())

    def test_po_review_directory_has_a_distinct_non_project_scope(self):
        fact = memory_service.parse_fact_text(
            "---\nsource: curator\n---\nNeeds manual ownership review.\n",
            "po-review/ambiguous-conclusion.md",
        )

        self.assertEqual(fact["id"], "po-review/ambiguous-conclusion")
        self.assertEqual(fact["scope"], "review:po")

    def test_embedding_failure_preserves_index(self):
        self.write_export([("global/a", "alpha")])
        self.update()
        before = self.rows()
        self.write_export([("global/a", "explode")])
        with self.assertRaisesRegex(RuntimeError, "forced embedding failure"):
            self.update()
        self.assertEqual(self.rows(), before)

    def test_malformed_frontmatter_preserves_index_and_reports_a_controlled_failure(self):
        self.write_export([("global/a", "alpha")])
        self.update()
        before = self.db.read_bytes()
        self.calls.clear()
        for source in ("export", "canon"):
            with self.subTest(source=source):
                good = "---\nsource: test\n---\nchanged alpha\n"
                bad = "---\n- not a mapping\n---\nBody\n"
                if source == "export":
                    self.export.write_text(
                        "".join(
                            json.dumps({"id": fact_id, "path": f"{fact_id}.md", "text": raw}) + "\n"
                            for fact_id, raw in (("global/a", good), ("global/b", bad))
                        ),
                        encoding="utf-8",
                    )
                else:
                    self.export.unlink()
                    (self.canon / "global").mkdir()
                    (self.canon / "global" / "a.md").write_text(good, encoding="utf-8")
                    (self.canon / "global" / "b.md").write_text(bad, encoding="utf-8")
                with self.assertRaisesRegex(MemoryValidationError, "frontmatter must be a mapping"):
                    self.update()
                with self.assertRaisesRegex(MemoryValidationError, "frontmatter must be a mapping"):
                    memory_reindex.rebuild(
                        self.canon, self.export, self.db, "test-model", 4, document_embed=self.embed
                    )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = memory_reindex.main(
                        [
                            "--canon",
                            str(self.canon),
                            "--export",
                            str(self.export),
                            "--target-db",
                            str(self.db),
                            "--model",
                            "test-model",
                            "--dim",
                            "4",
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"ok": False, "error": "fact frontmatter must be a mapping"},
                )
                self.assertEqual(self.db.read_bytes(), before)
                self.assertEqual(self.calls, [])

    def test_without_an_export_the_canon_files_of_a_non_git_root_are_read(self):
        (self.canon / "global").mkdir()
        (self.canon / "global" / "a.md").write_text("---\nsource: test\n---\nalpha\n", encoding="utf-8")
        (self.canon / "global" / "link.md").symlink_to(self.canon / "global" / "a.md")
        (self.canon / "global" / ".a.md.123.tmp").write_text("partial\n", encoding="utf-8")
        self.assertFalse(self.export.exists())

        with mock.patch("subprocess.run", side_effect=AssertionError("no Git child")):
            facts = memory_service.load_canon_entries(self.canon, self.export)

        self.assertEqual([fact["id"] for fact in facts], ["global/a"])
        self.assertEqual(facts[0]["text"], "alpha")
        self.assertEqual(facts[0]["scope"], "global")

    def test_the_canon_signature_follows_the_files_without_git(self):
        (self.canon / "global").mkdir()
        fact = self.canon / "global" / "a.md"
        fact.write_text("alpha\n", encoding="utf-8")
        with (
            mock.patch.object(memory_service, "CANON", self.canon),
            mock.patch.object(memory_service, "CANON_EXPORT", self.export),
            mock.patch("subprocess.run", side_effect=AssertionError("no Git child")),
        ):
            before = memory_service.canon_signature()
            fact.write_text("alpha, longer now\n", encoding="utf-8")
            after = memory_service.canon_signature()

        self.assertNotEqual(before, after)

    def test_bootstrap_reconciles_compatible_index_without_full_rebuild(self):
        self.write_export([("global/a", "alpha")])
        self.update()
        self.calls.clear()

        with (
            mock.patch.object(memory_service, "CANON", self.canon),
            mock.patch.object(memory_service, "CANON_EXPORT", self.export),
            mock.patch.object(memory_service, "DB_PATH", str(self.db)),
            mock.patch.object(memory_service, "MODEL", "test-model"),
            mock.patch.object(memory_service, "DIM", 4),
            mock.patch.object(memory_service, "embedder") as warm_embedder,
            mock.patch.object(memory_service, "embed_doc", side_effect=self.embed),
            mock.patch.object(
                memory_service, "update_index", wraps=memory_service.update_index
            ) as update_index,
            mock.patch.object(
                memory_service,
                "offline_rebuild",
                side_effect=AssertionError("compatible bootstrap must not rebuild"),
            ),
        ):
            indexed = memory_service.bootstrap_index()

        self.assertEqual(indexed, 1)
        warm_embedder.assert_called_once_with()
        update_index.assert_called_once_with()
        self.assertEqual(self.calls, [])

    def test_embedder_uses_configured_persistent_cache_and_thread_limit(self):
        fake_embedding = mock.Mock()
        with (
            mock.patch.object(memory_service, "_embedder", None),
            mock.patch.object(memory_service, "TextEmbedding", return_value=fake_embedding) as embedding,
            mock.patch.object(memory_service, "MODEL", "test-model"),
            mock.patch.object(memory_service, "MODEL_CACHE_DIR", self.root / "fastembed-cache"),
            mock.patch.object(memory_service, "THREADS", 1),
        ):
            self.assertIs(memory_service.embedder(), fake_embedding)

        embedding.assert_called_once_with(
            model_name="test-model", cache_dir=str(self.root / "fastembed-cache"), threads=1
        )

    def test_offline_rebuild_fallback_uses_configured_cache_and_thread_limit(self):
        self.write_export([("global/a", "alpha")])
        cache_dir = self.root / "fastembed-cache"
        with mock.patch.object(
            memory_service, "build_document_embedder", return_value=self.embed
        ) as build_embedder:
            result = memory_service.offline_rebuild(
                self.canon,
                self.export,
                self.db,
                "test-model",
                4,
                cache_dir=cache_dir,
                threads=2,
            )

        self.assertTrue(result["ok"])
        build_embedder.assert_called_once_with("test-model", cache_dir, 2)

    def test_reindex_entrypoint_forwards_cache_and_thread_limit(self):
        cache_dir = self.root / "fastembed-cache"
        with mock.patch.object(memory_service, "offline_rebuild", return_value={"ok": True}) as rebuild:
            self.assertEqual(
                memory_reindex.rebuild(
                    self.canon,
                    self.export,
                    self.db,
                    "test-model",
                    4,
                    cache_dir=cache_dir,
                    threads=2,
                ),
                {"ok": True},
            )

        rebuild.assert_called_once_with(
            self.canon, self.export, self.db, "test-model", 4, None, False, cache_dir, 2
        )


@unittest.skipIf(memory_service is None, "ummanu[memory] is not installed")
class MemoryReadAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "index.sqlite"
        self.log = Path(self.temp.name) / "search-log.jsonl"
        conn = memory_service.db(self.db)
        try:
            memory_service.create_schema(conn, 4)
            conn.executemany(
                "INSERT INTO memories(text, scope, tags, source, created_at) VALUES (?,?,?,?,?)",
                [
                    ("allowed project fact", "project:alpha", None, None, None),
                    ("foreign project fact", "project:foreign", None, None, None),
                    ("Ummanu development fact", "project:ummanu", None, None, None),
                    ("pending PO review", "review:po", "pending-review", "curator", None),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self.paths = mock.patch.multiple(memory_service, DB_PATH=str(self.db), SEARCH_LOG=str(self.log))
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def identity(self, scopes):
        return memory_service.memory_access.MemoryReadIdentity(
            "worker", {"kind": "card", "ref": "card-1", "project": "alpha"}, frozenset(scopes), "grant"
        )

    def test_get_and_list_cannot_bypass_the_shared_scope_guard(self):
        with mock.patch.object(memory_service, "read_guard", return_value=self.identity({"project:alpha"})):
            self.assertEqual(memory_service.memory_get(1)["text"], "allowed project fact")
            self.assertEqual(memory_service.memory_get(2), {"error": "not found", "id": 2})
            self.assertEqual(
                [entry["text"] for entry in memory_service.memory_list()], ["allowed project fact"]
            )

    def test_po_can_narrow_to_review_bucket_while_worker_cannot_receive_it(self):
        po = memory_service.memory_access.MemoryReadIdentity(
            "po", {"kind": "interactive", "ref": "interactive"}, None, "po-grant"
        )
        narrowed = memory_service.memory_access.narrow(po, "review:po")
        assert isinstance(narrowed, memory_service.memory_access.MemoryReadIdentity)
        with mock.patch.object(memory_service, "read_guard", return_value=narrowed):
            self.assertEqual([entry["text"] for entry in memory_service.memory_list()], ["pending PO review"])

        worker = self.identity({"project:alpha", "product:ummanu"})
        self.assertIsInstance(
            memory_service.memory_access.narrow(worker, "review:po"),
            memory_service.memory_access.MemoryAccessDenial,
        )

    def test_missing_runtime_identity_returns_a_data_free_denial_for_every_read(self):
        denial = memory_service.memory_access.MemoryAccessDenial("runtime_identity_missing")
        with mock.patch.object(memory_service, "read_guard", return_value=denial):
            self.assertEqual(memory_service.memory_search("ignored", caller="po"), denial.response())
            self.assertEqual(memory_service.memory_get(1), denial.response())
            self.assertEqual(memory_service.memory_list(), [denial.response()])

    def test_search_uses_the_resolved_scope_once_and_ignores_spoofed_caller(self):
        identity = self.identity({"project:alpha", "product:ummanu"})
        with (
            mock.patch.object(memory_service, "read_guard", return_value=identity) as guard,
            mock.patch.object(memory_service, "search_ready", return_value=True),
            mock.patch.object(memory_service, "search_memory", return_value=[]) as search,
        ):
            self.assertEqual(memory_service.memory_search("query", scope="project:alpha", caller="po"), [])

        guard.assert_called_once_with("project:alpha")
        search.assert_called_once_with("query", 5, allowed_scopes=identity.scopes)

    def test_audit_log_has_resolved_identity_not_fact_text_or_capability(self):
        identity = self.identity({"project:alpha"})
        memory_service.log_read("memory_get", identity, "allowed", results=[{"id": 1, "score": 0.9}])

        entry = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(entry["role"], "worker")
        self.assertEqual(entry["scopes"], ["project:alpha"])
        self.assertNotIn("allowed project fact", json.dumps(entry))
        self.assertNotIn("grant", json.dumps(entry))

    def test_scope_denial_audit_keeps_resolved_identity_and_malformed_scope_is_typed(self):
        identity = self.identity({"project:alpha"})
        with mock.patch.object(
            memory_service,
            "read_guard",
            return_value=memory_service.memory_access.MemoryAccessDenial("scope_malformed", identity),
        ):
            denial = memory_service.memory_search("not logged", scope="pro ject")

        self.assertEqual(denial, {"status": "denied", "error": "scope_malformed"})
        entry = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(entry["outcome"], "scope_malformed")
        self.assertEqual(entry["role"], "worker")
        self.assertEqual(entry["subject"], identity.subject)
        self.assertEqual(entry["scopes"], ["project:alpha"])
        self.assertNotIn("not logged", json.dumps(entry))

    def test_restore_uses_index_primitives_because_direct_mcp_calls_are_denied(self):
        denial = memory_service.memory_access.MemoryAccessDenial("runtime_identity_missing")
        with mock.patch.object(memory_service, "read_guard", return_value=denial):
            self.assertEqual(memory_service.memory_list(), [denial.response()])
        self.assertEqual(
            [entry["text"] for entry in memory_service.list_memory_entries()],
            [
                "pending PO review",
                "Ummanu development fact",
                "foreign project fact",
                "allowed project fact",
            ],
        )
        self.assertEqual(memory_service.get_memory_entry(1)["text"], "allowed project fact")


if __name__ == "__main__":
    unittest.main()
