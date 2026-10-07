import contextlib
import json
import random
import shutil
import string
import subprocess
import tempfile
import unittest
import zlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import split_board
from tests.fakes.tasks import writer_seed
from tests.retired_board import LEGACY_VALUES, legacy_runtime_lines, write_stale_leftovers
from tests.sql_backend_fixtures import card_store
from ummanu import secret_store
from ummanu._fsutil import publish_component_entries
from ummanu.board import (
    Actor,
    EntityKind,
    Event,
    EventKind,
)
from ummanu.board.checkpoint_layout import CheckpointBoard, CheckpointLayoutError, open_checkpoint_board
from ummanu.checkpoint import (
    ANALYTICS_MANIFEST,
    PUSH_INTERVAL_SECONDS,
    AnalyticsManifestError,
    CheckpointPusher,
    CheckpointResult,
    CheckpointWriter,
    PushOutcome,
    _analytics_checkpoint_id,
    _validate_board,
    _validate_board_events,
    _write_analytics_manifest,
    checkpoint_snapshot,
    render_checkpoint_lines,
    verify_analytics_checkpoint,
)
from ummanu.data import DataExport, export_board
from ummanu.infra.checkpoint_run import _coordinate_checkpoint
from ummanu.routing_journal import attempts
from ummanu.secret_store import import_env_file, initialize_store, set_secret
from ummanu.secret_words import RECOVERY_WORDS
from ummanu.tasks import TaskReader, task_audit_for


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
    return result.stdout


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


CARD = {
    "id": 1,
    "reference": "ummanu-637",
    "title": "Checkpoint writer",
    "column": "Ready",
    "comments": [],
}


SPRINT = {
    "reference": "sprint:41",
    "goal": "Ship sprint entities into the checkpoint",
    "definition_of_done": "restore rebuilds the entity",
    "repositories": ["ummanu"],
    "status": "closed",
    "budget": {"by_type": {"red_ci": 1}},
    "current_task": "ummanu-637",
    "resume": None,
    "audit": {
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z",
        "board": "Ummanu sprints",
    },
    "comments": [],
}


