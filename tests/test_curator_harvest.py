from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.automations.agents.curator import cli, harvest
from ummanu.runtime.state import PRECHECK_DEFERRED, PRECHECK_SKIP, AgentState


def claude(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def claude_tool() -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "read"}]}}
    ) + "\n"


def codex(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    ) + "\n"


class CuratorHarvestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = AgentState("curator", self.root / "state")
        self.identity = {"workspace": str(self.root / "curator")}
        self.limits = harvest.Limits(2, 10_000, 8, 20, 4096, 4096)
        self.patches = [
            mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.codex_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.hermes_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.all_memory_files", return_value=[]),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def _persist(self, batch):
        base = {key: self.state.load_watermark().get(key) for key in batch["pending"]}
        record = harvest.pending_record(batch, self.identity, base)
        self.state.ensure_dir()
        self.state.pending_file.write_text(json.dumps(record), encoding="utf-8")
        return record

    def test_partial_jsonl_advances_only_the_last_emitted_record(self) -> None:
        path = self.root / "claude.jsonl"
        path.write_text(claude("one") + claude("two") + claude("three"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            first = harvest.harvest(self.state, self.identity, self.limits)
            self.assertEqual([t["text"] for t in first["sessions"][0]["turns"]], ["one", "two"])
            record = self._persist(first)
            self.assertEqual(harvest.harvest(self.state, self.identity, self.limits), first)
            harvest.advance(self.state, record, self.identity)
            self.state.pending_file.unlink()
            second = harvest.harvest(self.state, self.identity, self.limits)
        self.assertEqual([t["text"] for t in second["sessions"][0]["turns"]], ["three"])
        self.assertEqual(first["pending"][str(path)]["offset"], len(claude("one") + claude("two")))

    def test_partial_hermes_rows_resume_at_last_selected_id(self) -> None:
        session = {"head": "hermes", "path": "state.db", "session_id": "h", "cwd": "/project"}
        rows = [
            {"id": 1, "role": "user", "content": "one", "timestamp": 0, "content_bytes": 3},
            {"id": 2, "role": "assistant", "content": "two", "timestamp": 0, "content_bytes": 3},
            {"id": 3, "role": "user", "content": "three", "timestamp": 0, "content_bytes": 5},
        ]
        def messages(_session, since, *_limits):
            return [row for row in rows if row["id"] > since]
        with mock.patch("ummanu.automations.agents.curator.discover.hermes_sessions", return_value=[session]), mock.patch(
            "ummanu.automations.agents.curator.discover.hermes_messages", side_effect=messages
        ):
            first = harvest.harvest(self.state, self.identity, self.limits)
            record = self._persist(first)
            harvest.advance(self.state, record, self.identity)
            self.state.pending_file.unlink()
            second = harvest.harvest(self.state, self.identity, self.limits)
        self.assertEqual(first["pending"]["hermes:h"], {"last_id": 2})
        self.assertEqual([t["text"] for t in second["sessions"][0]["turns"]], ["three"])

    def test_jsonl_noise_prefix_advances_across_row_limited_cycles(self) -> None:
        path = self.root / "claude.jsonl"
        oversized = "x" * 300 + "\n"
        path.write_text(claude_tool() * 4 + oversized + claude("real"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        limits = harvest.Limits(2, 10_000, 8, 2, 256, 4096)
        offsets = []
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            for expected_turns in ([], [], ["real"]):
                batch = harvest.harvest(self.state, self.identity, limits)
                turns = [turn["text"] for entry in batch["sessions"] for turn in entry["turns"]]
                self.assertEqual(turns, expected_turns)
                offsets.append(batch["pending"][str(path)]["offset"])
                record = self._persist(batch)
                harvest.advance(self.state, record, self.identity)
                self.state.pending_file.unlink()
        self.assertEqual(offsets, [len(claude_tool()) * 2, len(claude_tool()) * 4, path.stat().st_size])
        self.assertEqual(harvest.harvest(self.state, self.identity, limits)["pending"], {})

    def test_hermes_noise_prefix_advances_across_row_limited_cycles(self) -> None:
        session = {"head": "hermes", "path": "state.db", "session_id": "h", "cwd": "/project"}
        rows = [
            {"id": index, "role": "tool", "content": "ignored", "timestamp": 0, "content_bytes": 7}
            for index in range(1, 5)
        ] + [
            # discover.hermes_messages blanks content over the configured byte cap.
            {"id": 5, "role": "user", "content": "", "timestamp": 0, "content_bytes": 5000}
        ] + [{"id": 6, "role": "user", "content": "real", "timestamp": 0, "content_bytes": 4}]

        def messages(_session, since, limit, _max_record_bytes):
            return [row for row in rows if row["id"] > since][:limit]

        limits = harvest.Limits(2, 10_000, 8, 2, 4096, 4096)
        cursors = []
        with mock.patch("ummanu.automations.agents.curator.discover.hermes_sessions", return_value=[session]), mock.patch(
            "ummanu.automations.agents.curator.discover.hermes_messages", side_effect=messages
        ):
            for expected_turns in ([], [], ["real"]):
                batch = harvest.harvest(self.state, self.identity, limits)
                turns = [turn["text"] for entry in batch["sessions"] for turn in entry["turns"]]
                self.assertEqual(turns, expected_turns)
                cursors.append(batch["pending"]["hermes:h"]["last_id"])
                record = self._persist(batch)
                harvest.advance(self.state, record, self.identity)
                self.state.pending_file.unlink()
        self.assertEqual(cursors, [2, 4, 6])
        self.assertEqual(harvest.harvest(self.state, self.identity, limits)["pending"], {})

    def test_jsonl_incomplete_trailing_record_keeps_a_source_local_cursor(self) -> None:
        path = self.root / "claude.jsonl"
        path.write_text(claude_tool() + claude("still-writing").rstrip("\n"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        limits = harvest.Limits(2, 10_000, 8, 4, 256, 4096)
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            first = harvest.harvest(self.state, self.identity, limits)
        self.assertEqual(first["partial_sources"], [{"head": "claude", "path": str(path), "session_id": "c"}])
        self.assertEqual(first["pending"][str(path)]["offset"], len(claude_tool()))
        self.assertIn("Source-local partial JSONL tails", harvest.render_markdown(first))
        record = self._persist(first)
        harvest.advance(self.state, record, self.identity)
        self.assertEqual(self.state.load_watermark()[str(path)]["offset"], len(claude_tool()))

    def test_incomplete_legacy_pending_is_refused_without_advancing(self) -> None:
        path = self.root / "claude.jsonl"
        batch = {
            "sessions": [{"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project", "turns": [{"role": "user", "text": "fact", "ts": None}]}],
            "memory": [],
            "pending": {str(path): {"offset": 1}},
            "rejected": [],
            "incomplete": True,
        }
        record = harvest.pending_record(batch, self.identity, {str(path): None})
        with self.assertRaisesRegex(harvest.PendingError, "incomplete"):
            harvest.advance(self.state, record, self.identity)
        self.assertEqual(self.state.load_watermark(), {})

    def test_memory_is_selected_and_large_memory_is_rejected_before_prompt(self) -> None:
        small, large = self.root / "small.md", self.root / "large.md"
        small.write_text("durable memory", encoding="utf-8")
        large.write_text("x" * 50, encoding="utf-8")
        files = [
            {"head": "claude", "path": str(small), "cwd": "/project"},
            {"head": "hermes", "path": str(large), "cwd": ""},
        ]
        limits = harvest.Limits(5, 100, 5, 10, 100, 20)
        with mock.patch("ummanu.automations.agents.curator.discover.all_memory_files", return_value=files):
            batch = harvest.harvest(self.state, self.identity, limits)
        self.assertEqual(batch["memory"][0]["text"], "durable memory")
        self.assertEqual(batch["rejected"][0]["reason"], "memory-file-too-large")
        self.assertNotIn(str(large), batch["pending"])

    def test_mismatched_or_stale_pending_never_moves_a_watermark(self) -> None:
        path = self.root / "claude.jsonl"
        path.write_text(claude("one"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            batch = harvest.harvest(self.state, self.identity, self.limits)
        record = self._persist(batch)
        with self.assertRaises(harvest.PendingError):
            harvest.advance(self.state, record, {"workspace": "/other"})
        self.assertEqual(self.state.load_watermark(), {})
        self.state.save_watermark({str(path): {"offset": 1}})
        with self.assertRaises(harvest.PendingError):
            harvest.advance(self.state, record, self.identity)
        self.assertEqual(self.state.load_watermark(), {str(path): {"offset": 1}})

    def test_legacy_watermark_is_read_and_upgraded_only_when_selected(self) -> None:
        path = self.root / "claude.jsonl"
        first = claude("old")
        path.write_text(first, encoding="utf-8")
        old_stat = path.stat()
        path.write_text(first + claude("new"), encoding="utf-8")
        self.state.save_watermark({str(path): {"lines": 1, "mtime": old_stat.st_mtime, "size": old_stat.st_size}})
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            batch = harvest.harvest(self.state, self.identity, self.limits)
        self.assertEqual([t["text"] for t in batch["sessions"][0]["turns"]], ["new"])
        record = self._persist(batch)
        harvest.advance(self.state, record, self.identity)
        self.assertIn("offset", self.state.load_watermark()[str(path)])

    def test_an_already_advanced_record_fails_closed(self) -> None:
        path = self.root / "claude.jsonl"
        path.write_text(claude("one"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            batch = harvest.harvest(self.state, self.identity, self.limits)
        record = self._persist(batch)
        harvest.advance(self.state, record, self.identity)
        advanced = self.state.load_watermark()
        with self.assertRaisesRegex(harvest.PendingError, "stale"):
            harvest.advance(self.state, record, self.identity)
        self.assertEqual(self.state.load_watermark(), advanced)

    def test_source_order_and_limit_are_fixed_before_rendering(self) -> None:
        a, b = self.root / "a.jsonl", self.root / "b.jsonl"
        a.write_text(claude("a"), encoding="utf-8")
        b.write_text(claude("b"), encoding="utf-8")
        sessions = [
            {"head": "claude", "path": str(b), "session_id": "b", "cwd": "/project"},
            {"head": "claude", "path": str(a), "session_id": "a", "cwd": "/project"},
        ]
        limits = harvest.Limits(1, 10_000, 1, 20, 4096, 4096)
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            batch = harvest.harvest(self.state, self.identity, limits)
        self.assertEqual([s["path"] for s in batch["sessions"]], [str(a)])
        self.assertNotIn("\nb\n", harvest.render_markdown(batch))

    def test_legacy_pending_is_refused_without_overwrite(self) -> None:
        self.state.ensure_dir()
        self.state.pending_file.write_text(json.dumps({"old": {"lines": 3}}), encoding="utf-8")
        with self.assertRaisesRegex(harvest.PendingError, "legacy"):
            harvest.harvest(self.state, self.identity, self.limits)
        self.assertEqual(json.loads(self.state.pending_file.read_text(encoding="utf-8")), {"old": {"lines": 3}})

    def test_project_filter_precedes_budget_and_retains_routes_in_output(self) -> None:
        alpha, beta = self.root / "alpha.jsonl", self.root / "beta.jsonl"
        alpha.write_text(claude("alpha"), encoding="utf-8")
        beta.write_text(claude("beta"), encoding="utf-8")
        sessions = [
            {"head": "claude", "path": str(beta), "session_id": "b", "cwd": "/beta", "route": "beta"},
            {"head": "claude", "path": str(alpha), "session_id": "a", "cwd": "/alpha", "route": "alpha"},
        ]
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions), mock.patch(
            "ummanu.automations.agents.curator.discover.registered_project_ids", return_value={"alpha", "beta"}
        ):
            selected = harvest.harvest(self.state, self.identity, self.limits, project="alpha")
            all_backlog = harvest.harvest(self.state, self.identity, self.limits)
        self.assertEqual([entry["route"] for entry in selected["sessions"]], ["alpha"])
        self.assertEqual(selected["project"], "alpha")
        self.assertEqual({entry["route"] for entry in all_backlog["sessions"]}, {"alpha", "beta"})

    def test_project_selector_is_signed_into_pending_replay_and_advance(self) -> None:
        path = self.root / "alpha.jsonl"
        path.write_text(claude("alpha"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "a", "cwd": "/alpha", "route": "alpha"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]), mock.patch(
            "ummanu.automations.agents.curator.discover.registered_project_ids", return_value={"alpha"}
        ):
            batch = harvest.harvest(self.state, self.identity, self.limits, project="alpha")
            record = harvest.pending_record(batch, self.identity, {str(path): None}, project="alpha")
            self.state.ensure_dir()
            self.state.pending_file.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(harvest.PendingError, "selector"):
                harvest.harvest(self.state, self.identity, self.limits)
            with self.assertRaisesRegex(harvest.PendingError, "selector"):
                harvest.advance(self.state, record, self.identity)
            harvest.advance(self.state, record, self.identity, project="alpha")
        self.assertIn(str(path), self.state.load_watermark())

    def test_po_review_selector_is_reserved_and_isolated_from_projects(self) -> None:
        review, alpha = self.root / "review.jsonl", self.root / "alpha.jsonl"
        review.write_text(claude("cross-project conclusion"), encoding="utf-8")
        alpha.write_text(claude("alpha conclusion"), encoding="utf-8")
        sessions = [
            {
                "head": "claude",
                "path": str(review),
                "session_id": "review",
                "cwd": "/observer",
                "route": "review:po",
            },
            {"head": "claude", "path": str(alpha), "session_id": "alpha", "cwd": "/alpha", "route": "alpha"},
        ]
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions), mock.patch(
            "ummanu.automations.agents.curator.discover.registered_project_ids", return_value={"alpha"}
        ):
            batch = harvest.harvest(self.state, self.identity, self.limits, project="review:po")
            cutoff = harvest.baseline_cutoff(self.state, "review:po", self.limits)

        self.assertEqual(batch["project"], "review:po")
        self.assertEqual([entry["session_id"] for entry in batch["sessions"]], ["review"])
        self.assertEqual(set(cutoff["pending"]), {str(review)})

    def test_unknown_selector_is_reserved_and_isolated_from_projects(self) -> None:
        unknown, alpha = self.root / "unknown.jsonl", self.root / "alpha.jsonl"
        unknown.write_text(claude("owner unresolved"), encoding="utf-8")
        alpha.write_text(claude("alpha conclusion"), encoding="utf-8")
        sessions = [
            {"head": "claude", "path": str(unknown), "session_id": "u", "cwd": "/tmp", "route": "unknown"},
            {"head": "claude", "path": str(alpha), "session_id": "a", "cwd": "/alpha", "route": "alpha"},
        ]
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions), mock.patch(
            "ummanu.automations.agents.curator.discover.registered_project_ids", return_value={"alpha"}
        ):
            batch = harvest.harvest(self.state, self.identity, self.limits, project="unknown")
            cutoff = harvest.baseline_cutoff(self.state, "unknown", self.limits)

        self.assertEqual(batch["project"], "unknown")
        self.assertEqual([entry["session_id"] for entry in batch["sessions"]], ["u"])
        self.assertEqual(set(cutoff["pending"]), {str(unknown)})

    def test_backlog_summary_is_metadata_only_and_state_free(self) -> None:
        transcript, beta, memory = self.root / "alpha.jsonl", self.root / "beta.jsonl", self.root / "memory.md"
        transcript.write_text(claude("a secret transcript"), encoding="utf-8")
        beta.write_text(codex("a separate secret transcript"), encoding="utf-8")
        memory.write_text("a secret personal memory", encoding="utf-8")
        sessions = [
            {"head": "claude", "path": str(transcript), "session_id": "a", "cwd": "/alpha", "route": "alpha"},
            {"head": "codex", "path": str(beta), "session_id": "b", "cwd": "/beta", "route": "beta"},
        ]
        memories = [{"head": "claude", "path": str(memory), "cwd": "/alpha", "route": "alpha"}]
        before = self.state.load_watermark()
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[sessions[0]]), mock.patch(
            "ummanu.automations.agents.curator.discover.codex_sessions", return_value=[sessions[1]]
        ), mock.patch(
            "ummanu.automations.agents.curator.discover.all_memory_files", return_value=memories
        ), mock.patch("ummanu.automations.agents.curator.discover.registered_project_ids", return_value={"alpha", "beta"}):
            summary = harvest.backlog(self.state, project="alpha", limits=self.limits)
            all_summary = harvest.backlog(self.state, limits=self.limits)
        self.assertEqual(summary["groups"][0]["project"], "alpha")
        self.assertEqual(summary["groups"][0]["signal_turn_count"], 1)
        self.assertEqual(summary["groups"][0]["memory_file_count"], 1)
        self.assertEqual([group["project"] for group in all_summary["groups"]], ["alpha", "beta"])
        self.assertNotIn("secret", json.dumps(summary))
        self.assertEqual(self.state.load_watermark(), before)
        self.assertFalse(self.state.pending_file.exists())


class CuratorBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = AgentState("curator", self.root / "state")
        self.identity = harvest.current_identity()
        self.patches = [
            mock.patch.object(cli, "STATE", self.state),
            mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.codex_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.hermes_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.all_memory_files", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.registered_project_ids", return_value={"alpha", "beta"}),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def _sessions(self, *, alpha_text="alpha", beta_text="beta") -> tuple[Path, Path, list[dict]]:
        alpha, beta = self.root / "alpha.jsonl", self.root / "beta.jsonl"
        alpha.write_text(claude(alpha_text), encoding="utf-8")
        beta.write_text(claude(beta_text), encoding="utf-8")
        return alpha, beta, [
            {"head": "claude", "path": str(alpha), "session_id": "a", "cwd": "/alpha", "route": "alpha"},
            {"head": "claude", "path": str(beta), "session_id": "b", "cwd": "/beta", "route": "beta"},
        ]

    def _pending_batch(self, alpha: Path, sessions: list[dict]) -> dict:
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            batch = harvest.harvest(self.state, self.identity, project="alpha")
        record = harvest.pending_record(batch, self.identity, {str(alpha): None}, project="alpha")
        self.state.ensure_dir()
        self.state.pending_file.write_text(json.dumps(record), encoding="utf-8")
        return record

    def _audit_rows(self) -> list[dict]:
        path = self.state.dir / "baseline-audit.ndjson"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_po_review_cutoff_settles_only_multi_project_observer_sources(self) -> None:
        review, alpha = self.root / "review.jsonl", self.root / "alpha.jsonl"
        review.write_text(claude("already reviewed cross-project history"), encoding="utf-8")
        alpha.write_text(claude("unreviewed alpha history"), encoding="utf-8")
        sessions = [
            {
                "head": "claude",
                "path": str(review),
                "session_id": "review",
                "cwd": "/observer",
                "route": "review:po",
            },
            {"head": "claude", "path": str(alpha), "session_id": "alpha", "cwd": "/alpha", "route": "alpha"},
        ]
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            cutoff = harvest.baseline_cutoff(self.state, "review:po")
            audit = cli.baseline_settlement(
                project="review:po",
                actor="operator:alice",
                reason="reviewed multi-project observer backlog",
                cutoff_id=cutoff["cutoff_id"],
            )

        self.assertEqual(set(self.state.load_watermark()), {str(review)})
        self.assertEqual(audit["project"], "review:po")
        self.assertEqual(audit["affected_cursor_count"], 1)
        self.assertEqual(self._audit_rows()[0]["project"], "review:po")

    def test_cutoff_cli_is_project_isolated_and_audits_redacted_metadata_only(self) -> None:
        secret = "sk-proj-abcdefghijklmnopqrstuvwx"
        alpha, beta, sessions = self._sessions(alpha_text=f"alpha transcript {secret}", beta_text="beta transcript")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(cli.cmd_backlog(True, "alpha"), 0)
            backlog = json.loads(output.getvalue())
            cutoff_id = backlog["cutoff"]["id"]
            output.seek(0)
            output.truncate(0)
            self.assertEqual(
                cli.main(
                    [
                        "baseline",
                        "--project",
                        "alpha",
                        "--actor",
                        "operator:alice",
                        "--reason",
                        f"reviewed {secret}",
                        "--cutoff-id",
                        cutoff_id,
                    ]
                ),
                0,
            )

        mark = self.state.load_watermark()
        self.assertIn(str(alpha), mark)
        self.assertNotIn(str(beta), mark)
        self.assertEqual(backlog["cutoff"]["cursor_count"], 1)
        self.assertNotIn("transcript", json.dumps(backlog))
        self.assertNotIn(secret, output.getvalue() + errors.getvalue())
        row = self._audit_rows()[0]
        self.assertEqual(row["project"], "alpha")
        self.assertEqual(row["actor"], "operator:alice")
        self.assertEqual(row["evidence"], {"kind": "cutoff", "id": cutoff_id})
        self.assertEqual(row["affected_cursor_count"], 1)
        self.assertEqual(len(row["affected_cursor_ids"][0]), 64)
        self.assertEqual(row["outcome"], "settled")
        self.assertTrue(row["time"])
        self.assertIn("REDACTED", row["reason"])
        self.assertNotIn(secret, json.dumps(row))
        self.assertNotIn("alpha transcript", json.dumps(row))

    def test_cutoff_proof_is_stale_after_growth_and_retry_cannot_repeat_it(self) -> None:
        alpha, _beta, sessions = self._sessions()
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            cutoff = harvest.baseline_cutoff(self.state, "alpha")
            alpha.write_text(claude("alpha") + claude("newer"), encoding="utf-8")
            with self.assertRaisesRegex(harvest.PendingError, "stale"):
                cli.baseline_settlement(
                    project="alpha", actor="operator", reason="reviewed", cutoff_id=cutoff["cutoff_id"]
                )
            self.assertEqual(self.state.load_watermark(), {})
            self.assertFalse((self.state.dir / "baseline-audit.ndjson").exists())

            current = harvest.baseline_cutoff(self.state, "alpha")
            cli.baseline_settlement(project="alpha", actor="operator", reason="reviewed", cutoff_id=current["cutoff_id"])
            with self.assertRaisesRegex(harvest.PendingError, "stale"):
                cli.baseline_settlement(
                    project="alpha", actor="operator", reason="reviewed", cutoff_id=current["cutoff_id"]
                )
        self.assertEqual(len(self._audit_rows()), 1)

    def test_cutoff_migrates_valid_legacy_line_cursor_without_reprocessing_it(self) -> None:
        alpha, beta, sessions = self._sessions(alpha_text="already reviewed", beta_text="unrelated")
        alpha.write_text(claude("already reviewed") + claude("skip this too"), encoding="utf-8")
        stat = alpha.stat()
        self.state.save_watermark(
            {
                str(alpha): {"lines": 1, "mtime": stat.st_mtime, "size": stat.st_size},
                str(beta): {"lines": 1, "mtime": beta.stat().st_mtime, "size": beta.stat().st_size},
            }
        )
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            cutoff = harvest.baseline_cutoff(self.state, "alpha")
            self.assertEqual(cutoff["base"][str(alpha)]["lines"], 1)
            self.assertEqual(cutoff["pending"][str(alpha)]["offset"], alpha.stat().st_size)
            cli.baseline_settlement(
                project="alpha",
                actor="operator",
                reason="current state reviewed separately",
                cutoff_id=cutoff["cutoff_id"],
            )

        mark = self.state.load_watermark()
        self.assertEqual(mark[str(alpha)]["offset"], alpha.stat().st_size)
        self.assertIn("lines", mark[str(beta)])

    def test_batch_proof_settles_only_its_project_and_rejects_foreign_selector(self) -> None:
        alpha, beta, sessions = self._sessions()
        record = self._pending_batch(alpha, sessions)
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            with self.assertRaisesRegex(harvest.PendingError, "selector"):
                cli.baseline_settlement(
                    project="beta", actor="operator", reason="wrong project", batch_id=record["batch_id"]
                )
            audit = cli.baseline_settlement(
                project="alpha", actor="operator", reason="approved batch", batch_id=record["batch_id"]
            )
        mark = self.state.load_watermark()
        self.assertIn(str(alpha), mark)
        self.assertNotIn(str(beta), mark)
        self.assertFalse(self.state.pending_file.exists())
        self.assertEqual(audit["evidence"]["kind"], "batch")

    def test_batch_proof_rejects_a_foreign_cursor_before_mutation(self) -> None:
        alpha, beta, sessions = self._sessions()
        record = self._pending_batch(alpha, sessions)
        batch = json.loads(json.dumps(record["batch"]))
        stat = beta.stat()
        batch["pending"][str(beta)] = {"offset": stat.st_size, "mtime": stat.st_mtime, "size": stat.st_size}
        foreign = harvest.pending_record(batch, self.identity, {str(alpha): None, str(beta): None}, project="alpha")
        self.state.pending_file.write_text(json.dumps(foreign), encoding="utf-8")
        before_pending = self.state.pending_file.read_bytes()
        with (
            mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions),
            self.assertRaisesRegex(harvest.PendingError, "foreign"),
        ):
            cli.baseline_settlement(
                project="alpha", actor="operator", reason="approved batch", batch_id=foreign["batch_id"]
            )
        self.assertEqual(self.state.load_watermark(), {})
        self.assertEqual(self.state.pending_file.read_bytes(), before_pending)
        self.assertFalse((self.state.dir / "baseline-audit.ndjson").exists())

    def test_audit_write_failure_restores_watermark_and_pending(self) -> None:
        alpha, _beta, sessions = self._sessions()
        record = self._pending_batch(alpha, sessions)
        before_pending = self.state.pending_file.read_bytes()
        errors = io.StringIO()
        with (
            mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions),
            mock.patch.object(cli, "publish_state_atomic", side_effect=OSError("audit unavailable")),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(
                cli.cmd_baseline(
                    project="alpha", actor="operator", reason="approved batch", batch_id=record["batch_id"]
                ),
                1,
            )
        self.assertEqual(self.state.load_watermark(), {})
        self.assertEqual(self.state.pending_file.read_bytes(), before_pending)
        self.assertFalse((self.state.dir / "baseline-audit.ndjson").exists())
        self.assertNotIn("approved batch", errors.getvalue())

    def test_malformed_state_and_invalid_evidence_leave_everything_unchanged(self) -> None:
        alpha, _beta, sessions = self._sessions()
        self.state.save_watermark({str(alpha): {"lines": "one", "mtime": 1, "size": 1}})
        before = self.state.watermark_file.read_bytes()
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            with self.assertRaisesRegex(harvest.PendingError, "malformed or legacy"):
                harvest.baseline_cutoff(self.state, "alpha")
            with self.assertRaisesRegex(harvest.PendingError, "exactly one"):
                cli.baseline_settlement(project="alpha", actor="operator", reason="reviewed")
            with self.assertRaisesRegex(harvest.PendingError, "exactly one"):
                cli.baseline_settlement(
                    project="alpha", actor="operator", reason="reviewed", cutoff_id="a" * 64, batch_id="b" * 64
                )
        self.assertEqual(self.state.watermark_file.read_bytes(), before)
        self.assertFalse((self.state.dir / "baseline-audit.ndjson").exists())


class CuratorCliPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = AgentState("curator", self.root / "state")
        self.patches = [
            mock.patch.object(cli, "STATE", self.state),
            mock.patch("ummanu.automations.agents.curator.discover.codex_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.hermes_sessions", return_value=[]),
            mock.patch("ummanu.automations.agents.curator.discover.all_memory_files", return_value=[]),
            mock.patch.dict(
                "os.environ",
                {
                    "UMMANU_CURATOR_MAX_TURNS": "2",
                    "UMMANU_CURATOR_MAX_INPUT_BYTES": "10000",
                    "UMMANU_CURATOR_MAX_SOURCES": "8",
                    "UMMANU_CURATOR_MAX_ROWS_PER_SOURCE": "2",
                    "UMMANU_CURATOR_MAX_RECORD_BYTES": "4096",
                    "UMMANU_CURATOR_MAX_MEMORY_BYTES": "4096",
                },
                clear=False,
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_precheck_settles_noise_windows_then_exposes_later_turn(self) -> None:
        path = self.root / "noise.jsonl"
        path.write_text(claude_tool() * 4 + claude("real"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            self.assertEqual(cli.cmd_precheck(), PRECHECK_SKIP)
            first = self.state.load_watermark()[str(path)]["offset"]
            self.assertFalse(self.state.pending_file.exists())
            self.assertEqual(cli.cmd_precheck(), PRECHECK_SKIP)
            second = self.state.load_watermark()[str(path)]["offset"]
            self.assertGreater(second, first)
            self.assertEqual(cli.cmd_precheck(), 0)
        self.assertTrue(self.state.pending_file.exists())
        pending = harvest.read_pending(self.state)
        self.assertEqual([turn["text"] for entry in pending["batch"]["sessions"] for turn in entry["turns"]], ["real"])

    def test_cursor_only_harvest_leaves_no_deadlock_or_later_source_hidden(self) -> None:
        noisy = self.root / "a-noise.jsonl"
        noisy.write_text(claude_tool() * 2, encoding="utf-8")
        later = self.root / "b-later.jsonl"
        sessions = [{"head": "claude", "path": str(noisy), "session_id": "a", "cwd": "/project"}]
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            self.assertEqual(cli.cmd_harvest(False), 0)
            self.assertFalse(self.state.pending_file.exists())
            self.assertIn(str(noisy), self.state.load_watermark())
            later.write_text(claude("later"), encoding="utf-8")
            sessions.append({"head": "claude", "path": str(later), "session_id": "b", "cwd": "/project"})
            self.assertEqual(cli.cmd_precheck(), 0)
        self.assertTrue(self.state.pending_file.exists())
        pending = harvest.read_pending(self.state)
        self.assertEqual([turn["text"] for entry in pending["batch"]["sessions"] for turn in entry["turns"]], ["later"])

    def test_valid_pending_replays_before_fresh_discovery(self) -> None:
        path = self.root / "session.jsonl"
        path.write_text(claude("first"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            self.assertEqual(cli.cmd_harvest(True), 0)
            first = json.loads(stdout.getvalue())
            stdout.seek(0)
            stdout.truncate(0)
            path.write_text(claude("first") + claude("second"), encoding="utf-8")
            self.assertEqual(cli.cmd_harvest(True), 0)
            second = json.loads(stdout.getvalue())
        self.assertEqual(second, first)
        self.assertEqual(self.state.load_watermark(), {})

    def test_partial_tail_settles_complete_prefix_and_retries_once_completed(self) -> None:
        path = self.root / "writing.jsonl"
        path.write_text(claude_tool() + claude("still-writing").rstrip("\n"), encoding="utf-8")
        session = {"head": "claude", "path": str(path), "session_id": "c", "cwd": "/project"}
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[session]):
            self.assertEqual(cli.cmd_precheck(), PRECHECK_SKIP)
            self.assertEqual(self.state.load_watermark()[str(path)]["offset"], len(claude_tool()))
            self.assertFalse(self.state.pending_file.exists())
            path.write_text(claude_tool() + claude("still-writing"), encoding="utf-8")
            self.assertEqual(cli.cmd_precheck(), 0)
            self.assertEqual(cli.cmd_advance(), 0)
            self.assertEqual(cli.cmd_precheck(), PRECHECK_SKIP)
        self.assertEqual(self.state.load_watermark()[str(path)]["offset"], path.stat().st_size)
        self.assertFalse(self.state.pending_file.exists())

    def test_partial_tail_does_not_suppress_later_fact_or_safe_cursor_settlement(self) -> None:
        partial = self.root / "a-writing.jsonl"
        partial.write_text(claude_tool() + claude("still-writing").rstrip("\n"), encoding="utf-8")
        healthy = self.root / "b-live.jsonl"
        healthy.write_text(claude("fact"), encoding="utf-8")
        sessions = [
            {"head": "claude", "path": str(partial), "session_id": "a", "cwd": "/project"},
            {"head": "claude", "path": str(healthy), "session_id": "b", "cwd": "/project"},
        ]
        with mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=sessions):
            self.assertEqual(cli.cmd_precheck(), 0)
            pending = harvest.read_pending(self.state)
            self.assertEqual([turn["text"] for entry in pending["batch"]["sessions"] for turn in entry["turns"]], ["fact"])
            self.assertEqual(pending["batch"]["partial_sources"][0]["path"], str(partial))
            self.assertEqual(cli.cmd_advance(), 0)
        mark = self.state.load_watermark()
        self.assertEqual(mark[str(partial)]["offset"], len(claude_tool()))
        self.assertEqual(mark[str(healthy)]["offset"], healthy.stat().st_size)

    def test_codex_partial_tail_does_not_suppress_later_codex_session(self) -> None:
        partial = self.root / "a-writing.jsonl"
        partial.write_text(codex("prefix").rstrip("\n"), encoding="utf-8")
        healthy = self.root / "b-live.jsonl"
        healthy.write_text(codex("fact"), encoding="utf-8")
        sessions = [
            {"head": "codex", "path": str(partial), "session_id": "a", "cwd": "/project"},
            {"head": "codex", "path": str(healthy), "session_id": "b", "cwd": "/project"},
        ]
        with mock.patch("ummanu.automations.agents.curator.discover.codex_sessions", return_value=sessions):
            self.assertEqual(cli.cmd_precheck(), 0)
            pending = harvest.read_pending(self.state)
        self.assertEqual([turn["text"] for entry in pending["batch"]["sessions"] for turn in entry["turns"]], ["fact"])
        self.assertEqual(pending["batch"]["partial_sources"][0]["path"], str(partial))

    def _hold_settlement_lock(self):
        self.state.ensure_dir()
        path = self.state.dir / "cursor-settlement.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, pathlib, sys\n"
                    "with pathlib.Path(sys.argv[1]).open('a+') as handle:\n"
                    "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
                    "    print('locked', flush=True)\n"
                    "    sys.stdin.read()\n"
                ),
                str(path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def test_precheck_defers_on_another_process_settlement_without_state_mutation(self) -> None:
        holder = self._hold_settlement_lock()
        try:
            self.assertEqual(cli.cmd_precheck(), PRECHECK_DEFERRED)
            self.assertEqual(self.state.load_watermark(), {})
            self.assertFalse(self.state.pending_file.exists())
            self.assertFalse((self.state.dir / "runs.jsonl").exists())
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
            holder.stdout.close()

    def test_killed_settlement_holder_does_not_leave_a_stale_owner(self) -> None:
        holder = self._hold_settlement_lock()
        holder.kill()
        holder.wait(timeout=10)
        holder.stdin.close()
        holder.stdout.close()
        with cli.cursor_settlement_transaction(nonblocking=True):
            pass

    def test_all_curator_settlement_paths_use_the_one_transaction_boundary(self) -> None:
        source = str(self.root / "source.jsonl")
        identity = harvest.current_identity()
        batch = {
            "sessions": [
                {
                    "head": "claude",
                    "path": source,
                    "session_id": "c",
                    "cwd": "/project",
                    "turns": [{"role": "user", "text": "fact", "ts": None}],
                }
            ],
            "memory": [],
            "pending": {source: {"offset": 1}},
            "rejected": [],
        }
        with (
            mock.patch("ummanu.automations.agents.curator.discover.claude_sessions", return_value=[]),
            mock.patch.object(self.state, "lock", side_effect=AssertionError("run lock must not be used")),
            mock.patch.object(
                cli, "cursor_settlement_transaction", wraps=cli.cursor_settlement_transaction
            ) as transaction,
        ):
            self.assertEqual(cli.cmd_precheck(), PRECHECK_SKIP)
            self.assertEqual(cli.cmd_harvest(False), 0)
            self.state.ensure_dir()
            self.state.pending_file.write_text(
                json.dumps(harvest.pending_record(batch, identity, {source: None})), encoding="utf-8"
            )
            self.assertEqual(cli.cmd_advance(), 0)
        self.assertEqual(transaction.call_count, 3)
