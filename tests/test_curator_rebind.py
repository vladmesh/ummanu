"""Curator rebind after a known role-workspace move, and the busy-without-advance doctor finding."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from ummanu.automations.agents.curator import busy, cli, harvest, rebind
from ummanu.runtime.state import PRECHECK_SKIP, AgentState
from ummanu.transition import rewrite


def claude(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


class CuratorRebindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.projects = self.home / ".claude" / "projects"
        self.state = AgentState("curator", root / "state")
        moves = rewrite.claude_move_paths(self.home, rebind.MOVE_SPRINT)
        workspaces = self.home / "orca" / "workspaces"
        [(self.old_ws, self.new_ws)] = [(old, new) for old, new in moves if old.parent.parent == workspaces
                                        and old.name == "curator"]
        (po_old, po_new), (product_old, product_new) = moves[0], moves[3]
        self.po_old = self.projects / rewrite.claude_key(po_old)
        self.po_new = self.projects / rewrite.claude_key(po_new)
        self.product_old = self.projects / rewrite.claude_key(product_old)
        self.product_new = self.projects / rewrite.claude_key(product_new)
        self.ws_old_dir = self.projects / rewrite.claude_key(self.old_ws)
        self.ws_new_dir = self.projects / rewrite.claude_key(self.new_ws)
        self.outside = self.projects / "-home-dev-orca-workspaces-relay-relay-1200-card"
        self.new_ws.mkdir(parents=True)
        self.patches = [
            mock.patch.object(cli, "STATE", self.state),
            mock.patch.object(cli.discover, "CLAUDE_PROJECTS", self.projects),
            mock.patch.object(cli.discover, "codex_sessions", return_value=[]),
            mock.patch.object(cli.discover, "hermes_sessions", return_value=[]),
            mock.patch.object(cli.discover, "all_memory_files", return_value=[]),
            mock.patch.dict(os.environ, {"HOME": str(self.home), "TA_CURATOR_WORKSPACE": str(self.new_ws)}),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def _write(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def _cursor(path: Path, offset: int | None = None) -> dict:
        stat = path.stat()
        return {"offset": stat.st_size if offset is None else offset, "mtime": stat.st_mtime, "size": stat.st_size}

    def _fixture(self, *, workspace: Path | None = None) -> dict:
        """Old and new Claude project dirs, a watermark mixing every case, a pending record on the old workspace."""
        moved = self._write(self.po_new / "a.jsonl", claude("a1") + claude("a2"))
        ahead = self._write(self.po_new / "d.jsonl", claude("d1") + claude("d2"))
        behind = self._write(self.po_new / "e.jsonl", claude("e1"))
        kept_old = self._write(self.product_old / "b.jsonl", claude("b1"))
        self._write(self.product_new / "b.jsonl", claude("b1"))
        outside = self._write(self.outside / "c.jsonl", claude("c1"))
        pending_moved = self._write(self.ws_new_dir / "g.jsonl", claude("g1"))
        pending_outside = self._write(self.outside / "h.jsonl", claude("h1"))
        old = {path: str(self.po_old / path.name) for path in (moved, ahead, behind)}
        mark = {
            old[moved]: self._cursor(moved),
            str(kept_old): self._cursor(kept_old),
            str(outside): self._cursor(outside),
            old[ahead]: self._cursor(ahead, len(claude("d1"))),
            str(ahead): self._cursor(ahead),
            old[behind]: self._cursor(behind),
            str(behind): self._cursor(behind, 0),
            str(self.po_old / "f.jsonl"): {"offset": 7, "mtime": 1.0, "size": 7},
            "hermes:h1": {"last_id": 4},
        }
        self.state.ensure_dir()
        # Persistent by design: every curator settlement has created it on a live installation.
        (self.state.dir / "cursor-settlement.lock").touch()
        self.state.watermark_file.write_text(json.dumps(mark, indent=2, ensure_ascii=False), encoding="utf-8")
        old_g = str(self.ws_old_dir / "g.jsonl")
        batch = {
            "sessions": [
                {"head": "claude", "path": old_g, "session_id": "g", "cwd": str(self.old_ws), "route": "ummanu",
                 "turns": [{"role": "user", "text": "g1", "ts": None}]},
                {"head": "claude", "path": str(pending_outside), "session_id": "h", "cwd": "/elsewhere",
                 "route": "ummanu", "turns": [{"role": "user", "text": "h1", "ts": None}]},
            ],
            "memory": [],
            "pending": {old_g: self._cursor(pending_moved), str(pending_outside): self._cursor(pending_outside)},
            "rejected": [],
            "partial_sources": [],
            "project": "all",
        }
        identity = {"workspace": str(workspace or self.old_ws)}
        record = harvest.pending_record(batch, identity, {old_g: None, str(pending_outside): None})
        self.state.pending_file.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return {"mark": mark, "batch": batch, "old": old, "old_g": old_g, "files": {
            "moved": moved, "ahead": ahead, "behind": behind, "kept_old": kept_old, "outside": outside,
            "pending_moved": pending_moved, "pending_outside": pending_outside,
        }}

    def _snapshot(self, *, journal: bool = True) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in sorted(self.state.dir.iterdir())
            if path.is_file() and (journal or path.name != "runs.jsonl")
        }

    def _published_digest(self) -> str:
        return rebind.state_digest(self.state.pending_file.read_bytes(), self.state.watermark_file.read_bytes())

    def _journal(self) -> list[dict]:
        return [json.loads(line) for line in (self.state.dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()]

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["rebind", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_prints_the_plan_and_writes_nothing(self) -> None:
        fixture = self._fixture()
        before = self._snapshot()
        code, out, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self._snapshot(), before)
        self.assertIn(f"identity: rebind {self.old_ws} -> {self.new_ws}", out)
        self.assertIn(f"carry {fixture['old'][fixture['files']['moved']]} -> {fixture['files']['moved']}", out)
        self.assertIn("plan (dry run, nothing written); rebound=1 carried=2 superseded=1 pending_keys=1", out)
        self.assertIn("skipped: outside_moves=4, old_exists=1, new_missing=1, incomparable=0", out)

    def test_rebind_rewrites_pending_and_watermark_byte_for_byte_and_is_idempotent(self) -> None:
        fixture = self._fixture()
        files, old = fixture["files"], fixture["old"]
        code, out, err = self._run()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("curator rebind: rebound; rebound=1 carried=2 superseded=1 pending_keys=1", out)

        mark = fixture["mark"]
        expected_mark = {
            str(files["moved"]): mark[old[files["moved"]]],
            str(files["kept_old"]): mark[str(files["kept_old"])],
            str(files["outside"]): mark[str(files["outside"])],
            str(files["ahead"]): mark[str(files["ahead"])],
            str(files["behind"]): mark[old[files["behind"]]],
            str(self.po_old / "f.jsonl"): {"offset": 7, "mtime": 1.0, "size": 7},
            "hermes:h1": {"last_id": 4},
        }
        self.assertEqual(
            self.state.watermark_file.read_bytes(),
            json.dumps(expected_mark, indent=2, ensure_ascii=False).encode(),
        )
        batch = fixture["batch"]
        new_g = str(files["pending_moved"])
        expected_batch = {**batch, "pending": {
            new_g: batch["pending"][fixture["old_g"]],
            str(files["pending_outside"]): batch["pending"][str(files["pending_outside"])],
        }}
        expected_record = harvest.pending_record(
            expected_batch, {"workspace": str(self.new_ws)}, {new_g: None, str(files["pending_outside"]): None}
        )
        self.assertEqual(self.state.pending_file.read_bytes(), json.dumps(expected_record, ensure_ascii=False).encode())
        self.assertEqual(expected_record["batch"]["sessions"], batch["sessions"])
        harvest.read_pending(self.state, harvest.current_identity())

        audit = [json.loads(line) for line in (self.state.dir / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(len(audit), 1)
        self.assertEqual(
            {key: value for key, value in audit[0].items() if key != "ts"},
            {"event": "rebind", "rebound": 1, "carried": 2, "superseded": 1, "pending_keys": 1,
             "skipped": {"outside_moves": 4, "old_exists": 1, "new_missing": 1, "incomparable": 0},
             "from_workspace": str(self.old_ws), "to_workspace": str(self.new_ws),
             "state_digest": self._published_digest()},
        )

        after = self._snapshot()
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("nothing to rebind; rebound=0 carried=0 superseded=0 pending_keys=0", out)
        self.assertEqual(self._snapshot(), after)

    def test_rebind_runs_under_the_cursor_settlement_lock_and_dry_run_takes_none(self) -> None:
        self._fixture()
        with mock.patch.object(
            cli, "cursor_settlement_transaction", wraps=cli.cursor_settlement_transaction
        ) as transaction:
            self.assertEqual(self._run("--dry-run")[0], 0)
            self.assertEqual(transaction.call_count, 0)
            self.assertEqual(self._run()[0], 0)
            self.assertEqual(transaction.call_count, 1)

    def test_a_failed_audit_append_changes_nothing_and_exits_non_zero(self) -> None:
        self._fixture()
        before = self._snapshot()
        with mock.patch("ummanu.runtime.state.os.write", side_effect=OSError("No space left on device")):
            code, out, err = self._run()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("curator: rebind failed: No space left on device; state unchanged", err)
        self.assertEqual(self._snapshot(journal=False), {k: v for k, v in before.items() if k != "runs.jsonl"})
        journal = self.state.dir / "runs.jsonl"
        self.assertEqual(journal.read_bytes() if journal.exists() else b"", b"")  # O_CREAT may leave it empty

    def test_a_failed_publication_is_rolled_back_audited_and_a_retry_applies(self) -> None:
        self._fixture()
        before = self._snapshot(journal=False)
        real_replace = os.replace
        faults = [self.state.pending_file]

        def replace(source, target, *args, **kwargs):
            # Once, after watermark.json was already replaced; the rollback's own replaces succeed.
            if faults and Path(target) == faults[0]:
                faults.pop()
                raise OSError("pending unavailable")
            return real_replace(source, target, *args, **kwargs)

        with mock.patch("ummanu.runtime.state.os.replace", side_effect=replace):
            code, out, err = self._run()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("curator: rebind failed: pending unavailable; state unchanged", err)
        self.assertEqual(self._snapshot(journal=False), before)
        failed = self._journal()
        self.assertEqual([event["event"] for event in failed], ["rebind", "rebind-failed"])
        self.assertEqual(failed[1]["state_digest"], failed[0]["state_digest"])
        self.assertEqual(failed[1]["error"], "pending unavailable")

        code, out, err = self._run()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("curator rebind: rebound; rebound=1 carried=2", out)
        journal = self._journal()
        self.assertEqual([event["event"] for event in journal], ["rebind", "rebind-failed", "rebind"])
        self.assertEqual(journal[2]["state_digest"], journal[0]["state_digest"])
        self.assertEqual(journal[2]["state_digest"], self._published_digest())
        harvest.read_pending(self.state, harvest.current_identity())

    def test_concurrent_appends_survive_the_rebind(self) -> None:
        self._fixture()
        journal = self.state.dir / "runs.jsonl"
        journal.write_text('{"event": "precheck", "result": "change"}\n', encoding="utf-8")
        real_publish = cli.publish_state_atomic

        def publish(writes, **kwargs):
            # Another writer (a precheck, the dispatcher's busy skip) appends between audit and publish.
            cli.STATE.log_run("dispatch", action="supervised-busy-skip")
            self.assertNotIn(journal, [path for path, _ in writes])
            return real_publish(writes, **kwargs)

        with mock.patch.object(cli, "publish_state_atomic", side_effect=publish):
            self.assertEqual(self._run()[0], 0)
        cli.STATE.log_run("precheck", result="no-change")
        self.assertEqual(
            [(event["event"], event.get("action") or event.get("result")) for event in self._journal()],
            [("precheck", "change"), ("rebind", None), ("dispatch", "supervised-busy-skip"), ("precheck", "no-change")],
        )
        self.assertEqual(self._journal()[1]["state_digest"], self._published_digest())

    def test_rebind_only_appends_to_the_journal(self) -> None:
        self._fixture()
        journal = self.state.dir / "runs.jsonl"
        journal.write_text('{"event": "precheck", "result": "change"}\n', encoding="utf-8")
        before, inode = journal.read_bytes(), journal.stat().st_ino
        real_open, real_replace = os.open, os.replace
        opened: list[int] = []
        replaced: list[Path] = []

        def spy_open(path, flags, *args, **kwargs):
            if Path(path) == journal:
                opened.append(flags)
            return real_open(path, flags, *args, **kwargs)

        def spy_replace(source, target, *args, **kwargs):
            replaced.append(Path(target))
            return real_replace(source, target, *args, **kwargs)

        with (
            mock.patch("ummanu.runtime.state.os.open", side_effect=spy_open),
            mock.patch("ummanu.runtime.state.os.replace", side_effect=spy_replace),
        ):
            self.assertEqual(self._run()[0], 0)
        after = journal.read_bytes()
        # The old bytes are an unchanged prefix of the same file, which gained exactly one line.
        self.assertTrue(after.startswith(before))
        self.assertEqual(journal.stat().st_ino, inode)
        self.assertEqual([event["event"] for event in self._journal()], ["precheck", "rebind"])
        self.assertTrue(opened)
        for flags in opened:
            self.assertTrue(flags & os.O_APPEND)
            self.assertFalse(flags & os.O_TRUNC)
        self.assertNotIn(journal, replaced)
        # A second run has nothing to rebind and writes nothing, the journal included.
        self.assertEqual(self._run()[0], 0)
        self.assertEqual(journal.read_bytes(), after)

    def test_a_carried_cursor_never_moves_backwards(self) -> None:
        fixture = self._fixture()
        files = fixture["files"]
        self.assertEqual(self._run()[0], 0)
        mark = self.state.load_watermark()
        self.assertEqual(mark[str(files["ahead"])]["offset"], files["ahead"].stat().st_size)
        self.assertEqual(mark[str(files["behind"])]["offset"], files["behind"].stat().st_size)
        self.assertNotIn(fixture["old"][files["ahead"]], mark)

    def test_refuses_an_identity_outside_the_known_moves(self) -> None:
        self._fixture(workspace=self.home / "orca" / "workspaces" / "elsewhere" / "curator")
        before = self._snapshot()
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("rebind refused (identity-outside-moves)", err)
        self.assertEqual(self._snapshot(), before)

    def test_refuses_when_the_old_workspace_still_exists(self) -> None:
        self._fixture()
        self.old_ws.mkdir(parents=True)
        before = self._snapshot()
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("rebind refused (old-workspace-exists)", err)
        self.assertEqual(self._snapshot(), before)

    def test_refuses_a_rebound_identity_that_is_not_this_run(self) -> None:
        self._fixture()
        before = self._snapshot()
        with mock.patch.dict(os.environ, {"TA_CURATOR_WORKSPACE": str(self.home / "somewhere")}):
            code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("rebind refused (identity-not-current)", err)
        self.assertEqual(self._snapshot(), before)

    def test_refuses_a_pending_start_that_disagrees_with_the_carried_cursor(self) -> None:
        fixture = self._fixture()
        mark = fixture["mark"]
        mark[str(fixture["files"]["pending_moved"])] = {"offset": 1, "mtime": 1.0, "size": 1}
        self.state.watermark_file.write_text(json.dumps(mark, indent=2), encoding="utf-8")
        before = self._snapshot()
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("rebind refused (pending-base-diverged)", err)
        self.assertEqual(self._snapshot(), before)

    def test_the_identity_refusal_names_the_verb_and_passes_after_the_rebind(self) -> None:
        self._fixture()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cli.cmd_precheck(), 1)
        self.assertIn("belongs to a different run identity", err.getvalue())
        self.assertIn("ummanu automations curator rebind", err.getvalue())
        self.assertEqual(self._run()[0], 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_precheck(), 0)

    def test_a_carried_transcript_is_not_harvested_again(self) -> None:
        fixture = self._fixture()
        files = fixture["files"]
        sessions = [
            {"head": "claude", "path": str(files[name]), "session_id": name, "cwd": "/project"}
            for name in ("moved", "ahead", "behind", "pending_moved", "pending_outside")
        ]
        self.assertEqual(self._run()[0], 0)
        with mock.patch.object(cli.discover, "claude_sessions", return_value=sessions), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_advance(), 0)
            self.assertEqual(cli.cmd_precheck(), PRECHECK_SKIP)
            self.assertIn("no new turns since watermark", err.getvalue())
            self.assertFalse(self.state.pending_file.exists())
            with files["moved"].open("a", encoding="utf-8") as handle:
                handle.write(claude("a3"))
            self.assertEqual(cli.cmd_precheck(), 0)
        pending = harvest.read_pending(self.state)
        self.assertEqual(
            [(entry["path"], turn["text"]) for entry in pending["batch"]["sessions"] for turn in entry["turns"]],
            [(str(files["moved"]), "a3")],
        )


class BusyWithoutAdvanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.runs = self.data / "automation-state" / "curator" / "runs.jsonl"
        self.runs.parent.mkdir(parents=True)
        self.now = datetime.now(UTC)

    def _log(self, *events: tuple[timedelta, dict]) -> None:
        self.runs.write_text(
            "".join(json.dumps({"ts": (self.now - ago).isoformat(), **event}) + "\n" for ago, event in events),
            encoding="utf-8",
        )

    def _skips(self, days: int) -> list[tuple[timedelta, dict]]:
        return [(timedelta(hours=hours), {"event": "dispatch", "action": "supervised-busy-skip"})
                for hours in range(days * 24, 0, -1)]

    def test_an_eight_day_busy_skip_streak_is_a_doctor_finding(self) -> None:
        from ummanu import cli as product_cli

        self._log(
            (timedelta(days=8, hours=2), {"event": "advance"}),
            (timedelta(days=8, hours=1), {"event": "dispatch", "action": "supervised-started", "reference": None}),
            *self._skips(8),
        )
        [finding] = product_cli.automation_busy_findings(self.data)
        self.assertEqual(finding["code"], "automation_busy_without_advance")
        self.assertEqual(finding["agent"], "curator")
        self.assertEqual(finding["busy_skips"], 8 * 24)
        self.assertEqual(finding["threshold_hours"], busy.BUSY_WITHOUT_ADVANCE_HOURS)
        self.assertEqual(finding["busy_since"], (self.now - timedelta(days=8)).isoformat())

    def test_a_recent_advance_or_memory_write_gives_no_finding(self) -> None:
        from ummanu import cli as product_cli

        for progress in ({"event": "advance"}, {"event": "memory_write", "result": "ok"}):
            with self.subTest(progress=progress):
                self._log(*self._skips(8), (timedelta(minutes=30), progress))
                self.assertEqual(product_cli.automation_busy_findings(self.data), [])

    def test_a_failed_memory_write_is_not_progress(self) -> None:
        self._log(*self._skips(1), (timedelta(minutes=30), {"event": "memory_write", "result": "error"}))
        self.assertIsNotNone(busy.busy_without_advance(self.runs))

    def test_below_the_threshold_or_after_a_fresh_head_there_is_no_finding(self) -> None:
        hours = busy.BUSY_WITHOUT_ADVANCE_HOURS
        self._log(*[(timedelta(hours=h), {"event": "dispatch", "action": "supervised-busy-skip"})
                    for h in range(hours - 1, 0, -1)])
        self.assertIsNone(busy.busy_without_advance(self.runs))
        self._log(*self._skips(3), (timedelta(minutes=30), {"event": "dispatch", "action": "supervised-started"}))
        self.assertIsNone(busy.busy_without_advance(self.runs))

    def test_no_runs_file_gives_no_finding(self) -> None:
        from ummanu import cli as product_cli

        self.assertEqual(product_cli.automation_busy_findings(self.data / "missing"), [])
        self.assertEqual(product_cli.automation_busy_findings(None), [])


if __name__ == "__main__":
    unittest.main()