class CheckpointWriterTests(unittest.TestCase):
    _client = None

    @property
    def client(self):
        """This installation's card store, made the first time a case needs it."""
        if self._client is None:
            self._client = card_store(self, writer_seed(), instance_dir=self.instance_dir)
        return self._client

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.data_dir = root / "ummanu-data"
        self.instance_dir = root / "secretary-instance"
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "runs").mkdir(parents=True)
        self.instance_dir.mkdir()
        git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(self.instance_dir, "config", "user.name", "operator")
        git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance_dir, "add", "instance.yaml")
        git(self.instance_dir, "commit", "--quiet", "-m", "config")
        self.seed_board([CARD])
        self.seed_runs([])

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def seed_board(
        self,
        cards: list[dict],
        *,
        card_count: int | None = None,
        sprints: list[dict] | None = None,
        sprint_count: int | None = None,
    ) -> None:
        board = self.data_dir / "board"
        board.mkdir(parents=True, exist_ok=True)
        sprints = sprints if sprints is not None else []
        body = "".join(json.dumps(card, sort_keys=True) + "\n" for card in cards)
        (board / "cards.ndjson").write_text(body, encoding="utf-8")
        (board / "sprints.ndjson").write_text(
            "".join(json.dumps(sprint, sort_keys=True) + "\n" for sprint in sprints),
            encoding="utf-8",
        )
        (board / "events.ndjson").write_text("", encoding="utf-8")
        (board / "audit.ndjson").write_text("", encoding="utf-8")
        (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
        (board / "sprints.json").write_text(json.dumps({"sprints": sprints}), encoding="utf-8")
        (board / "export.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "card_count": card_count if card_count is not None else len(cards),
                    "sprint_count": sprint_count if sprint_count is not None else len(sprints),
                }
            ),
            encoding="utf-8",
        )

    def seed_runs(self, records: list[dict], *, run_record_count: int | None = None) -> None:
        runs = self.data_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        (runs / "runs.ndjson").write_text(body, encoding="utf-8")
        (runs / "watermarks.json").write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
        (runs / "claims.json").write_text(json.dumps({"version": 1, "claims": {}}), encoding="utf-8")
        (runs / "export.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_record_count": (run_record_count if run_record_count is not None else len(records)),
                    "watermark_count": 0,
                    "claim_count": 0,
                }
            ),
            encoding="utf-8",
        )

    def writer(self, client: object | None = None) -> CheckpointWriter:
        """The writer, over this installation's card client.

        The gate the writer opens with is the card audit of that client (`requests`). Given none,
        it is this installation's own store.
        """
        return CheckpointWriter(
            self.data_dir, self.instance_dir, client=client if client is not None else self.client
        )

    def write(self, client: object | None = None, *, export: bool = False):
        """Run the writer with the export step stubbed by the seeded snapshot, or with the real one."""

        def board_export(data_dir, **_kwargs):
            if export:
                return export_board(
                    Path(data_dir),
                    instance_dir=self.instance_dir,
                    reader=TaskReader(self.client),
                    sprint_client=self.client,
                )
            lines = (Path(data_dir) / "board" / "cards.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        def runs_export(data_dir, **_kwargs):
            lines = (Path(data_dir) / "runs" / "runs.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        with (
            mock.patch("ummanu.checkpoint.export_board", side_effect=board_export),
            mock.patch("ummanu.checkpoint.export_runs", side_effect=runs_export),
        ):
            return self.writer(client).write()

    def head_files(self) -> list[str]:
        return git(self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD").split()

    def committed_board(self) -> CheckpointBoard | None:
        """The board HEAD carries, read through the checkpoint reader; None when it carries none."""
        if not any(name.startswith("state/board/") for name in self.head_files()):
            return None
        extracted = Path(tempfile.mkdtemp(dir=self.tmpdir.name))
        archive = subprocess.run(
            ["git", "-C", str(self.instance_dir), "archive", "HEAD", "state/board"],
            capture_output=True,
            check=True,
        )
        subprocess.run(["tar", "-x", "-C", str(extracted)], input=archive.stdout, check=True)
        return open_checkpoint_board(extracted / "state" / "board")

    def committed_text(self, name: str) -> str:
        board = self.committed_board()
        assert board is not None, "HEAD carries no board checkpoint"
        return board.read_text(name)

    def published_text(self, name: str) -> str:
        return open_checkpoint_board(self.instance_dir / "state" / "board").read_text(name)

    def test_board_and_runs_land_in_state_as_one_commit(self):
        result = self.write()

        self.assertEqual(result.status, "committed")
        self.assertEqual(result.board_cards, 1)
        files = self.head_files()
        board = self.committed_board()
        assert board is not None
        for name in ("cards.ndjson", "events.ndjson", "audit.ndjson", "export.json"):
            self.assertTrue(board.has(name), name)
        self.assertIn("state/board/analytics-manifest.json", files)
        verify_analytics_checkpoint(board.directory)
        self.assertIn("state/runs/runs.ndjson", files)
        self.assertIn("state/runs/claims.json", files)
        self.assertIn("state/runs/watermarks.json", files)
        self.assertIn("state/runs/export.json", files)
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), result.commit)

    def test_every_outcome_of_a_run_reports_how_long_the_run_took(self):
        """The cost of a checkpoint, for each outcome the writer produces (secretary-1649).

        A checkpoint run is the most expensive thing the dispatcher minute does — it regenerates
        the whole board and run projection and commits it — and the only evidence it left was
        whether it succeeded. A no-change run has to carry its own number too: it did all the
        regeneration work and found nothing to commit, so it costs an operator the same minute.
        """
        committed = self.write()
        self.assertEqual(committed.status, "committed")
        self.assertGreater(committed.duration_ms, 0.0)
        self.assertEqual(committed.to_json()["duration_ms"], committed.duration_ms)

        unchanged = self.write()
        self.assertEqual(unchanged.status, "unchanged")
        self.assertGreater(unchanged.duration_ms, 0.0)

        # The gate refuses this one before anything is staged, and a refusal is a run too.
        self.seed_board([CARD, dict(CARD, id=2, title="collision")])
        blocked = self.write()
        self.assertEqual(blocked.status, "blocked")
        self.assertGreater(blocked.duration_ms, 0.0)

    def staging_dirs(self) -> list[str]:
        """Checkpoint staging and publish backups left in the state repo's `state/` directory."""
        state = self.instance_dir / "state"
        if not state.exists():
            return []
        return sorted(path.name for path in state.iterdir() if path.name.endswith(".tmp"))

    def watch_staging(self) -> tuple[list[Path], contextlib.AbstractContextManager]:
        """Record every staging directory the writer creates, so a test can prove one existed."""
        created: list[Path] = []
        real = tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            path = real(*args, **kwargs)
            if "-checkpoint-" in str(kwargs.get("prefix", "")):
                created.append(Path(path))
            return path

        return created, mock.patch("ummanu.checkpoint.tempfile.mkdtemp", side_effect=mkdtemp)

    def test_every_outcome_of_a_run_removes_its_staging(self):
        """Staging never outlives its run: committed, unchanged and blocked (secretary-1663)."""
        created, watching = self.watch_staging()
        with watching:
            # Blocked on the second component, after the board was already published.
            self.seed_runs([{"line": 1}], run_record_count=9)
            blocked = self.write()
            self.assertEqual(blocked.status, "blocked")
            self.assertIn("runs export count mismatch", blocked.reason)
            self.assertEqual(self.staging_dirs(), [])
            # Blocked on the board: the gate refuses the staged copy.
            self.seed_runs([])
            self.seed_board([CARD], card_count=4)
            blocked = self.write()
            self.assertEqual(blocked.status, "blocked")
            self.assertIn("board export count mismatch", blocked.reason)
            self.assertEqual(self.staging_dirs(), [])
            self.seed_board([CARD])
            self.assertEqual(self.write().status, "committed")
            self.assertEqual(self.staging_dirs(), [])
            self.assertEqual(self.write().status, "unchanged")
            self.assertEqual(self.staging_dirs(), [])

        self.assertGreaterEqual(len(created), 6)
        self.assertEqual([path for path in created if path.exists()], [])

    def test_an_exception_mid_publish_still_removes_its_staging(self):
        """The leak production showed: an error no gate names left 132 MB of staging behind."""
        self.assertEqual(self.write().status, "committed")
        self.seed_board([dict(CARD, title="new cut")])
        created, watching = self.watch_staging()

        with (
            watching,
            mock.patch("ummanu.checkpoint.publish_split_board", side_effect=OSError("disk went away")),
            self.assertRaisesRegex(OSError, "disk went away"),
        ):
            self.write()

        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())
        self.assertEqual(self.staging_dirs(), [])

    def test_abandoned_staging_is_collected_at_start_under_the_lock(self):
        """Only one writer holds the lock, so staging found then belongs to no live run."""
        state = self.instance_dir / "state"
        abandoned = [state / ".board-checkpoint-3xno64tc.tmp", state / ".runs-checkpoint-abc123.tmp"]
        for path in abandoned:
            (path / "nested").mkdir(parents=True)
            (path / "nested" / "cards.ndjson").write_text("{}\n", encoding="utf-8")
        survivors = [
            state / ".board-checkpoint-3xno64tc.tmpx",
            state / "board-checkpoint-3xno64tc.tmp",
            state / ".board-old-3xno64tc.tmp",
            state / ".other-checkpoint-3xno64tc.tmp",
            state / ".board-checkpoint-3xno64tc",
        ]
        for path in survivors:
            path.mkdir()
        planted_file = state / ".runs-checkpoint-file.tmp"
        planted_file.write_text("not a directory\n", encoding="utf-8")
        elsewhere = self.instance_dir / ".board-checkpoint-elsewhere.tmp"
        elsewhere.mkdir()

        from ummanu import checkpoint as checkpoint_module

        held = {"lock": False}
        real_lock = checkpoint_module.state_repo.state_repo_lock
        real_cleanup = checkpoint_module._cleanup_staging_dir
        removals: list[tuple[str, bool]] = []

        @contextlib.contextmanager
        def lock(instance_dir):
            with real_lock(instance_dir):
                # Nothing was collected before the lock was taken.
                self.assertTrue(all(path.exists() for path in abandoned))
                held["lock"] = True
                try:
                    yield
                finally:
                    held["lock"] = False

        def cleanup(path):
            removals.append((Path(path).name, held["lock"]))
            return real_cleanup(path)

        # A run the audit gate blocks before staging anything still collects.
        task_audit_for(self.client).stage("request-1", {"event_id": "e1", "request_id": "request-1"})
        with (
            mock.patch("ummanu.checkpoint.state_repo.state_repo_lock", side_effect=lock),
            mock.patch("ummanu.checkpoint._cleanup_staging_dir", side_effect=cleanup),
        ):
            result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(sorted(removals), sorted((path.name, True) for path in abandoned))
        self.assertTrue(all(not path.exists() for path in abandoned))
        self.assertTrue(all(path.is_dir() for path in survivors))
        self.assertEqual(planted_file.read_text(encoding="utf-8"), "not a directory\n")
        self.assertTrue(elsewhere.is_dir())

    def test_duplicate_fresh_export_leaves_checkpoint_head_index_and_canon_untouched(self):
        self.assertEqual(self.write().status, "committed")
        head = git(self.instance_dir, "rev-parse", "HEAD").strip()
        canon = self.published_text("cards.ndjson")
        self.seed_board([CARD, dict(CARD, id=2, title="collision")])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("duplicate references ummanu-637", result.reason)
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), head)
        self.assertEqual(git(self.instance_dir, "diff", "--cached", "--name-only"), "")
        self.assertEqual(self.published_text("cards.ndjson"), canon)

    def test_missing_live_events_publish_as_an_empty_sealed_journal(self):
        self.write()
        self.assertEqual(self.committed_text("events.ndjson"), "")

        (self.data_dir / "board" / "events.ndjson").unlink()
        result = self.write()

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(self.committed_text("events.ndjson"), "")
        self.assertEqual(self.published_text("events.ndjson"), "")
        self.assertEqual(self.committed_text("cards.ndjson"), json.dumps(CARD, sort_keys=True) + "\n")

    def test_missing_live_audit_history_blocks_the_checkpoint(self):
        (self.data_dir / "board" / "audit.ndjson").unlink()

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("checkpoint board export is missing audit.ndjson", result.reason)
        self.assertNotIn("state/board/analytics-manifest.json", self.head_files())

    def test_board_publication_exposes_no_seal_during_its_copy_window(self):
        self.assertEqual(self.write().status, "committed")
        changed = dict(CARD, title="new cut")
        self.seed_board([changed])

        from ummanu import checkpoint as checkpoint_module

        publish = checkpoint_module.publish_split_board
        observed: list[str] = []

        def copy_with_window(staging, destination):
            # The seal left before the first part is rewritten, so this window verifies nothing.
            self.assertFalse((destination / ANALYTICS_MANIFEST).exists())
            with self.assertRaisesRegex(AnalyticsManifestError, "manifest is missing"):
                verify_analytics_checkpoint(destination)
            observed.append("no manifest")
            return publish(staging, destination)

        with mock.patch("ummanu.checkpoint.publish_split_board", side_effect=copy_with_window):
            self.assertEqual(self.write().status, "committed")

        self.assertEqual(observed, ["no manifest"])
        verify_analytics_checkpoint(self.instance_dir / "state" / "board")

    def test_a_stored_typed_event_is_staged_as_a_board_checkpoint_artifact(self):
        """The pre-2026-09-10 file journal is stored history: nothing writes it, the cut copies it."""
        event = Event(
            "evt-stored",
            EventKind.ENTITY_CREATED,
            EntityKind.CARD,
            "ummanu-1419",
            Actor("po", "operator"),
            "accepted into the sprint",
            datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
        record = event.to_record("checkpoint-event-1")
        (self.data_dir / "board" / "events.ndjson").write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )

        result = self.write()

        self.assertEqual(result.status, "committed")
        committed = self.committed_text("events.ndjson")
        self.assertEqual(json.loads(committed)["event_id"], event.event_id)
        self.assertEqual(json.loads(committed)["subject"], {"kind": "card", "ref": "ummanu-1419"})

    def test_invalid_typed_event_blocks_checkpoint_but_generic_history_stays_allowed(self):
        (self.data_dir / "board" / "events.ndjson").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "record_type": "board.protocol_event",
                    "request_id": "bad",
                    "event_id": "bad",
                    "kind": "card.started",
                    "subject": {"kind": "card", "ref": "x"},
                    "actor": {"role": "worker", "id": "w"},
                    "reason": "missing occurrence",
                    "related_refs": [],
                }
            )
            + "\n"
            + json.dumps({"event_id": "released-generic", "request_id": "old", "kind": "moved"})
            + "\n",
            encoding="utf-8",
        )

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("invalid board protocol event", result.reason)

    def test_empty_live_runs_cannot_replace_non_empty_canonical_history(self):
        self.seed_runs([{"source": "runs.jsonl", "line": 1, "record": {"event": "claim"}}])
        self.assertEqual(self.write().status, "committed")
        self.seed_runs([])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("non-empty canonical run history", result.reason)
        self.assertEqual(
            len(
                (self.instance_dir / "state" / "runs" / "runs.ndjson")
                .read_text(encoding="utf-8")
                .splitlines()
            ),
            1,
        )

    def test_partial_live_history_cannot_truncate_the_canonical_prefix(self):
        records = [
            {"source": "runs.jsonl", "line": 1, "record": {"event": "claim"}},
            {"source": "runs.jsonl", "line": 2, "record": {"event": "review"}},
        ]
        self.seed_runs(records)
        self.assertEqual(self.write().status, "committed")
        self.seed_runs(records[:1])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("truncate or rewrite", result.reason)

    def test_new_earlier_sorting_source_is_a_valid_per_journal_extension(self):
        old = {"source": "z-runs.jsonl", "line": 1, "record": {"event": "claim"}}
        self.seed_runs([old])
        self.assertEqual(self.write().status, "committed")
        self.seed_runs(
            [
                {"source": "a-runs.jsonl", "line": 1, "record": {"event": "new"}},
                old,
            ]
        )

        result = self.write()

        self.assertEqual(result.status, "committed")

    def test_derived_board_dump_is_not_part_of_the_checkpoint(self):
        self.write()

        files = self.head_files()
        self.assertNotIn("state/board/cards.json", files)
        self.assertNotIn("state/board/sprints.json", files)
        self.assertIn("state/board/.gitignore", files)
        ignore = (self.instance_dir / "state" / "board" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("cards.json", ignore.splitlines())
        self.assertIn("sprints.json", ignore.splitlines())

    def test_sprint_entities_are_committed_next_to_the_cards(self):
        self.seed_board([CARD], sprints=[SPRINT])

        result = self.write()

        self.assertEqual(result.status, "committed")
        committed = self.committed_text("sprints.ndjson")
        self.assertEqual(
            [json.loads(line)["reference"] for line in committed.splitlines() if line.strip()],
            ["sprint:41"],
        )

    def test_typed_records_with_invalid_product_projects_block_checkpoint(self):
        (self.instance_dir / "projects").mkdir()
        (self.instance_dir / "projects" / "ummanu.yaml").write_text("id: ummanu\n", encoding="utf-8")
        product = {
            "reference": "product:ummanu",
            "title": "Ummanu",
            "column": "Issues",
            "closed": False,
            "metadata": {"record_type": "product", "product_id": "ummanu", "product_projects": "[]"},
        }
        self.seed_board([product])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("non-empty unique project set", result.reason)

    def test_sprint_count_mismatch_blocks_the_commit(self):
        self.seed_board([CARD], sprints=[SPRINT], sprint_count=4)

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("board sprint count mismatch", result.reason)
        self.assertIsNone(self.committed_board())

    def test_board_export_without_sprints_blocks_the_commit(self):
        self.seed_board([CARD], sprints=[SPRINT])
        (self.data_dir / "board" / "sprints.ndjson").unlink()

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("missing sprints.ndjson", result.reason)

    def test_routing_attempts_reach_the_committed_checkpoint(self):
        """secretary-716: attempt telemetry is journal-only, so a restore that replays the
        checkpoint has to hand back the worker/reviewer pair of every attempt.

        The attempt telemetry is the card audit's, so it reaches the checkpoint through the board
        export's history (`audit.ndjson`), which is what a restore replays."""
        audit = task_audit_for(self.client)
        worker = {
            "role": "worker",
            "head": "codex",
            "head_source": "role_default",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "model_source": "profile",
            "effort": "default",
            "codex_mode": "tui",
            "resource": "openai-sub",
            "account": "openai-subscription",
        }
        reviewer = {
            "role": "reviewer",
            "head": "claude-default",
            "head_source": "card",
            "adapter": "claude",
            "model": "",
            "model_source": "cli_default",
            "resource": "claude-sub",
            "account": "claude-subscription",
        }
        for phase, heads, outcome in (
            ("worker", [worker], ""),
            ("review", [reviewer], ""),
            ("verdict", [worker, reviewer], "green"),
        ):
            audit.append(
                f"routing-{phase}",
                {
                    "event_id": f"evt_{phase}",
                    "schema_version": 1,
                    "kind": "routing",
                    "occurred_at": "2026-07-24T00:00:00Z",
                    "outcome": "success",
                    "actor": {"role": "dispatcher", "id": "ummanu-dispatcher"},
                    "task_id": "task_postgres_1",
                    "ref": "ummanu-637",
                    "backend": {"kind": "postgres", "task_id": 1, "revision": "updated_at:x"},
                    "request_id": f"routing-{phase}",
                    "payload": {
                        "attempt": 1,
                        "attempt_id": "attempt-1",
                        "phase": phase,
                        "outcome": outcome,
                        "heads": heads,
                    },
                },
            )

        result = self.write(export=True)

        self.assertEqual(result.status, "committed", result.reason)
        committed = self.committed_text("audit.ndjson")
        history = attempts([json.loads(line) for line in committed.splitlines() if line.strip()])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].worker.head, "codex")
        self.assertEqual(history[0].worker.model, "gpt-5.6-terra")
        self.assertEqual(history[0].reviewer.head, "claude-default")
        self.assertEqual(history[0].reviewer.model_source, "cli_default")
        self.assertEqual(history[0].outcome, "green")

    def test_unchanged_state_skips_the_commit(self):
        first = self.write()
        second = self.write()

        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.commit, "")
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), first.commit)

    def test_changed_board_commits_again(self):
        first = self.write()
        self.seed_board([CARD, {**CARD, "id": 2, "reference": "ummanu-638"}])
        second = self.write()

        self.assertEqual(second.status, "committed")
        self.assertNotEqual(second.commit, first.commit)

    def test_pending_audit_blocks_the_commit(self):
        task_audit_for(self.client).stage("request-1", {"event_id": "e1", "request_id": "request-1"})

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("pending", result.reason)
        self.assertIsNone(self.committed_board())

    def test_count_mismatch_blocks_the_commit(self):
        self.seed_board([CARD], card_count=4)

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("board export count mismatch", result.reason)
        self.assertIsNone(self.committed_board())

    def test_run_record_mismatch_blocks_the_commit(self):
        self.seed_runs([{"line": 1}], run_record_count=9)

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("runs export count mismatch", result.reason)

    def test_secret_in_a_card_blocks_the_commit(self):
        self.seed_board([{**CARD, "description": "token ghp_" + "a" * 40}])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("secret detected in state/board/cards.ndjson", result.reason)
        self.assertIsNone(self.committed_board())

    def test_stale_transport_text_in_a_card_does_not_block_the_checkpoint(self):
        """The retired transport file is not a redaction source: its plain configuration publishes."""
        stale = write_stale_leftovers(self.instance_dir)
        contents = stale.read_text(encoding="utf-8")
        self.seed_board(
            [
                {
                    **CARD,
                    "description": f"Board configuration:\n{contents}",
                    "comments": [{"text": f"report comment:\n{contents}"}],
                }
            ]
        )
        (self.data_dir / "board" / "export.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "card_count": 1,
                    "sprint_count": 0,
                    "report": f"exported board configuration:\n{contents}",
                }
            ),
            encoding="utf-8",
        )

        result = self.write()

        self.assertEqual(result.status, "committed")
        published = self.published_text("cards.ndjson")
        self.assertIn(LEGACY_VALUES[0], published)
        exported = self.published_text("export.json")
        self.assertIn(contents, json.loads(exported)["report"])

    def test_a_stale_transport_file_with_a_broad_mode_does_not_block_the_checkpoint(self):
        """Nothing reads the leftover, so its mode is no redaction failure either."""
        write_stale_leftovers(self.instance_dir).chmod(0o644)
        self.seed_board([CARD])

        result = self.write()

        self.assertEqual(result.status, "committed")

    def test_a_legacy_runtime_token_is_still_redacted_by_the_ordinary_rule(self):
        (self.instance_dir / "runtime.env").write_text(legacy_runtime_lines(), encoding="utf-8")
        self.seed_board([{**CARD, "description": f"token {LEGACY_VALUES[2]}"}])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("secret detected", result.reason)

    def test_named_runtime_secret_in_a_card_still_blocks_the_checkpoint(self):
        secret = "opaque-token-value"
        (self.instance_dir / "runtime.env").write_text(f"EXAMPLE_API_TOKEN={secret}\n", encoding="utf-8")
        self.seed_board([{**CARD, "description": f"token {secret}"}])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("secret detected in state/board/cards.ndjson", result.reason)

    def test_custom_catalog_secret_in_a_card_still_blocks_the_checkpoint(self):
        secret = b"catalogued-but-unusually-named-value"
        with mock.patch.object(
            secret_store,
            "_new_key_params",
            return_value={
                "format": secret_store.KEY_PARAMS_FORMAT,
                "version": secret_store.KEY_PARAMS_VERSION,
                "kdf": {
                    "id": secret_store.PHRASE_KDF_ID,
                    "salt": secret_store._b64(b"0123456789abcdef"),
                    "length": secret_store.KEY_LENGTH,
                    "n": 2**10,
                    "r": 8,
                    "p": 1,
                },
            },
        ):
            initialize_store(self.instance_dir, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
        set_secret(
            self.instance_dir,
            secret_id="binary.value",
            value=b"\xff\xfe\x00\x80",
            scope="installation",
            purpose="a binary credential that cannot appear in text",
            actor="tester",
            environment="BINARY_CREDENTIAL",
        )
        set_secret(
            self.instance_dir,
            secret_id="custom.value",
            value=secret,
            scope="installation",
            purpose="a custom integration credential",
            actor="tester",
            environment="UNUSUAL_INTEGRATION_CREDENTIAL",
        )
        self.seed_board([{**CARD, "description": secret.decode("utf-8")}])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("secret detected in state/board/cards.ndjson", result.reason)

    def test_imported_runtime_config_paths_do_not_block_checkpoint(self):
        runtime = self.instance_dir / "runtime.env"
        url = "https://board.example.invalid/rpc"
        data_dir = "/srv/ummanu-data"
        product_root = "/srv/ummanu"
        runtime.write_text(
            "\n".join(
                [
                    f"UMMANU_DATA_DIR={data_dir}",
                    f"UMMANU_REPO={product_root}",
                    f"EXAMPLE_URL={url}",
                    "EXAMPLE_API_TOKEN=opaque-token-value",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            secret_store,
            "_new_key_params",
            return_value={
                "format": secret_store.KEY_PARAMS_FORMAT,
                "version": secret_store.KEY_PARAMS_VERSION,
                "kdf": {
                    "id": secret_store.PHRASE_KDF_ID,
                    "salt": secret_store._b64(b"0123456789abcdef"),
                    "length": secret_store.KEY_LENGTH,
                    "n": 2**10,
                    "r": 8,
                    "p": 1,
                },
            },
        ):
            initialize_store(self.instance_dir, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
        import_env_file(
            self.instance_dir,
            source=runtime,
            scope="installation",
            purpose="imported runtime",
            actor="tester",
        )
        self.seed_board([{**CARD, "description": f"data={data_dir} repo={product_root} board={url}"}])

        result = self.write()

        self.assertEqual(result.status, "committed")

    def test_blocked_snapshot_leaves_the_previous_checkpoint_intact(self):
        first = self.write()
        self.seed_board([{**CARD, "description": "token ghp_" + "a" * 40}])
        blocked = self.write()

        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), first.commit)
        published = self.published_text("cards.ndjson")
        self.assertNotIn("ghp_", published)

    def test_export_failure_blocks_without_touching_state(self):
        with mock.patch(
            "ummanu.checkpoint.export_board", side_effect=RuntimeError("pipeline list failed")
        ):
            result = self.writer().write()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "pipeline list failed")
        self.assertFalse((self.instance_dir / "state").exists())

    def test_ignored_state_blocks_instead_of_reporting_unchanged(self):
        (self.instance_dir / ".gitignore").write_text("state/\n", encoding="utf-8")

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("not tracked by the instance repo", result.reason)

    def test_partially_ignored_state_blocks_instead_of_committing_half(self):
        (self.instance_dir / ".gitignore").write_text("state/runs/\n", encoding="utf-8")

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("state/runs/runs.ndjson", result.reason)
        self.assertIsNone(self.committed_board())

    def test_operator_config_changes_stay_out_of_the_checkpoint_commit(self):
        (self.instance_dir / "instance.yaml").write_text("version: 2\n", encoding="utf-8")
        git(self.instance_dir, "add", "instance.yaml")

        self.write()

        committed = git(self.instance_dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertNotIn("instance.yaml", committed)
        self.assertTrue(any(name.startswith("state/board/cards/") for name in committed), committed)


def _object_store(repo: Path) -> tuple[int, int]:
    """(objects, bytes) in a repository's object store, loose and packed, from `git count-objects -v`."""
    counts = dict(
        line.split(": ", 1) for line in git(repo, "count-objects", "-v").splitlines() if ": " in line
    )
    objects = int(counts["count"]) + int(counts["in-pack"])
    size = (int(counts["size"]) + int(counts["size-pack"])) * 1024
    return objects, size


class CheckpointGitCostTests(unittest.TestCase):
    """secretary-1656: a tick's Git cost follows what changed, not the board's size."""

    CARDS = 2000

    # The writer fixture, borrowed without rerunning the writer cases.
    setUp = CheckpointWriterTests.setUp
    tearDown = CheckpointWriterTests.tearDown
    seed_board = CheckpointWriterTests.seed_board
    seed_runs = CheckpointWriterTests.seed_runs
    writer = CheckpointWriterTests.writer
    client = CheckpointWriterTests.client
    _client = None
    write = CheckpointWriterTests.write
    head_files = CheckpointWriterTests.head_files
    committed_board = CheckpointWriterTests.committed_board
    committed_text = CheckpointWriterTests.committed_text

    @staticmethod
    def _prose(rng, size: int) -> str:
        words: list[str] = []
        while sum(len(word) + 1 for word in words) < size:
            words.append("".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(3, 9))))
        return " ".join(words)

    def seed_large_board(self, *, audit_lines: int) -> list[dict]:
        rng = random.Random(1656)
        cards = [
            dict(CARD, id=number, reference=f"ummanu-{number}", description=self._prose(rng, 1500))
            for number in range(1, self.CARDS + 1)
        ]
        self.seed_board(cards, sprints=[SPRINT])
        self.append_audit(
            [{"request_id": f"r-{n}", "note": self._prose(rng, 200)} for n in range(audit_lines)]
        )
        return cards

    def append_audit(self, rows: list[dict]) -> None:
        with (self.data_dir / "board" / "audit.ndjson").open("a", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))

    def test_checkpoint_over_an_unchanged_board_creates_no_git_object(self):
        self.seed_large_board(audit_lines=500)
        self.assertEqual(self.write().status, "committed")
        before = _object_store(self.instance_dir)

        result = self.write()

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(_object_store(self.instance_dir), before)

    def test_one_card_change_adds_less_than_a_megabyte_of_objects(self):
        cards = self.seed_large_board(audit_lines=5000)
        self.assertEqual(self.write().status, "committed")
        # The fixture is big enough that the old layout, which stored cards.ndjson whole, would
        # have added more than a megabyte for this change on the cards file alone.
        flat_cards = (self.data_dir / "board" / "cards.ndjson").read_bytes()
        self.assertGreater(len(zlib.compress(flat_cards)), 1024 * 1024)
        objects_before, size_before = _object_store(self.instance_dir)

        cards[1000] = dict(cards[1000], column="Done", title="one card moved")
        self.seed_board(cards, sprints=[SPRINT])  # rewrites the local export, audit included
        self.seed_large_board_audit_again()
        self.append_audit([{"request_id": f"r-new-{n}", "note": "moved"} for n in range(10)])
        result = self.write()

        self.assertEqual(result.status, "committed")
        objects_after, size_after = _object_store(self.instance_dir)
        self.assertLess(size_after - size_before, 1024 * 1024)
        # A commit, the trees on the changed paths, one card blob, one audit segment and the seal:
        # a handful of objects, not one per card.
        self.assertLess(objects_after - objects_before, 20)
        committed = self.committed_board()
        assert committed is not None
        board = self.data_dir / "board"
        self.assertEqual(committed.read_bytes("cards.ndjson"), (board / "cards.ndjson").read_bytes())
        self.assertEqual(committed.read_bytes("audit.ndjson"), (board / "audit.ndjson").read_bytes())
        verify_analytics_checkpoint(committed.directory)

    def seed_large_board_audit_again(self) -> None:
        """`seed_board` empties the local audit log; put the committed history back unchanged."""
        rng = random.Random(1656)
        for _ in range(self.CARDS):
            self._prose(rng, 1500)
        self.append_audit([{"request_id": f"r-{n}", "note": self._prose(rng, 200)} for n in range(5000)])

    def test_appended_log_adds_one_segment_and_a_rewritten_log_is_one_segment_again(self):
        self.append_audit([{"request_id": "r-1"}])
        self.assertEqual(self.write().status, "committed")
        segments = self.instance_dir / "state" / "board" / "audit" / "0000"
        self.assertEqual(sorted(path.name for path in segments.iterdir()), ["00000000.ndjson"])

        self.append_audit([{"request_id": "r-2"}])
        self.assertEqual(self.write().status, "committed")
        self.assertEqual(
            sorted(path.name for path in segments.iterdir()), ["00000000.ndjson", "00000001.ndjson"]
        )
        self.assertEqual((segments / "00000001.ndjson").read_text(), '{"request_id": "r-2"}\n')

        (self.data_dir / "board" / "audit.ndjson").write_text('{"request_id": "r-9"}\n', encoding="utf-8")
        self.assertEqual(self.write().status, "committed")
        self.assertEqual(sorted(path.name for path in segments.iterdir()), ["00000000.ndjson"])
        self.assertEqual(self.committed_text("audit.ndjson"), '{"request_id": "r-9"}\n')

    def test_a_flat_checkpoint_is_converted_to_the_split_layout(self):
        """The first checkpoint after the upgrade replaces the committed flat files."""
        flat = self.instance_dir / "state" / "board"
        flat.mkdir(parents=True)
        for name in ("cards.ndjson", "sprints.ndjson", "events.ndjson", "audit.ndjson", "export.json"):
            shutil.copy(self.data_dir / "board" / name, flat / name)
        git(self.instance_dir, "add", "state")
        git(self.instance_dir, "commit", "--quiet", "-m", "flat checkpoint")
        self.assertEqual(self.committed_board().layout, "flat")
        self.append_audit([{"request_id": "r-1"}])

        self.assertEqual(self.write().status, "committed")

        board = self.committed_board()
        assert board is not None
        self.assertEqual(board.layout, "split")
        self.assertFalse({"state/board/cards.ndjson", "state/board/audit.ndjson"} & set(self.head_files()))
        self.assertEqual(
            board.read_bytes("cards.ndjson"), (self.data_dir / "board" / "cards.ndjson").read_bytes()
        )
        self.assertEqual(board.read_text("audit.ndjson"), '{"request_id": "r-1"}\n')
        verify_analytics_checkpoint(board.directory)


