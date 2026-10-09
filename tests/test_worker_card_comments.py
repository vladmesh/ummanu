"""secretary-1768: PO, owner and observer comments reach the worker who executes the card.

On launch and rework TASK.md carries every such comment, oldest first, in its own section after the
description and before the red bodies. A comment that lands while the worker runs is pointed at
once, through the same `_nudge_worker` delivery the report prompt and the continuation use, after
TASK.md has been rewritten to hold it.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.dispatcher_fixtures import (
    PromptAfterStartCatalog,
    RecordingReviewHost,
    supervised_run,
    write_heartbeat,
)
from tests.dispatcher_fixtures import clear_env as _clear_env
from ummanu.dispatch import worker_report
from ummanu.dispatch.heartbeat import run_heartbeat_identity
from ummanu.dispatch.runtime_provenance import ProductionRuntime
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.dispatch.worker_comments import (
    WORKER_COMMENTS_HEADING,
    WORKER_COMMENTS_RULE,
    select_worker_comments,
    task_doc_comment_keys,
)
from ummanu.dispatch.worker_lifecycle import WorkerContinuation, WorkerContinuationStage
from ummanu.runtime.head import operations as head_ops
from ummanu.tasks import _digest

REF = "ummanu-1768"
DESCRIPTION = "Build the frobnicator."
EXCLUDED_ROLES = ("dispatcher", "worker", "reviewer", "steward", "retro")


class CardAudit:
    """The card audit the host reads, answering committed events for one ref in append order."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def events(self, reference: str = "", **_ignored) -> list[dict]:
        return [dict(record) for record in self.records if not reference or record.get("ref") == reference]


class Card:
    """A card and its audit, written the way `TaskWriter.comment` writes both."""

    def __init__(self) -> None:
        self.audit = CardAudit()
        self.task = {
            "ref": REF,
            "project": "ummanu",
            "type": "code",
            "description": DESCRIPTION,
            "workspace": {"base_branch": "main"},
            "routing": {},
            "comments": [],
        }
        self.audit.records.append(
            {
                "event_id": "evt_created",
                "kind": "created",
                "outcome": "success",
                "ref": REF,
                "occurred_at": "2026-09-26T10:00:00Z",
                "payload": {"description_sha256": _digest(DESCRIPTION)},
            }
        )

    def comment(self, role: str, body: str, *, at: str, event_id: str) -> None:
        self.task["comments"].append({"created_at": at, "body": f"[{role}]\n{body}", "marker": role})
        self.audit.records.append(
            {
                "event_id": event_id,
                "kind": "commented",
                "outcome": "success",
                "ref": REF,
                "occurred_at": at,
                "actor": {"role": role, "id": role},
                "payload": {"marker": role, "body_sha256": _digest(body)},
            }
        )

    def review_red(self, body: str, *, at: str) -> None:
        """A reviewer verdict bound to the current description, as `TaskWriter.verdict` binds it."""
        self.task["comments"].append({"created_at": at, "body": f"[review:red]\n{body}", "marker": "review:red"})
        self.audit.records.append(
            {
                "event_id": "evt_review_red",
                "request_id": attempt_request_id("attempt-1", "review-red", REF, "1"),
                "kind": "verdict",
                "outcome": "success",
                "ref": REF,
                "occurred_at": at,
                "payload": {
                    "marker": "review:red",
                    "body": body,
                    "marker_occurrence": 1,
                    "specification_revision": "evt_created",
                    "description_sha256": _digest(DESCRIPTION),
                },
            }
        )

    def gate_red(self, body: str, *, at: str) -> None:
        self.task["comments"].append(
            {
                "created_at": at,
                "body": f"[dispatcher]\nThe mechanical validation gate is red: {body}",
                "marker": "dispatcher",
            }
        )


