"""The no-candidate lifecycle of research/infra cards and the stored review choice (secretary-1639).

A research or infra card goes from Ready to Done without a branch, pull request, CI run or workflow
dispatch, and reaches Done only through the one completion-evidence check. `review: skipped` means no
reviewer for any kind; a code card with it still runs the gate and merges.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.fakes.dispatcher import FakeCatalog
from tests.integration_setup import require_disposable_board_fixture
from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board.completion_evidence import (
    RESEARCH_TRANSFER_FILE_CAP_BYTES,
    has_candidate,
    infra_completion_record,
    infra_report_fields,
    missing_completion_evidence,
    render_infra_completion_record,
    render_research_completion_link,
    review_required,
)
from ummanu.dispatch import release_lifecycle
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.observer import render_observer_prompt
from ummanu.tasks import TaskError

INFRA_REPORT = "## What was done\nRotated the relay key.\n\n## How to verify\n`ssh relay true` exits 0\n"
# Host calls that would publish a branch, open a pull request, poll CI or dispatch a workflow: the
# fake host's gate and merge seams are where the real host does each of those.
CANDIDATE_CALLS = ("gate_check", "complete_green", "rerun_failed_ci")
REPORT_DIR = f"state/knowledge/reports/{CARD_REF}"
LEAKED = "token sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX\n"


def setUpModule() -> None:
    """Confirm this CI shard can build its disposable board seam before tests run."""
    require_disposable_board_fixture(PostgresBoard.shared)


class NoCandidateLifecycleTests(DispatcherRuntimeFixture, unittest.TestCase):
    def _kind(self, kind: str, *, review: str = "") -> None:
        """The card as `task create --type` stores it, with the kind's default review choice."""
        self.board.save_metadata(12, task_type=kind)
        self.board.save_metadata(12, review=review or ("required" if kind == "code" else "skipped"))

    def _unobserved(self) -> None:
        self.board.save_metadata(12, {"sprint_ref": ""})
        self.sprints.rows.clear()
        self.board.clear_sprints()

    def _comments(self, marker_line: str) -> list[str]:
        return [
            comment["body"]
            for comment in self.reader.show(CARD_REF)["comments"]
            if marker_line in comment["body"].splitlines()
        ]

    def _assert_no_candidate_calls(self) -> None:
        for call in CANDIDATE_CALLS:
            self.assertNotIn(call, self.host.calls)
        self.assertEqual(self.host.gate_calls, [])
        self.assertEqual(self.host.completed, [])

    def _instance_repo(self) -> None:
        """The fixture's instance directory as the live root the knowledge writer writes into.

        A plain directory: the writer makes no Git call, so nothing here is a work tree.
        """
        (self.data_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")

    def _write_report(self, files: dict[str, str]) -> None:
        """The worker's `.ummanu-report/`, replaced wholesale, and the report writer pointed at it."""
        workspace = Path(self._pilot_record()["workspace"])
        report = workspace / ".ummanu-report"
        if report.exists():
            for path in sorted(report.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
        for name, text in files.items():
            path = report / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.writer.workspace = workspace

    def _committed_report(self) -> list[str]:
        """Every file of the transferred report directory, by live-root path."""
        report = self.data_dir / REPORT_DIR
        return sorted(
            path.relative_to(self.data_dir).as_posix() for path in report.rglob("*") if path.is_file()
        )

    def _report_identity(self) -> int:
        """The inode of the report directory: a write swaps in a new one, a no-op keeps it."""
        return (self.data_dir / REPORT_DIR).stat().st_ino

    def _no_transfer(self):
        """A release whose transfer produced nothing: the evidence check alone decides."""
        return mock.patch.object(release_lifecycle, "transfer_research_report", return_value=None)

    def test_infra_with_review_skipped_goes_from_ready_to_done_without_a_candidate(self) -> None:
        self._kind("infra", review="skipped")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self.assertIn("no branch is published", self._task_document())
        self.assertIn("## How to verify", self._task_document())
        self.host.retained_workspace_dirty = True
        self._report_done(INFRA_REPORT)

        self.assertEqual(self.tick()["to"], "validate")
        done = self.tick()

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self._assert_no_candidate_calls()
        self.assertEqual(self.host.reviews, [], "review: skipped launches no reviewer")
        self.assertIn("teardown", self.host.calls)
        (record,) = self._comments("[completion:infra]")
        self.assertIn("Rotated the relay key.", record)
        self.assertIn("`ssh relay true` exits 0", record)
        self.assertNotIn(CARD_REF, self.runtime.production_state.load()["records"])

    def test_infra_in_a_parking_sprint_waits_in_assessment_for_the_release(self) -> None:
        self._kind("infra")
        self.start_dispatcher()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")

        released = self._park_and_decide("accept")

        self.assertEqual(released["to"], "done")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self._assert_no_candidate_calls()
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(len(self._comments("[completion:infra]")), 1)

    def test_blocked_infra_requeued_on_the_same_card_receives_a_fresh_worker_packet(self) -> None:
        self._kind("infra")
        self.start_dispatcher()
        self.tick()
        initial = self._pilot_record()
        self.writer.report(
            role="worker", actor="worker", reference=CARD_REF, kind="blocked",
            classification="wrong_task_definition", body="Execution contract requires replacement",
            request_id=self._worker_report_request_id("blocked", "wrong_task_definition"),
        )
        self.assertEqual(self.tick()["to"], "blocked")
        self.writer.move(
            role="po", actor="operator", reference=CARD_REF, target="ready",
            sprint_override=True, sprint_override_reason="Replace the blocked worker contract",
            reason="Generator activated; replace the worker execution contract",
            request_id="requeue-infra-contract",
        )
        self.assertEqual(self.tick()["step"], "claim")
        replacement = self._pilot_record()
        self.assertNotEqual(replacement["attempt_id"], initial["attempt_id"])
        self.assertEqual(replacement["workspace"], initial["workspace"])
        document = self._task_document()
        self.assertIn("First TASK.md of this attempt: read it whole.", document)
        self.assertIn("only to orchestrate an explicitly authorized remote target", document)
        self.assertIn("## Applicable sprint local_run_exceptions\n\nnone", document)
        self.assertIn(replacement["attempt_id"] + "-worker-report-done", document)
        self.assertEqual(self.host.prepared, [CARD_REF, CARD_REF])
        self._assert_no_candidate_calls()

    def test_an_infra_report_without_both_sections_is_refused_and_the_card_does_not_advance(self) -> None:
        self._kind("infra")
        self.start_dispatcher()
        self.tick()
        for body in ("## What was done\nRotated the relay key.\n", "## How to verify\n`ssh relay true`\n"):
            with self.subTest(body=body), self.assertRaises(TaskError) as caught:
                self._report_done(body)
            self.assertIn("## What was done", caught.exception.message)

        waiting = self.tick()

        self.assertEqual(waiting["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(self._comments("[completion:infra]"), [])

    def test_an_infra_release_without_its_record_is_blocked_with_the_missing_evidence_named(self) -> None:
        self._kind("infra")
        self.start_dispatcher()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["to"], "assessment")
        # The record is gone by the time the observer releases: the check reads the board, not the
        # acceptance that wrote it.
        self.board.remove_comments(12, "[completion:infra]")
        self._decide("accept")

        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "completion evidence missing")
        self.assertEqual(blocked["missing_evidence"], "completion:infra")
        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        self.assertIn("`[completion:infra]`", card["comments"][-1]["body"])
        self.assertNotIn("teardown", self.host.calls, "a card without evidence keeps its workspace")
        self._assert_no_candidate_calls()

    def test_a_research_card_without_a_completion_link_is_blocked_on_release(self) -> None:
        self._kind("research")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self._write_report({"report.md": "findings\n"})
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")

        with self._no_transfer():
            blocked = self.tick()

        self.assertEqual(blocked["reason"], "completion evidence missing")
        self.assertEqual(blocked["missing_evidence"], "completion:research")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self._assert_no_candidate_calls()

    def test_research_with_review_skipped_goes_from_ready_to_done_with_its_report_in_knowledge(self) -> None:
        self._instance_repo()
        self._kind("research", review="skipped")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self.assertIn("`.ummanu-report/report.md`", self._task_document())
        self._write_report({"report.md": "# Findings\n", "data/results.csv": "a,b\n1,2\n"})
        self._report_done("findings written up")

        self.assertEqual(self.tick()["to"], "validate")
        done = self.tick()

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self._assert_no_candidate_calls()
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(
            self._committed_report(), [f"{REPORT_DIR}/data/results.csv", f"{REPORT_DIR}/report.md"]
        )
        (link,) = self._comments("[completion:research]")
        self.assertIn(f"{REPORT_DIR}/", link.splitlines())
        dispatcher = [
            c for c in self.reader.show(CARD_REF)["comments"] if "[completion:research]" in c["body"]
        ]
        self.assertEqual([c["marker"] for c in dispatcher], ["dispatcher"])
        self.assertFalse((self.data_dir / ".git").exists(), "the transfer makes no Git call")

    def test_research_transfer_takes_small_text_and_names_what_stays_in_the_workspace(self) -> None:
        self._instance_repo()
        self._kind("research", review="skipped")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self._write_report(
            {
                "report.md": "# Findings\n",
                "notes.txt": "context\n",
                "data/results.csv": "a,b\n1,2\n",
                "data/cards.json": "{}\n",
                "data/huge.csv": "x\n" * (RESEARCH_TRANSFER_FILE_CAP_BYTES // 2 + 1),
                "__pycache__/probe.cpython-312.pyc": "bytecode\n",
            }
        )
        self._report_done("findings written up")
        while self.reader.show(CARD_REF)["state"] not in {"done", "blocked"}:
            self.tick()

        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self.assertEqual(
            self._committed_report(),
            [f"{REPORT_DIR}/data/results.csv", f"{REPORT_DIR}/notes.txt", f"{REPORT_DIR}/report.md"],
        )
        report = (self.data_dir / REPORT_DIR / "report.md").read_text(encoding="utf-8")
        workspace = Path(self.writer.workspace) / ".ummanu-report"
        self.assertTrue(report.startswith("# Findings\n"))
        self.assertIn(f"kept at `{workspace}`", report)
        for kept in ("data/cards.json", "data/huge.csv", "__pycache__/probe.cpython-312.pyc"):
            self.assertIn(f"`{kept}`", report)
            self.assertTrue((workspace / kept).is_file())

    def test_research_in_a_parking_sprint_is_transferred_and_linked_before_the_release_decision(self) -> None:
        self._instance_repo()
        self._kind("research")
        self.start_dispatcher()
        self.tick()
        self._write_report({"report.md": "# Findings\n"})
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")

        self.assertEqual(self.tick()["to"], "assessment")
        self.assertEqual(self._committed_report(), [f"{REPORT_DIR}/report.md"])
        self.assertEqual(len(self._comments("[completion:research]")), 1)
        transferred = self._report_identity()
        self._decide("accept")

        self.assertEqual(self.tick()["to"], "done")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self._assert_no_candidate_calls()
        self.assertEqual(self._report_identity(), transferred, "the release repeats the transfer as a no-op")
        self.assertEqual(len(self._comments("[completion:research]")), 1)

    def test_a_research_rework_round_replaces_the_report_directory(self) -> None:
        self._instance_repo()
        self._kind("research")
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        self._write_report({"report.md": "first\n", "notes/draft.md": "to be dropped\n"})
        self._report_done("first write-up")
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        self.assertIn(f"{REPORT_DIR}/notes/draft.md", self._committed_report())

        self._write_report({"report.md": "second\n"})
        self._report_done("second write-up")
        self.assertEqual(self.tick()["to"], "validate")
        released = self._park_and_decide("accept")

        self.assertEqual(released["to"], "done")
        self.assertEqual(self._committed_report(), [f"{REPORT_DIR}/report.md"])
        self.assertEqual((self.data_dir / REPORT_DIR / "report.md").read_text(encoding="utf-8"), "second\n")
        self.assertEqual(len(self._comments("[completion:research]")), 2)

    def test_a_research_done_report_without_its_report_file_is_refused(self) -> None:
        self._kind("research")
        self.start_dispatcher()
        self.tick()
        for files in ({}, {"report.md": "  \n"}, {"other.md": "findings\n"}):
            self._write_report(files)
            with self.subTest(files=files), self.assertRaises(TaskError) as caught:
                self._report_done("findings written up")
            self.assertEqual(caught.exception.code, "validation")
            self.assertIn(".ummanu-report/report.md", caught.exception.message)

        self.assertEqual(self.tick()["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")

    def _assert_transfer_refused_blocks(self, refusal: str) -> None:
        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "research report transfer refused")
        self.assertEqual(blocked["transfer_refusal"], refusal)
        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        self.assertIn(f"research report transfer refused ({refusal})", card["comments"][-1]["body"])
        self.assertEqual(self._comments("[completion:research]"), [])
        self.assertFalse((self.data_dir / REPORT_DIR).exists())
        self.assertNotIn("teardown", self.host.calls, "a refused transfer keeps the workspace")
        self._assert_no_candidate_calls()

    def test_a_report_holding_a_secret_blocks_the_card_and_commits_nothing(self) -> None:
        self._instance_repo()
        self._kind("research")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self._write_report({"report.md": "findings\n", "notes/probe.md": LEAKED})
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")

        self._assert_transfer_refused_blocks("secret")

    def test_a_report_over_the_size_cap_blocks_the_card_before_assessment(self) -> None:
        self._instance_repo()
        self._kind("research")
        self.start_dispatcher()
        self.tick()
        self._write_report({"report.md": "findings\n", "data/big.csv": "x" * 64})
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")

        with mock.patch("ummanu.knowledge_write.KNOWLEDGE_DIRECTORY_CAP_BYTES", 32):
            self._assert_transfer_refused_blocks("size_cap")

    def test_a_link_written_by_anyone_but_the_dispatcher_is_not_evidence(self) -> None:
        self._kind("research")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self.writer.comment(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            body=render_research_completion_link(CARD_REF),
            request_id="forged-link",
        )
        self._write_report({"report.md": "findings\n"})
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")

        with self._no_transfer():
            self.assertEqual(self.tick()["reason"], "completion evidence missing")

    def _assert_review_required_launches_a_reviewer_and_no_gate(self, kind: str) -> None:
        self._kind(kind, review="required")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        if kind == "research":
            self._write_report({"report.md": "findings\n"})
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")

        self.assertEqual(self.tick()["action"], "review-started")
        self.assertEqual(len(self.host.reviews), 1)
        self._assert_no_candidate_calls()

    def test_review_required_on_a_research_card_launches_a_reviewer_and_no_gate(self) -> None:
        self._assert_review_required_launches_a_reviewer_and_no_gate("research")

    def test_review_required_on_an_infra_card_launches_a_reviewer_and_no_gate(self) -> None:
        self._assert_review_required_launches_a_reviewer_and_no_gate("infra")

    def test_a_green_review_on_an_unobserved_infra_card_releases_through_the_evidence_check(self) -> None:
        self._kind("infra", review="required")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="the verification command is sound",
            request_id=self._review_verdict_request_id("green"),
        )

        self.assertEqual(self.tick()["to"], "done")
        self._assert_no_candidate_calls()

    def test_review_skipped_on_a_code_card_runs_the_gate_and_the_merge_without_a_reviewer(self) -> None:
        self._kind("code", review="skipped")
        self.start_dispatcher()
        self._unobserved()
        self._run_worker_to_validate()

        done = self.tick()

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.host.reviews, [])
        self.assertIn("gate_check", self.host.calls)
        self.assertEqual(self.host.completed, [CARD_REF])

        from ummanu.board.audit_contract import card_transition_of
        stages = [(card_transition_of(event) or ("", ""))[1]
                  for event in self.writer.audit.events(CARD_REF)]
        self.assertEqual([stage for stage in stages if stage in {"ci", "review", "assessment", "done"}],
                         ["ci", "assessment", "done"])

    def test_review_skipped_on_a_parked_code_card_merges_only_on_release(self) -> None:
        self._kind("code", review="skipped")
        self.start_dispatcher()
        self._run_worker_to_validate()

        released = self._park_and_decide("accept")

        self.assertEqual(released["to"], "done")
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(self.host.completed, [CARD_REF])

    def test_a_research_rework_round_is_accepted_with_an_unchanged_head(self) -> None:
        """The reviewer rejected the report, not a commit: the same HEAD is a fresh report."""
        self._kind("research", review="required")
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        self._write_report({"report.md": "first\n"})
        self._report_done("first write-up")
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self._review_red(body="the report misses the budget section")
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        self.assertEqual(self._pilot_record()["rejected_sha"], self.host.commit)

        self._report_done("second write-up, same checkout")
        advanced = self.tick()

        self.assertEqual(advanced["to"], "validate")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")
        self._assert_no_candidate_calls()

    def test_an_infra_rework_round_writes_a_fresh_completion_record(self) -> None:
        self._kind("infra")
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")

        self._report_done(INFRA_REPORT.replace("exits 0", "prints ok"))
        self.assertEqual(self.tick()["to"], "validate")
        released = self._park_and_decide("accept")

        self.assertEqual(released["to"], "done")
        records = self._comments("[completion:infra]")
        self.assertEqual(len(records), 2)
        self.assertIn("prints ok", records[-1])


class NoCandidateReviewerDocumentTests(DispatcherRuntimeFixture, unittest.TestCase):
    def _review_document(self, kind: str) -> str:
        self.board.save_metadata(12, task_type=kind)
        self.board.save_metadata(12, review="required")
        host = CommandHostRuntime(FakeCatalog(), self.data_dir, mode="noop")  # type: ignore[arg-type]
        return host._review_prompt(self.reader.show(CARD_REF), "attempt-1", 1)

    def test_a_research_reviewer_is_pointed_at_the_report_directory_not_the_branch(self) -> None:
        document = self._review_document("research")
        self.assertNotIn("commit messages on this branch", document)
        self.assertNotIn("inspect the diff", document)
        self.assertIn("`.ummanu-report/`", document)
        self.assertIn(f"`state/knowledge/reports/{CARD_REF}/`", document)

    def test_an_infra_reviewer_is_pointed_at_the_report_body(self) -> None:
        document = self._review_document("infra")
        self.assertNotIn("commit messages on this branch", document)
        self.assertIn("`## What was done`", document)
        self.assertIn("`## How to verify`", document)

    def test_a_code_reviewer_still_reads_the_commit_messages(self) -> None:
        document = self._review_document("code")
        self.assertIn("Read the commit messages on this branch, not only the diff.", document)
        self.assertNotIn(".ummanu-report", document)


class ObserverReviewChoiceWordingTests(unittest.TestCase):
    def test_whether_review_runs_is_the_card_s_review_choice_not_a_reviewer_head(self) -> None:
        document = render_observer_prompt({"ref": "sprint:1", "comments": []})
        self.assertIn("`--review skipped`", document)
        self.assertIn("skipped for `research` and `infra`", document)
        self.assertNotIn("--review-head none", document)


class CompletionEvidenceParsingTests(unittest.TestCase):
    def test_infra_fields_ignore_headings_inside_a_fence_and_keep_subheadings(self) -> None:
        body = (
            "## What was done\nRotated.\n### Detail\nboth relays\n\n"
            "## How to verify\n```\n## not a heading\nssh relay true\n```\n"
        )
        fields, refusal = infra_report_fields(body)
        self.assertEqual(refusal, "")
        self.assertIn("both relays", fields["What was done"])
        self.assertIn("## not a heading", fields["How to verify"])

    def test_the_record_reads_back_what_it_renders(self) -> None:
        fields, _ = infra_report_fields(INFRA_REPORT)
        task = {
            "ref": CARD_REF,
            "type": "infra",
            "comments": [
                {"marker": "dispatcher", "body": "[dispatcher]\n" + render_infra_completion_record(fields)}
            ],
        }
        self.assertEqual(infra_completion_record(task), fields)
        self.assertEqual(missing_completion_evidence(task), "")
        self.assertEqual(missing_completion_evidence({**task, "comments": []}), "completion:infra")

    def test_a_research_link_must_name_this_cards_report_directory(self) -> None:
        other = {
            "marker": "dispatcher",
            "body": "[dispatcher]\n" + render_research_completion_link("other-1"),
        }
        own = {"marker": "dispatcher", "body": "[dispatcher]\n" + render_research_completion_link(CARD_REF)}
        task = {"ref": CARD_REF, "type": "research", "comments": [other]}
        self.assertEqual(missing_completion_evidence(task), "completion:research")
        self.assertEqual(missing_completion_evidence({**task, "comments": [other, own]}), "")

    def test_a_code_card_needs_no_marker_and_the_review_choice_defaults_to_required(self) -> None:
        self.assertEqual(missing_completion_evidence({"ref": CARD_REF, "type": "code", "comments": []}), "")
        self.assertTrue(review_required({"type": "infra"}))
        self.assertFalse(review_required({"type": "code", "review": "skipped"}))
        self.assertTrue(has_candidate({"type": ""}))
        self.assertFalse(has_candidate({"type": "research"}))


if __name__ == "__main__":
    unittest.main()