class AnalyticsManifestTests(unittest.TestCase):
    """The analytics boundary verifies only a sealed board directory."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.board = Path(self.tmpdir.name) / "board"
        self.board.mkdir()
        (self.board / "cards.ndjson").write_text('{"reference":"ummanu-1"}\n', encoding="utf-8")
        (self.board / "sprints.ndjson").write_text('{"reference":"sprint:1"}\n', encoding="utf-8")
        (self.board / "events.ndjson").write_text('{"event_id":"event-1"}\n', encoding="utf-8")
        (self.board / "audit.ndjson").write_text('{"request_id":"request-1"}\n', encoding="utf-8")
        (self.board / "export.json").write_text(
            json.dumps({"version": 1, "card_count": 1, "sprint_count": 1}) + "\n",
            encoding="utf-8",
        )
        _write_analytics_manifest(self.board)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def copy_board(self) -> Path:
        copied = self.board.parent / f"copy-{len(list(self.board.parent.glob('copy-*')))}"
        shutil.copytree(self.board, copied)
        return copied

    def manifest(self, board: Path) -> dict:
        return json.loads((board / ANALYTICS_MANIFEST).read_text(encoding="utf-8"))

    def write_manifest(self, board: Path, payload: dict) -> None:
        (board / ANALYTICS_MANIFEST).write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_split_layout_carries_the_same_seal_and_tampering_is_caught(self):
        """secretary-1656: the seal covers logical bytes, so both layouts verify to one identity."""
        flat_id = verify_analytics_checkpoint(self.board).checkpoint_id
        split = self.copy_board()
        split_board(split)
        self.assertFalse((split / "cards.ndjson").exists())

        self.assertEqual(verify_analytics_checkpoint(split).checkpoint_id, flat_id)

        segment = split / "audit" / "0000" / "00000000.ndjson"
        segment.write_text('{"request_id":"forged"}\n', encoding="utf-8")
        with self.assertRaisesRegex(AnalyticsManifestError, "sha256 does not match"):
            verify_analytics_checkpoint(split)
        segment.write_text('{"request_id":"request-1"}\n', encoding="utf-8")
        (split / "cards" / "0000" / "stray.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(AnalyticsManifestError, "unexpected entry"):
            verify_analytics_checkpoint(split)

    def test_symlinked_layout_marker_is_refused_not_read_as_flat(self):
        """secretary-1656 rework: the marker is the one entry that must not be followed."""
        split = self.copy_board()
        split_board(split)
        verify_analytics_checkpoint(split)
        external = self.board.parent / "external-layout.json"
        external.write_bytes((split / "layout.json").read_bytes())
        (split / "layout.json").unlink()
        (split / "layout.json").symlink_to(external)

        with self.assertRaisesRegex(AnalyticsManifestError, "layout marker is a symlink"):
            verify_analytics_checkpoint(split)
        with self.assertRaisesRegex(CheckpointLayoutError, "layout marker is a symlink"):
            open_checkpoint_board(split)
        external.unlink()  # dangling: still refused, never the flat fallback
        with self.assertRaisesRegex(CheckpointLayoutError, "layout marker is a symlink"):
            open_checkpoint_board(split)

    def test_baseline_seal_returns_only_checkpoint_metadata(self):
        verified = verify_analytics_checkpoint(self.board)

        self.assertEqual(verified.directory, self.board.resolve())
        self.assertRegex(verified.checkpoint_id, r"^[0-9a-f]{64}$")
        manifest = self.manifest(self.board)
        self.assertEqual(manifest["schema"], "ummanu.board.analytics-checkpoint")
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            [
                "events.ndjson",
                "cards.ndjson",
                "sprints.ndjson",
                "audit.ndjson",
                "export.json",
            ],
        )
        self.assertNotIn("line_count", manifest["files"][-1])

    def test_empty_events_are_a_valid_sealed_cut(self):
        board = self.copy_board()
        (board / "events.ndjson").write_text("", encoding="utf-8")
        _write_analytics_manifest(board)

        verify_analytics_checkpoint(board)

    def test_publish_helper_removes_the_old_seal_before_copying_and_adds_the_new_one_last(self):
        destination = self.copy_board()
        staging = self.board.parent / "staging"
        shutil.copytree(self.board, staging)
        (staging / "cards.ndjson").write_text('{"reference":"ummanu-2"}\n', encoding="utf-8")
        _write_analytics_manifest(staging)

        from ummanu import _fsutil

        replace = _fsutil.os.replace
        observations: list[str] = []

        def observe_replace(source, target):
            replace(source, target)
            if not (destination / ANALYTICS_MANIFEST).exists():
                with self.assertRaisesRegex(AnalyticsManifestError, "manifest is missing"):
                    verify_analytics_checkpoint(destination)
                observations.append("no manifest")
            elif target == destination / ANALYTICS_MANIFEST:
                verify_analytics_checkpoint(destination)
                observations.append("sealed")

        with mock.patch("ummanu._fsutil.os.replace", side_effect=observe_replace):
            publish_component_entries(
                staging,
                destination,
                ["cards.ndjson", "sprints.ndjson", "events.ndjson", "export.json", ANALYTICS_MANIFEST],
                "test board",
                publish_last=ANALYTICS_MANIFEST,
            )

        self.assertIn("no manifest", observations)
        self.assertEqual(observations[-1], "sealed")

    def test_stale_export_summary_is_rejected_even_when_resealed(self):
        board = self.copy_board()
        (board / "export.json").write_text(
            json.dumps({"version": 1, "card_count": 2, "sprint_count": 1}) + "\n",
            encoding="utf-8",
        )
        _write_analytics_manifest(board)

        with self.assertRaisesRegex(AnalyticsManifestError, "stale card_count"):
            verify_analytics_checkpoint(board)

    def test_valid_event_truncation_is_rejected_before_analytics_parsing(self):
        board = self.copy_board()
        (board / "events.ndjson").write_text("", encoding="utf-8")

        # The legacy structural reader accepts this eventless directory. The
        # analytics boundary does not accept it because its old seal is stale.
        _validate_board(board, registered_project_ids=set())
        with self.assertRaisesRegex(AnalyticsManifestError, "events.ndjson: sha256"):
            verify_analytics_checkpoint(board)

    def test_extra_or_unlisted_files_are_rejected(self):
        board = self.copy_board()
        (board / "events-copy.ndjson").write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(AnalyticsManifestError, "events-copy.ndjson: unlisted"):
            verify_analytics_checkpoint(board)

    def test_missing_or_unknown_manifest_is_rejected(self):
        board = self.copy_board()
        (board / ANALYTICS_MANIFEST).unlink()
        with self.assertRaisesRegex(AnalyticsManifestError, "analytics-manifest.json: manifest is missing"):
            verify_analytics_checkpoint(board)

        board = self.copy_board()
        manifest = self.manifest(board)
        manifest["schema"] = "ummanu.board.unknown"
        self.write_manifest(board, manifest)
        with self.assertRaisesRegex(AnalyticsManifestError, "unknown manifest schema"):
            verify_analytics_checkpoint(board)

        board = self.copy_board()
        manifest = self.manifest(board)
        manifest["version"] = 3
        self.write_manifest(board, manifest)
        with self.assertRaisesRegex(AnalyticsManifestError, "unknown manifest version"):
            verify_analytics_checkpoint(board)

        board = self.copy_board()
        (board / "events.ndjson").unlink()
        with self.assertRaisesRegex(
            AnalyticsManifestError, "events.ndjson: required analytics file is missing"
        ):
            verify_analytics_checkpoint(board)

    def test_version_one_seal_without_audit_remains_readable(self):
        board = self.copy_board()
        manifest = self.manifest(board)
        entries = [entry for entry in manifest["files"] if entry["path"] != "audit.ndjson"]
        manifest["version"] = 1
        manifest["files"] = entries
        manifest["checkpoint_id"] = _analytics_checkpoint_id(entries)
        self.write_manifest(board, manifest)
        (board / "audit.ndjson").unlink()

        verify_analytics_checkpoint(board)

    def test_version_two_seal_rejects_audit_history_tampering(self):
        board = self.copy_board()
        (board / "audit.ndjson").write_text('{"request_id":"changed"}\n', encoding="utf-8")

        with self.assertRaisesRegex(AnalyticsManifestError, "audit.ndjson: sha256"):
            verify_analytics_checkpoint(board)

    def test_malformed_manifest_duplicate_entry_and_bad_file_metadata_are_rejected(self):
        board = self.copy_board()
        (board / ANALYTICS_MANIFEST).write_text("not json\n", encoding="utf-8")
        with self.assertRaisesRegex(AnalyticsManifestError, "could not parse manifest"):
            verify_analytics_checkpoint(board)

        cases = {
            "duplicate": lambda manifest: manifest["files"].append(dict(manifest["files"][0])),
            "malformed-id": lambda manifest: manifest.update(checkpoint_id="not-a-digest"),
            "malformed-digest": lambda manifest: manifest["files"][0].update(sha256="not-a-digest"),
            "malformed-bytes": lambda manifest: manifest["files"][0].update(bytes=True),
            "malformed-lines": lambda manifest: manifest["files"][0].update(line_count=-1),
            "digest": lambda manifest: manifest["files"][0].update(sha256="0" * 64),
            "bytes": lambda manifest: manifest["files"][0].update(bytes=999),
            "lines": lambda manifest: manifest["files"][0].update(line_count=999),
        }
        expected = {
            "duplicate": "duplicate manifest entry",
            "malformed-id": "checkpoint_id must be",
            "malformed-digest": "malformed sha256",
            "malformed-bytes": "malformed bytes",
            "malformed-lines": "malformed line_count",
            "digest": "sha256 does not match",
            "bytes": "byte count does not match",
            "lines": "line count does not match",
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                board = self.copy_board()
                manifest = self.manifest(board)
                mutate(manifest)
                self.write_manifest(board, manifest)
                with self.assertRaisesRegex(AnalyticsManifestError, expected[name]):
                    verify_analytics_checkpoint(board)

    def test_legacy_eventless_unsealed_directory_remains_restorable_but_not_analytics_input(self):
        board = self.copy_board()
        (board / ANALYTICS_MANIFEST).unlink()
        (board / "events.ndjson").unlink()

        _validate_board(board, registered_project_ids=set())
        with self.assertRaisesRegex(AnalyticsManifestError, "analytics-manifest.json: manifest is missing"):
            verify_analytics_checkpoint(board)


class FakeClock:
    """A clock the push window can be walked forward by hand."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CheckpointPusherPrivilegeTests(unittest.TestCase):
    """Every pusher probe shares the owner-safe instance-repository runner."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.instance = Path(self.tmpdir.name) / "instance"
        (self.instance / ".git").mkdir(parents=True)

    @staticmethod
    def result(args: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    def test_branch_probe_reachability_and_push_all_use_state_repo_runner(self):
        head = "a" * 40
        remote = "b" * 40
        commands: list[list[str]] = []

        def run_git(command: list[str], **_kwargs):
            commands.append(command)
            args = command[command.index("-C") + 2 :]
            operation = args
            if operation[:3] == ["symbolic-ref", "--quiet", "--short"]:
                return self.result(operation, stdout="main\n")
            if operation == ["remote"]:
                return self.result(operation, stdout="origin\n")
            if operation == ["remote", "get-url", "origin"]:
                return self.result(operation, stdout="/tmp/checkpoint.git\n")
            if operation == ["rev-parse", "HEAD"]:
                return self.result(operation, stdout=f"{head}\n")
            if operation[:3] == ["ls-remote", "--heads", "origin"]:
                return self.result(operation, stdout=f"{remote}\trefs/heads/main\n")
            if operation[:2] == ["cat-file", "-e"]:
                return self.result(operation)
            if operation[:2] == ["merge-base", "--is-ancestor"]:
                return self.result(operation)
            if operation == ["push", "--quiet", "origin", "HEAD:refs/heads/main"]:
                return self.result(operation)
            self.fail(f"unexpected pusher Git command: {operation}")

        with (
            mock.patch(
                "ummanu.checkpoint.state_repo.state_repo_lock", return_value=contextlib.nullcontext()
            ),
            mock.patch("ummanu.state_repo.os.getuid", return_value=0),
            mock.patch("ummanu.state_repo.pwd.getpwuid", return_value=SimpleNamespace(pw_name="runtime")),
            mock.patch("ummanu.state_repo._proc.run_isolated", side_effect=run_git),
        ):
            state = CheckpointPusher(self.instance).push()

        self.assertEqual(state["status"], "pushed")
        self.assertEqual(
            [command[command.index("-C") + 2 :] for command in commands],
            [
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                ["remote"],
                ["remote", "get-url", "origin"],
                ["rev-parse", "HEAD"],
                ["ls-remote", "--heads", "origin", "refs/heads/main"],
                ["cat-file", "-e", f"{remote}^{{commit}}"],
                ["merge-base", "--is-ancestor", remote, head],
                ["push", "--quiet", "origin", "HEAD:refs/heads/main"],
            ],
        )
        for command in commands:
            git = command.index("git")
            self.assertEqual(command[:5], ["runuser", "--user", "runtime", "--", "env"])
            self.assertIn("GIT_TERMINAL_PROMPT=0", command)
            self.assertIn("GIT_SSH_COMMAND=ssh -o BatchMode=yes", command)
            self.assertEqual(
                command[git + 1 : git + 5],
                [
                    "-c",
                    f"safe.directory={self.instance.resolve()}",
                    "-c",
                    "core.hooksPath=/dev/null",
                ],
            )

    def test_branch_discovery_failure_remains_actionable_not_no_branch(self):
        failure = self.result(
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            returncode=128,
            stderr="fatal: detected dubious ownership\n",
        )
        with mock.patch("ummanu.checkpoint.state_repo.run_git", return_value=failure):
            state = CheckpointPusher(self.instance).push()

        self.assertEqual(state["status"], "failed")
        self.assertIn("dubious ownership", state["reason"])
        self.assertNotIn("no checked-out branch", state["reason"])


class CheckpointPusherTests(unittest.TestCase):
    """Contract: docs/RECOVERY.md, "Cadence and RPO", "Failure and divergence"."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.remote))
        self.instance_dir = self.root / "secretary-instance"
        self.instance_dir.mkdir()
        git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(self.instance_dir, "config", "user.name", "operator")
        git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        git(self.instance_dir, "remote", "add", "origin", str(self.remote))
        self.commit("config")
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def commit(self, message: str, *, repo: Path | None = None) -> str:
        repo = repo or self.instance_dir
        (repo / "instance.yaml").write_text(f"version: {message}\n", encoding="utf-8")
        git(repo, "add", "instance.yaml")
        git(repo, "commit", "--quiet", "-m", message)
        return git(repo, "rev-parse", "HEAD").strip()

    def pusher(self, **kwargs) -> CheckpointPusher:
        kwargs.setdefault("clock", self.clock)
        return CheckpointPusher(self.instance_dir, **kwargs)

    def remote_head(self) -> str:
        return git(self.remote, "rev-parse", "refs/heads/main").strip()

    def push_from_elsewhere(self) -> str:
        """Land a commit on the remote that the instance repo has never seen."""
        other = self.root / "other"
        git(self.root, "clone", "--quiet", str(self.remote), str(other))
        git(other, "config", "user.name", "operator")
        git(other, "config", "user.email", "operator@example.invalid")
        (other / "operator.txt").write_text("resolved by hand\n", encoding="utf-8")
        git(other, "add", "operator.txt")
        git(other, "commit", "--quiet", "-m", "from-another-host")
        git(other, "push", "--quiet", "origin", "main")
        return git(other, "rev-parse", "HEAD").strip()

    def test_first_push_lands_the_checkpoint_on_the_remote(self):
        state = self.pusher().push()

        self.assertEqual(state["status"], "pushed")
        self.assertEqual(self.remote_head(), git(self.instance_dir, "rev-parse", "HEAD").strip())
        self.assertEqual(state["last_push_commit"], self.remote_head())
        self.assertFalse(state["remote_diverged"])

    def test_push_waits_for_its_window_and_runs_once_the_window_is_due(self):
        state = self.pusher().push()
        head = self.commit("second")

        self.clock.advance(29 * 60)
        state = self.pusher().push(state)
        self.assertNotEqual(self.remote_head(), head)

        self.clock.advance(2 * 60)
        state = self.pusher().push(state)
        self.assertEqual(state["status"], "pushed")
        self.assertEqual(self.remote_head(), head)

    def test_a_checkpoint_already_on_the_remote_needs_no_push(self):
        state = self.pusher().push()
        self.clock.advance(PUSH_INTERVAL_SECONDS)

        state = self.pusher().push(state)

        self.assertEqual(state["status"], "unchanged")
        self.assertEqual(state["failures"], 0)

    def test_push_failure_is_recorded_and_the_next_window_retries(self):
        git(self.instance_dir, "remote", "set-url", "origin", str(self.root / "missing.git"))

        state = self.pusher().push()

        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["reason"])
        self.assertEqual(state["failures"], 1)
        self.assertFalse(state["remote_diverged"])

        git(self.instance_dir, "remote", "set-url", "origin", str(self.remote))
        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)

        self.assertEqual(state["status"], "pushed")
        self.assertEqual(state["failures"], 0)

    def test_failed_push_leaves_a_growing_lag_that_status_can_see(self):
        git(self.instance_dir, "remote", "set-url", "origin", str(self.root / "missing.git"))
        state = self.pusher().push()
        self.commit("second")

        snapshot = checkpoint_snapshot(self.instance_dir, push_state=state)

        self.assertEqual(snapshot["push_status"], "failed")
        self.assertEqual(snapshot["lag_commits"], 2)
        self.assertEqual(snapshot["last_push_at"], "")

    def test_remote_with_commits_we_lack_stops_the_push_without_forcing(self):
        state = self.pusher().push()
        theirs = self.push_from_elsewhere()
        self.commit("ours")
        self.clock.advance(PUSH_INTERVAL_SECONDS)

        state = self.pusher().push(state)

        self.assertEqual(state["status"], "diverged")
        self.assertTrue(state["remote_diverged"])
        # The remote still carries their commit: nothing overwrote it.
        self.assertEqual(self.remote_head(), theirs)

    def test_divergence_keeps_the_push_stopped_until_the_operator_resolves_it(self):
        state = self.pusher().push()
        theirs = self.push_from_elsewhere()
        self.commit("ours")
        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)

        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)
        self.assertEqual(state["status"], "diverged")
        self.assertEqual(self.remote_head(), theirs)

        # The operator merges the remote work by hand; the next window resumes.
        git(self.instance_dir, "fetch", "--quiet", "origin", "main")
        git(self.instance_dir, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)

        self.assertEqual(state["status"], "pushed")
        self.assertFalse(state["remote_diverged"])
        self.assertEqual(self.remote_head(), git(self.instance_dir, "rev-parse", "HEAD").strip())

    def test_diverged_state_rechecks_without_waiting_for_the_next_window(self):
        state = self.pusher().push()
        self.push_from_elsewhere()
        ours = self.commit("ours")
        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)

        git(self.instance_dir, "fetch", "--quiet", "origin", "main")
        git(self.instance_dir, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
        resolved = git(self.instance_dir, "rev-parse", "HEAD").strip()
        self.assertTrue(is_ancestor(self.instance_dir, ours, resolved))
        state = self.pusher().push(state)

        self.assertEqual(state["status"], "pushed")
        self.assertFalse(state["remote_diverged"])
        self.assertEqual(self.remote_head(), resolved)

    def test_coordinator_bounds_sticky_divergence_preparation_to_the_checkpoint_cadence(self):
        writer = mock.Mock()
        writer.write.return_value = CheckpointResult(status="unchanged")
        pusher = self.pusher()
        runtime = SimpleNamespace(checkpoint=writer, checkpoint_push=pusher)
        payload: dict[str, object] = {}

        with mock.patch.object(
            pusher, "_attempt", return_value=PushOutcome("diverged", "remote has unrelated history")
        ) as attempt:
            checkpoint, push = _coordinate_checkpoint(runtime, payload)
            self.assertEqual(checkpoint["status"], "unchanged")
            self.assertEqual(push["status"], "diverged")
            self.assertTrue(payload["checkpoint_push"]["remote_diverged"])

            for _ in range(4):
                self.clock.advance(60)
                checkpoint, push = _coordinate_checkpoint(runtime, payload)
                self.assertEqual(checkpoint["status"], "skipped")
                self.assertEqual(push["status"], "diverged")

            self.assertEqual(writer.write.call_count, 1)
            self.assertEqual(attempt.call_count, 5, "the real pusher still performs its sticky recheck")
            self.clock.advance(60)
            checkpoint, push = _coordinate_checkpoint(runtime, payload)

        self.assertEqual(checkpoint["status"], "unchanged")
        self.assertEqual(push["status"], "diverged")
        self.assertEqual(writer.write.call_count, 2)
        self.assertEqual(attempt.call_count, 6)

    def test_coordinator_consumes_withheld_preparation_retry_after_failed_delivery(self):
        writer = mock.Mock()
        writer.write.side_effect = (
            CheckpointResult(status="unchanged"),
            CheckpointResult(status="blocked", reason="audit pending"),
            CheckpointResult(status="unchanged"),
        )
        pusher = self.pusher()
        runtime = SimpleNamespace(checkpoint=writer, checkpoint_push=pusher)
        payload: dict[str, object] = {}

        with mock.patch.object(
            pusher,
            "_attempt",
            return_value=PushOutcome("failed", "could not resolve host github.com"),
        ) as attempt:
            _coordinate_checkpoint(runtime, payload)
            self.assertEqual(attempt.call_count, 1)
            self.assertNotIn("retry_pending", payload["checkpoint_push"])

            self.clock.advance(PUSH_INTERVAL_SECONDS)
            checkpoint, push = _coordinate_checkpoint(runtime, payload)
            self.assertEqual(checkpoint["status"], "blocked")
            self.assertEqual(push["status"], "skipped")
            self.assertTrue(push["retry_pending"])
            self.assertEqual(attempt.call_count, 1, "blocked preparation withholds remote delivery")

            self.clock.advance(60)
            checkpoint, push = _coordinate_checkpoint(runtime, payload)
            self.assertEqual(checkpoint["status"], "unchanged")
            self.assertEqual(push["status"], "failed")
            self.assertNotIn("retry_pending", push)
            self.assertEqual(attempt.call_count, 2)

            for _ in range(4):
                self.clock.advance(60)
                checkpoint, push = _coordinate_checkpoint(runtime, payload)
                self.assertEqual(checkpoint["status"], "skipped")
                self.assertIsNone(push)

        self.assertEqual(writer.write.call_count, 3)
        self.assertEqual(attempt.call_count, 2)

    def test_a_remote_that_moves_under_the_push_reads_as_divergence(self):
        state = self.pusher().push()
        theirs = self.push_from_elsewhere()
        self.commit("ours")
        self.clock.advance(PUSH_INTERVAL_SECONDS)

        pusher = self.pusher()
        # The probe saw a remote that has already moved on by the time git pushes;
        # git's own rejection has to land as divergence, not as a generic failure.
        with mock.patch.object(pusher, "_remote_head", return_value=""):
            state = pusher.push(state)

        self.assertEqual(state["status"], "diverged")
        self.assertTrue(state["remote_diverged"])
        self.assertEqual(self.remote_head(), theirs)

    def test_an_instance_without_a_remote_is_skipped_rather_than_failed(self):
        git(self.instance_dir, "remote", "remove", "origin")

        state = self.pusher().push()

        self.assertEqual(state["status"], "skipped")
        self.assertIn("no remote", state["reason"])
        self.assertEqual(state["failures"], 0)

    def test_a_secret_store_change_rides_the_tick_commit_and_the_same_push(self):
        """secretary-777, after ummanu-24: the store writes files and makes no commit of its own; the
        legacy tick commits its exported files with the rest of the canon, so there is still one
        HEAD and one push, and nothing routes a secret around it."""
        from ummanu.secret_store import initialize_store, set_secret

        fast_params = {
            "format": "ummanu.installation-key",
            "version": 1,
            "kdf": {"id": "scrypt", "salt": "", "length": 32, "n": 2**8, "r": 8, "p": 1},
        }

        def fast_key_params():
            from ummanu import secret_store

            params = json.loads(json.dumps(fast_params))
            params["kdf"]["salt"] = secret_store._b64(b"0123456789abcdef")
            return params

        before_secret = git(self.instance_dir, "rev-parse", "HEAD").strip()
        with mock.patch("ummanu.secret_store._new_key_params", side_effect=fast_key_params):
            initialize_store(self.instance_dir, phrase="one two three four", actor="tester")
            set_secret(
                self.instance_dir,
                secret_id="service.api-token",
                value=b"token-value",
                scope="installation",
                purpose="board api",
                actor="tester",
            )
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), before_secret)

        self.data_dir = self.root / "ummanu-data"
        CheckpointWriterTests.seed_board(self, [CARD])
        CheckpointWriterTests.seed_runs(self, [])

        class Settled:
            def status(self) -> dict:
                return {"ok": True, "pending": 0}

        def exported(name: str):
            def export(data_dir, **_kwargs):
                lines = (Path(data_dir) / name).read_text(encoding="utf-8")
                return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

            return export

        with (
            mock.patch.object(CheckpointWriter, "_audit_owner", return_value=(None, Settled())),
            mock.patch("ummanu.checkpoint.export_board", side_effect=exported("board/cards.ndjson")),
            mock.patch("ummanu.checkpoint.export_runs", side_effect=exported("runs/runs.ndjson")),
        ):
            result = CheckpointWriter(self.data_dir, self.instance_dir).write()

        self.assertEqual(result.status, "committed", result.reason)
        head = git(self.instance_dir, "rev-parse", "HEAD").strip()
        touched = git(self.instance_dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertIn("secrets/values/service.api-token.enc.json", touched)
        self.assertIn("state/runs/runs.ndjson", touched)
        self.assertNotIn("secrets/installation.key", touched)

        snapshot_before_push = checkpoint_snapshot(self.instance_dir)
        self.assertGreaterEqual(snapshot_before_push["lag_commits"], 1)

        state = self.pusher().push()

        self.assertEqual(state["status"], "pushed")
        self.assertEqual(self.remote_head(), head)
        snapshot = checkpoint_snapshot(self.instance_dir, push_state=state)
        self.assertEqual(snapshot["lag_commits"], 0)


class CheckpointSnapshotTests(unittest.TestCase):
    """Contract: docs/RECOVERY.md, "Observability"."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.instance_dir = Path(self.tmpdir.name) / "secretary-instance"
        self.instance_dir.mkdir(parents=True)
        git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(self.instance_dir, "config", "user.name", "operator")
        git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance_dir, "add", "instance.yaml")
        git(self.instance_dir, "commit", "--quiet", "-m", "config")
        self.head = git(self.instance_dir, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_the_duration_of_the_run_the_status_describes_is_exposed(self):
        """`ummanu status` is where an operator reads what the last checkpoint run cost."""
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            write_state={"status": "committed", "at": "2026-07-20T10:00:00Z", "duration_ms": 1234.5},
        )

        self.assertEqual(snapshot["checkpoint_status"], "committed")
        self.assertEqual(snapshot["checkpoint_duration_ms"], 1234.5)
        self.assertIn("checkpoint: committed in 1234 ms", render_checkpoint_lines(snapshot))

    def test_a_state_written_before_durations_existed_reports_no_time_rather_than_a_wrong_one(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir, write_state={"status": "committed", "at": "2026-07-20T10:00:00Z"}
        )

        self.assertEqual(snapshot["checkpoint_duration_ms"], 0.0)

    def test_a_pushed_checkpoint_reports_no_lag(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            push_state={
                "status": "pushed",
                "last_push_at": "2026-07-20T10:00:00Z",
                "last_push_commit": self.head,
            },
        )

        self.assertEqual(snapshot["last_commit"], self.head)
        self.assertEqual(snapshot["lag_commits"], 0)
        self.assertEqual(snapshot["lag_minutes"], 0)
        self.assertEqual(snapshot["last_push_at"], "2026-07-20T10:00:00Z")

    def test_lag_counts_the_commits_the_remote_does_not_have(self):
        pushed = self.head
        for index in range(2):
            (self.instance_dir / "state.txt").write_text(f"{index}\n", encoding="utf-8")
            git(self.instance_dir, "add", "state.txt")
            git(self.instance_dir, "commit", "--quiet", "-m", f"checkpoint {index}")

        snapshot = checkpoint_snapshot(self.instance_dir, push_state={"last_push_commit": pushed})

        self.assertEqual(snapshot["lag_commits"], 2)
        self.assertIsInstance(snapshot["lag_minutes"], int)

    def test_the_blocked_gate_reason_and_divergence_alarm_are_visible(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            write_state={"status": "blocked", "reason": "secret detected in state/board/cards.ndjson"},
            push_state={
                "status": "diverged",
                "reason": "remote origin/main is at deadbeef",
                "remote_diverged": True,
            },
        )

        self.assertEqual(snapshot["blocked_reason"], "secret detected in state/board/cards.ndjson")
        self.assertTrue(snapshot["remote_diverged"])
        lines = "\n".join(render_checkpoint_lines(snapshot))
        self.assertIn("alarm: remote diverged", lines)
        self.assertIn("blocked: secret detected", lines)

    def test_failed_operation_attempt_has_its_own_time_and_age(self):
        attempted = "2026-07-20T10:00:00+00:00"
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            push_state={
                "status": "failed",
                "reason": "earlier credential was missing",
                "attempted_at": attempted,
                "attempted_epoch": datetime.fromisoformat(attempted).timestamp(),
                "last_push_at": "2026-07-20T09:00:00+00:00",
                "last_push_commit": self.head,
            },
            now=datetime.fromisoformat("2026-07-20T10:12:00+00:00").timestamp(),
        )

        self.assertEqual(snapshot["push_attempted_at"], attempted)
        self.assertEqual(snapshot["push_attempt_age_minutes"], 12)
        self.assertEqual(snapshot["push_attempt_freshness"], "fresh")
        self.assertEqual(snapshot["last_push_at"], "2026-07-20T09:00:00+00:00")
        lines = "\n".join(render_checkpoint_lines(snapshot))
        self.assertIn("last push attempt: 2026-07-20T10:00:00+00:00 (12 min ago, fresh)", lines)
        self.assertIn("push reason: earlier credential was missing", lines)

    def test_a_committed_gate_reports_no_blocking_reason(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            write_state={"status": "committed", "reason": ""},
        )

        self.assertEqual(snapshot["blocked_reason"], "")
        self.assertEqual(snapshot["push_status"], "pending")

    def test_cadence_snapshot_keeps_a_failure_visible_across_a_quiet_skip(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            write_state={
                "status": "skipped",
                "reason": "not due",
                "last_success_epoch": 1_000.0,
                "last_success_at": "1970-01-01T00:16:40Z",
                "last_success_status": "unchanged",
                "last_failure_epoch": 1_050.0,
                "last_failure_at": "1970-01-01T00:17:30Z",
                "last_failure_reason": "audit pending",
                "retry_pending": True,
                "skip_epoch": 1_060.0,
                "skip_at": "1970-01-01T00:17:40Z",
                "next_due_epoch": 1_300.0,
                "next_due_at": "1970-01-01T00:21:40Z",
            },
            now=1_120.0,
        )

        self.assertEqual(snapshot["checkpoint_status"], "skipped")
        self.assertEqual(snapshot["last_checkpoint_prepared_status"], "unchanged")
        self.assertEqual(snapshot["checkpoint_last_failure_reason"], "audit pending")
        self.assertEqual(snapshot["blocked_reason"], "audit pending")
        self.assertTrue(snapshot["checkpoint_retry_pending"])
        lines = "\n".join(render_checkpoint_lines(snapshot))
        # The line now states what the run cost as well as how it ended (secretary-1649). This
        # write state carries no duration, so the skip reports nought milliseconds, which is also
        # very nearly what a not-due decision actually costs.
        self.assertIn("checkpoint: skipped in 0 ms (retry pending)", lines)
        self.assertIn("checkpoint last skipped: 1970-01-01T00:17:40Z (not due)", lines)

    def test_non_https_remote_is_reported_as_bypass_before_the_first_push(self):
        with mock.patch(
            "ummanu.checkpoint.state_repo.git", return_value="ssh://example.invalid/private.git\n"
        ):
            snapshot = checkpoint_snapshot(self.instance_dir)
        self.assertEqual(snapshot["credential"]["state"], "ambient/manual-bypass")
        self.assertEqual(snapshot["credential"]["source"], "remote")


class BoardEventCheckpointCompatibilityTests(unittest.TestCase):
    def test_legacy_decision_event_is_accepted_by_checkpoint_validation(self) -> None:
        event = Event(
            "legacy-decision",
            EventKind.CARD_DECIDED,
            EntityKind.CARD,
            "ummanu-1546",
            Actor("observer", "observer"),
            "rework it",
            datetime(2026, 9, 3, 22, 0, tzinfo=UTC),
            data={
                "marker": "decision:rework",
                "decision": "rework",
                "body": "rework it",
                "body_sha256": "a" * 64,
                "assessment_visit": "assessment-1",
                "description_sha256": "b" * 64,
                "specification_revision": "specification-1",
                "protocol_prerequisites": [],
            },
        )
        record = event.to_record("legacy-decision-request")
        del record["data"]["protocol_prerequisites"]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            _validate_board_events(path)


if __name__ == "__main__":
    unittest.main()