class HostCase(unittest.TestCase):
    """A real `CommandHostRuntime` above a recording `local-pty` backend: the document, the pointer
    and `_nudge_worker` run for real, and only the head operation is faked at its boundary."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "UMMANU_DISPATCHER_WORKER_COMMAND")
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root)
        os.environ["UMMANU_CODEX_SESSIONS"] = str(self.root / "sessions")
        os.environ["UMMANU_CLAUDE_PROJECTS"] = str(self.root / "claude-projects")
        self.card = Card()
        self.host = RecordingReviewHost(self.root, catalog=PromptAfterStartCatalog())
        self.host.audit = self.card.audit
        # The launch fence asks that this process imported the product it names: this checkout,
        # wherever the ambient environment points the installed one.
        self.host.production_runtime = ProductionRuntime.current(
            Path(__file__).resolve().parents[1], git_workspaces_root=self.root / "workspaces"
        )

    def record(self, **fields) -> DispatcherRecord:
        record = DispatcherRecord(
            worker=f"{REF}-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )
        for name, value in fields.items():
            setattr(record, name, value)
        return record

    def live_worker(self, **fields) -> DispatcherRecord:
        """A worker the record holds a live `local-pty` run of, with its heartbeat answering."""
        record = self.record(
            worker_pid_file=str(self.root / "w.pid"),
            worker_run={"adapter": "codex", "codex_mode": "tui"},
            **fields,
        )
        record.worker_head_run = supervised_run(
            "worker-running-run",
            workspace=str(self.workspace),
            task_ref=head_ops.TaskRef.card(REF),
            handle=record.handle,
            pid_file=record.worker_pid_file,
        )
        write_heartbeat(
            Path(record.worker_pid_file),
            os.getpid(),
            identity=run_heartbeat_identity(record.worker_head_run, role="worker"),
        )
        return record

    def task_doc(self) -> str:
        return (self.workspace / "TASK.md").read_text(encoding="utf-8")

    def first_launch(self) -> str:
        """Bring the worker up through `prepare_worker`, on the checkout the test owns."""
        self.host.mode = "noop"
        with (
            mock.patch.object(self.host, "restore_workspace", return_value=str(self.workspace)),
            mock.patch.object(self.host, "_validate_resumable_workspace"),
            mock.patch.object(self.host, "_workspace_environment_ready", return_value=True),
            mock.patch.object(self.host, "_prepare_workspace_environment"),
            mock.patch.object(self.host, "_run_setup"),
        ):
            self.host.prepare_worker(self.card.task, f"{REF}-w", "codex", attempt_id="attempt-1")
        self.host.mode = "real"
        return self.task_doc()


class TaskDocumentTests(HostCase):
    def test_first_launch_carries_every_po_owner_and_observer_comment_in_time_order(self) -> None:
        self.card.comment("po", "Narrow it: only the CLI.", at="2026-09-26T11:00:00Z", event_id="evt_po")
        self.card.comment("observer", "Skip the web half.", at="2026-09-26T12:00:00Z", event_id="evt_obs")
        self.card.comment("owner", "Keep the old flag.\n\nSecond paragraph.", at="2026-09-26T13:00:00Z", event_id="evt_own")

        document = self.first_launch()

        self.assertIn(WORKER_COMMENTS_HEADING, document)
        self.assertIn(WORKER_COMMENTS_RULE, document)
        po = document.index("### po, 2026-09-26T11:00:00Z\n\nNarrow it: only the CLI.")
        observer = document.index("### observer, 2026-09-26T12:00:00Z\n\nSkip the web half.")
        owner = document.index("### owner, 2026-09-26T13:00:00Z\n\nKeep the old flag.\n\nSecond paragraph.")
        self.assertLess(document.index(DESCRIPTION), document.index(WORKER_COMMENTS_HEADING))
        self.assertLess(po, observer)
        self.assertLess(observer, owner)
        self.assertEqual(task_doc_comment_keys(str(self.workspace)), {"evt_po", "evt_obs", "evt_own"})

    def test_first_launch_without_such_comments_has_no_section(self) -> None:
        self.card.comment("dispatcher", "claimed", at="2026-09-26T11:00:00Z", event_id="evt_d")

        document = self.first_launch()

        self.assertNotIn(WORKER_COMMENTS_HEADING, document)
        self.assertNotIn("### ", document)
        self.assertEqual(task_doc_comment_keys(str(self.workspace)), frozenset())

    def test_rework_adds_the_new_comment_and_keeps_the_section_before_the_red_bodies(self) -> None:
        self.card.comment("po", "First refinement.", at="2026-09-26T11:00:00Z", event_id="evt_po1")
        first = self.first_launch()
        self.assertNotIn("Second refinement.", first)
        self.card.review_red("Finding: the CLI flag is missing.", at="2026-09-26T12:00:00Z")
        self.card.gate_red("unit tests failed", at="2026-09-26T12:30:00Z")
        self.card.comment("observer", "Second refinement.", at="2026-09-26T13:00:00Z", event_id="evt_obs2")
        record = self.record(
            report_generation=2,
            rejected_sha="deadbeef",
            rejected_failure_class="substantive",
            rejected_failure_reason="gate-red",
        )

        self.host.restart_worker(self.card.task, record)

        document = self.task_doc()
        section = document.index(WORKER_COMMENTS_HEADING)
        first_comment = document.index("First refinement.")
        second_comment = document.index("Second refinement.")
        review = document.index("## Reviewer verdict to address")
        gate = document.index("## Mechanical gate failure to address")
        self.assertLess(document.index(DESCRIPTION), section)
        self.assertLess(section, first_comment)
        self.assertLess(first_comment, second_comment)
        self.assertLess(second_comment, review)
        self.assertLess(review, gate)
        self.assertIn("Finding: the CLI flag is missing.", document)
        self.assertEqual(task_doc_comment_keys(str(self.workspace)), {"evt_po1", "evt_obs2"})

    def test_excluded_roles_never_reach_the_section(self) -> None:
        for number, role in enumerate(EXCLUDED_ROLES):
            self.card.comment(role, f"note from {role}", at=f"2026-09-26T1{number}:00:00Z", event_id=f"evt_{role}")
        self.card.comment("po", "the one that counts", at="2026-09-26T19:00:00Z", event_id="evt_po")

        document = self.first_launch()

        section = document[document.index(WORKER_COMMENTS_HEADING) : document.index("## No subagents")]
        self.assertIn("the one that counts", section)
        for role in EXCLUDED_ROLES:
            self.assertNotIn(f"note from {role}", section)
            self.assertNotIn(f"### {role},", document)
        self.assertEqual(task_doc_comment_keys(str(self.workspace)), {"evt_po"})


class SelectorTests(unittest.TestCase):
    def test_each_comment_takes_its_own_event_even_when_two_bodies_are_equal(self) -> None:
        card = Card()
        card.comment("po", "same words", at="2026-09-26T11:00:00Z", event_id="evt_1")
        card.comment("po", "same words", at="2026-09-26T12:00:00Z", event_id="evt_2")

        selected = select_worker_comments(card.task, card.audit.events(REF))

        self.assertEqual([comment.key for comment in selected], ["evt_1", "evt_2"])
        self.assertEqual([comment.at for comment in selected], ["2026-09-26T11:00:00Z", "2026-09-26T12:00:00Z"])

    def test_a_comment_no_event_accounts_for_keeps_a_stable_key(self) -> None:
        card = Card()
        card.task["comments"].append({"created_at": "2026-09-26T11:00:00Z", "body": "[observer]\nmoved back", "marker": "observer"})

        first = select_worker_comments(card.task, card.audit.events(REF))
        again = select_worker_comments(card.task, card.audit.events(REF))

        self.assertEqual(len(first), 1)
        self.assertEqual(first, again)
        self.assertTrue(first[0].key.startswith("comment:observer:"))
        self.assertEqual(first[0].body, "moved back")


class MidRoundContinuationTests(HostCase):
    """The tick arm over a real host: `_nudge_worker` runs, the backend's `deliver` is the fake."""

    def setUp(self) -> None:
        super().setUp()
        self.saves: list[dict] = []
        self.runtime = SimpleNamespace(
            host=self.host,
            save_records=lambda payload, records: self.saves.append(
                {ref: record.to_json() for ref, record in records.items()}
            ),
            bind_codex_provider_ingress=lambda *args, **kwargs: None,
        )
        self.card.comment("po", "Before launch.", at="2026-09-26T11:00:00Z", event_id="evt_before")
        self.first_launch()

    def tick(self, record: DispatcherRecord):
        records = {REF: record}
        return worker_report.deliver_worker_comments(self.runtime, self.card.task, record, records, {}, "attempt-1")

    def test_a_comment_the_launch_document_already_carries_is_not_sent_again(self) -> None:
        self.assertIsNone(self.tick(self.live_worker()))
        self.assertEqual(self.host.backend.deliveries, [])

    def test_a_new_comment_is_delivered_once_task_doc_first_then_the_pointer(self) -> None:
        record = self.live_worker(report_generation=0)
        self.card.comment("observer", "Mid-round: drop the web half.", at="2026-09-26T14:00:00Z", event_id="evt_mid")
        seen_at_delivery: list[str] = []
        self.host.backend.on_deliver = lambda: seen_at_delivery.append(self.task_doc())

        outcome = self.tick(record)

        self.assertEqual(outcome["action"], "worker-comments-delivered")
        self.assertEqual(outcome["comments"], ["evt_mid"])
        [(run, pointer, subject)] = self.host.backend.deliveries
        self.assertEqual(run.run_id, "worker-running-run")
        self.assertEqual(subject, "worker-comments")
        self.assertEqual(pointer.document, str(self.workspace / "TASK.md"))
        self.assertIn("comments section", pointer.text)
        self.assertIn("generation 0", pointer.text)
        # The document the pointer names already holds the new comment when the pointer goes out.
        [document] = seen_at_delivery
        self.assertIn("Mid-round: drop the web half.", document)
        self.assertLess(document.index("Before launch."), document.index("Mid-round: drop the web half."))
        # Recorded by event id, and on disk before the send.
        self.assertEqual(record.worker_comment_deliveries, ("evt_mid",))
        self.assertEqual(self.saves[0][REF]["worker_comment_deliveries"], ["evt_mid"])

    def test_not_redelivered_on_a_repeated_tick_or_after_the_record_is_rebuilt(self) -> None:
        record = self.live_worker()
        self.card.comment("po", "Mid-round.", at="2026-09-26T14:00:00Z", event_id="evt_mid")
        self.tick(record)
        self.assertEqual(len(self.host.backend.deliveries), 1)

        self.assertIsNone(self.tick(record))
        rebuilt = DispatcherRecord.from_json(json.loads(json.dumps(record.to_json())))
        self.assertEqual(rebuilt.worker_comment_deliveries, ("evt_mid",))
        self.assertIsNone(self.tick(rebuilt))
        # Even a record that forgot the delivery reads it off the document the worker holds.
        rebuilt.worker_comment_deliveries = ()
        self.assertIsNone(self.tick(rebuilt))
        self.assertEqual(len(self.host.backend.deliveries), 1)

    def test_a_second_comment_later_in_the_round_is_its_own_single_delivery(self) -> None:
        record = self.live_worker()
        self.card.comment("po", "One.", at="2026-09-26T14:00:00Z", event_id="evt_one")
        self.tick(record)
        self.card.comment("owner", "Two.", at="2026-09-26T15:00:00Z", event_id="evt_two")

        outcome = self.tick(record)

        self.assertEqual(outcome["comments"], ["evt_two"])
        self.assertEqual(len(self.host.backend.deliveries), 2)
        self.assertEqual(record.worker_comment_deliveries, ("evt_one", "evt_two"))

    def test_a_parked_suspended_or_paused_worker_is_sent_nothing_and_the_next_round_carries_it(self) -> None:
        self.card.comment("po", "While parked.", at="2026-09-26T14:00:00Z", event_id="evt_parked")
        not_running = {
            "parked in Assessment": {
                "worker_continuation": WorkerContinuation(stage=WorkerContinuationStage.ASSESSMENT_PARKED)
            },
            "held for validation": {
                "worker_continuation": WorkerContinuation(
                    stage=WorkerContinuationStage.RETAINED, session_held=True
                )
            },
            "paused": {"paused_worker_at": 1.0},
            "between rounds": {"state": "claim_verified"},
        }
        for label, fields in not_running.items():
            with self.subTest(label):
                record = self.live_worker(**fields)
                self.assertIsNone(self.tick(record))
                self.assertEqual(record.worker_comment_deliveries, ())
        suspended = self.live_worker()
        with mock.patch.object(
            self.host, "_head_status", return_value={"known": True, "alive": True, "match": True, "state": "live-match", "stopped": True}
        ):
            self.assertIsNone(self.tick(suspended))
        self.assertEqual(self.host.backend.deliveries, [])
        self.assertNotIn("While parked.", self.task_doc())

        self.host.restart_worker(self.card.task, self.record(report_generation=1))

        self.assertIn("While parked.", self.task_doc())

    def test_excluded_roles_never_trigger_a_continuation(self) -> None:
        record = self.live_worker()
        for number, role in enumerate(EXCLUDED_ROLES):
            self.card.comment(role, f"note from {role}", at=f"2026-09-26T1{number}:30:00Z", event_id=f"evt_{role}")

        self.assertIsNone(self.tick(record))
        self.assertEqual(self.host.backend.deliveries, [])
        self.assertEqual(record.worker_comment_deliveries, ())

    def test_a_health_probe_never_delivers_a_comment(self) -> None:
        """The probe walks the tick with effects aborted; typing into a live worker is one."""
        from ummanu.dispatch.production import ProbeAbort, _ProbeHost

        with self.assertRaises(ProbeAbort):
            _ProbeHost(self.host).deliver_worker_comments(self.card.task, self.live_worker())
        self.assertEqual(self.host.backend.deliveries, [])

    def test_the_tick_offers_comments_after_the_report_and_headless_checks_and_before_the_wait(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "ummanu" / "dispatch" / "runtime.py").read_text(
            encoding="utf-8"
        )
        advance = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_advance_worker"
        )
        body = ast.get_source_segment(source, advance) or ""
        self.assertLess(body.index("_resolve_headless_worker("), body.index("_deliver_worker_comments("))
        self.assertLess(body.index("_deliver_worker_comments("), body.index("_wait_watchdog(self, "))


if __name__ == "__main__":
    unittest.main()
