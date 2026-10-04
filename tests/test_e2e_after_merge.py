"""The e2e stage after the merge: one coalesced run on `main` per project, on a branch the dispatcher owns.

secretary-1807. A project whose adapter declares `validation.e2e.placement: after_merge` runs no e2e
before the merge; the post-merge watch queues each card whose merge commit's CI is green, and the
dispatcher runs the workflow on the newest queued merge SHA for every queued card on its line. The
board is the disposable PostgreSQL board of `tests/test_e2e_stage.py`, the budget is the real sprint row,
and GitHub is a fake behind `run_capture`: `main`'s history for the compare, the branch refs, the
dispatch, the run listing, the run the wait card reads, its jobs and its `--log-failed`.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from tests.e2e_stage_fixtures import REPO, E2eGitHubHost, E2eStageFixture, SimulatedCrash
from tests.integration_setup import require_disposable_board_fixture
from tests.sql_backend_fixtures import PostgresBoard
from ummanu._fsutil import file_lock
from ummanu.board import po_origin as origin_field
from ummanu.board.e2e_record import AfterMergeMark, e2e_state
from ummanu.board.owner_events import OwnerEventStore
from ummanu.dispatch import e2e_after_merge
from ummanu.dispatch.e2e_after_merge import queues, reconcile_after_merge
from ummanu.dispatch.post_merge import reconcile_post_merge_watches, watches
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import new_attempt_id, now_rfc3339
from ummanu.dispatch.types import HostError
from ummanu.sprints import SprintReader, SprintWriter
from ummanu.tasks import TaskError, is_significant_card_event

SPRINT = "sprint:1031"
OTHER_REPO = "vladmesh/codegen"
AFTER_MERGE = {
    "workflow": "e2e.yml",
    "inputs": {"suite": "mega-noop"},
    "candidate_input": "sha",
    "deadline": "2h",
    "placement": "after_merge",
}
FIRST_RUN = 7001


def setUpModule() -> None:
    require_disposable_board_fixture(PostgresBoard.shared)


def _sha(digit: str) -> str:
    return (digit * 40)[:40]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AfterMergeGitHub(E2eGitHubHost):
    """GitHub for after-merge runs: `main`'s line, the dispatcher's branches, runs dispatched on them.

    `history` is `main`'s first-parent line, oldest first: a merge SHA is an ancestor of every later
    one, and a SHA off it compares `diverged`. `crash_after_ref` makes the dispatcher die right after the
    branch was created; `ran_on` makes the next run report another head SHA than its branch's.
    """

    def __init__(self, root: Any, catalog: Any) -> None:
        super().__init__(root, catalog)
        self.history: list[str] = []
        self.refs: dict[str, str] = {}
        self.ref_log: list[tuple[str, str, str]] = []
        self.crash_after_ref = False
        self.ran_on = ""
        self.compares = 0
        # The coming DELETEs GitHub refuses with 422 while the branch stays: `refused`, or `unreadable`
        # (the read that would confirm the branch is gone then fails too).
        self.delete_refusals: list[str] = []
        self.ref_read_fails = False

    def run_capture(self, args: list[str], label: str, *, cwd: Any = None) -> subprocess.CompletedProcess:
        args = list(args)
        self.gh_calls.append(args)
        if args[:3] == ["gh", "repo", "view"]:
            return self._ok(args, REPO)
        if args[:4] == ["gh", "api", "--method", "POST"] and args[4].endswith("/git/refs"):
            fields = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-f")
            name = fields["ref"].removeprefix("refs/heads/")
            if name in self.refs and self.refs[name] != fields["sha"]:
                return subprocess.CompletedProcess(args, 1, "", "gh: Reference already exists (HTTP 422)")
            self.refs[name] = fields["sha"]
            self.ref_log.append(("create", name, fields["sha"]))
            if self.crash_after_ref:
                raise SimulatedCrash("the dispatcher died after the branch was created")
            return self._ok(args, "{}")
        if args[:4] == ["gh", "api", "--method", "DELETE"]:
            name = args[4].split("/git/refs/heads/", 1)[1]
            refusal = self.delete_refusals.pop(0) if self.delete_refusals else ""
            if refusal:
                # GitHub refuses the delete with 422 (validation, protection, spam) and the branch stays.
                self.ref_read_fails = refusal == "unreadable"
                self.ref_log.append(("refused", name, ""))
                return subprocess.CompletedProcess(args, 1, "", "gh: Validation Failed (HTTP 422)")
            if name not in self.refs:
                return subprocess.CompletedProcess(args, 1, "", "gh: Reference does not exist (HTTP 422)")
            del self.refs[name]
            self.ref_log.append(("delete", name, ""))
            return self._ok(args, "")
        if args[:4] == ["gh", "api", "--method", "POST"]:
            return self._dispatch(args)
        if args[:2] == ["gh", "api"]:
            path = args[2]
            if match := re.fullmatch(r"repos/[^/]+/[^/]+/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)", path):
                self.compares += 1
                return self._ok(args, json.dumps({"status": self._compare(match.group(1), match.group(2))}))
            if match := re.fullmatch(r"repos/[^/]+/[^/]+/git/ref/heads/(.+)", path):
                if self.ref_read_fails:
                    self.ref_read_fails = False
                    return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 500)")
                if match.group(1) not in self.refs:
                    return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")
                return self._ok(args, json.dumps({"sha": self.refs[match.group(1)]}))
            if match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows/e2e\.yml/runs\?(.*)", path):
                query = dict(part.split("=", 1) for part in match.group(1).split("&"))
                self.run_listings += 1
                listed = [
                    run
                    for run in self.runs.values()
                    if run["head_branch"] == query.get("branch") and run["head_sha"] == query.get("head_sha")
                ]
                return self._ok(args, json.dumps(listed))
            if match := re.fullmatch(r"repos/([^/]+/[^/]+)/actions/runs/(\d+)", path):
                return self._run(args, int(match.group(2)))
            if re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/\d+/jobs\?per_page=100", path):
                return self._ok(args, json.dumps(self.jobs))
        if args[:3] == ["gh", "run", "view"] and "--log-failed" in args:
            return self._ok(args, self.failed_log)
        raise AssertionError(f"unexpected GitHub call {args}")

    def _compare(self, base: str, head: str) -> str:
        if base not in self.history or head not in self.history:
            return "diverged"
        order = self.history.index(head) - self.history.index(base)
        return "identical" if order == 0 else ("ahead" if order > 0 else "behind")

    def _dispatch(self, args: list[str]) -> subprocess.CompletedProcess:
        fields = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-f")
        inputs = {
            key[len("inputs[") : -1]: value for key, value in fields.items() if key.startswith("inputs[")
        }
        self.dispatches.append({"path": args[4], "ref": fields.get("ref"), "inputs": inputs})
        if isinstance(self.dispatch_answer, tuple):
            return subprocess.CompletedProcess(args, 1, "", self.dispatch_answer[1])
        branch = str(fields.get("ref"))
        repo = args[4].split("/actions/", 1)[0].removeprefix("repos/")
        run_id = FIRST_RUN + len(self.runs)
        self.runs[run_id] = {
            "id": run_id,
            "event": "workflow_dispatch",
            "head_branch": branch,
            "display_title": "e2e",
            "name": "e2e",
            "head_sha": self.ran_on or self.refs.get(branch, ""),
            "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
            "status": "queued",
            "created_at": _now(),
        }
        self.ran_on = ""
        if self.dispatch_answer == "crash":
            raise SimulatedCrash("the dispatcher died after GitHub took the dispatch")
        url = f"https://github.com/{repo}/actions/runs/{run_id}"
        return self._ok(args, json.dumps({"workflow_run_id": run_id, "html_url": url}))

    def _run(self, args: list[str], run_id: int) -> subprocess.CompletedProcess:
        status, conclusion = self.run_answer
        run = self.runs.get(run_id, {})
        return self._ok(
            args,
            json.dumps(
                {
                    "head_sha": run.get("head_sha", ""),
                    "status": status,
                    "conclusion": conclusion,
                    "html_url": run.get("html_url", ""),
                    "created_at": _now(),
                    "run_started_at": _now(),
                    "updated_at": _now(),
                }
            ),
        )


class AfterMergeFixture(E2eStageFixture):
    """Cards that merged, the post-merge watch that queues them, and the after-merge pass."""

    def setUp(self) -> None:
        super().setUp()
        self.host = AfterMergeGitHub(self.data_dir / "workspaces", self.catalog)
        self.host.audit = self.writer.audit
        self.runtime = self._runtime()
        self.catalog._adapter = {
            "validation": {"ci": "github", "required_checks": ["test"], "e2e": dict(AFTER_MERGE)}
        }
        self.start_dispatcher()
        self.merged_at = 1_700_000_000.0
        self.number = 9000

    # --- arrangement ------------------------------------------------------------------------------

    def done_card(self, *, sprint: str = SPRINT, project: str = "ummanu", origin: bool = False) -> str:
        """A code card the release already merged: Done. Its number leaves room for the cards the
        dispatcher cuts in between (wait cards, decisions, hotfixes take the next free number)."""
        self.number += 100
        ref = f"{project}-{self.number}"
        metadata: dict[str, Any] = {"task_type": "code", "review": "skipped"}
        if sprint:
            metadata["sprint_ref"] = sprint
        if origin:
            metadata["po_origin"] = origin_field.origin_text("po-session-7", "po-request-7")
        self.board.add_card(self.board.next_key(), ref, project=project, state="done", metadata=metadata)
        return ref

    def on_main(self, *shas: str) -> None:
        self.host.history.extend(shas)

    def merge(
        self, ref: str, sha: str, *, result: str = "green", project: str = "ummanu", repo: str = REPO
    ) -> list[dict[str, Any]]:
        """The post-merge watch of `ref` resolved with `result`, published on the next tick."""
        self.merged_at += 60
        card = self.reader.show(ref)
        with file_lock(self.runtime.production_state.tick_lock):
            payload = self.runtime.production_state.load()
            records = self.runtime.production_state.records(payload)
            watches(payload)[ref] = {
                "version": 1,
                "id": f"w{int(self.merged_at)}",
                "ref": ref,
                "project": project,
                "sprint": str(card.get("sprint") or ""),
                "base": "main",
                "merge_sha": sha,
                "merge_path": "pr",
                "branch": f"pipeline/{ref}",
                "ci": "github",
                "started_at": self.merged_at,
                "absent_reason": "",
                "repo": repo,
                "last_state": "",
                "result": {
                    "result": result,
                    "card": ref,
                    "base": "main",
                    "merge_sha": sha,
                    "waited_seconds": 60,
                },
            }
            outcomes = reconcile_post_merge_watches(self.runtime, payload, records)
            self.runtime.save_records(payload, records)
        return outcomes

    def watch_pass(self) -> list[dict[str, Any]]:
        """One pass of the post-merge watches as they stand, with no new merge."""
        with file_lock(self.runtime.production_state.tick_lock):
            payload = self.runtime.production_state.load()
            records = self.runtime.production_state.records(payload)
            outcomes = reconcile_post_merge_watches(self.runtime, payload, records)
            self.runtime.save_records(payload, records)
        return outcomes

    def watched(self) -> list[str]:
        return sorted(watches(self.runtime.production_state.load()))

    def published(self, ref: str) -> list[str]:
        return self.comments_on(ref, "Post-merge CI GREEN")

    def am_tick(self, runtime: DispatcherRuntime | None = None) -> list[dict[str, Any]]:
        """One after-merge pass, as the production tick makes it after the post-merge watches."""
        runtime = runtime or self.runtime
        with file_lock(runtime.production_state.tick_lock):
            payload = runtime.production_state.load()
            records = runtime.production_state.records(payload)
            outcomes = reconcile_after_merge(runtime, payload, records)
            runtime.production_state.put_records(payload, records)
            payload["last_tick_at"] = now_rfc3339()
            runtime.production_state.save(payload)
        return outcomes

    def queue(self, project: str = "ummanu") -> dict[str, Any]:
        return queues(self.runtime.production_state.load()).get(project) or {}

    def pending(self, project: str = "ummanu") -> list[str]:
        return [str(entry["ref"]) for entry in self.queue(project).get("pending") or []]

    def run_of(self, carrier: str, index: int = -1) -> Any:
        """The carrier's after-merge run: its newest by default."""
        return e2e_state(self.reader.show(carrier)).after_merge_runs[index]

    def conclude(self, conclusion: str, wait_ref: str) -> dict[str, Any]:  # type: ignore[override]
        """The run concludes; its wait card sees it and freezes it."""
        self.host.run_answer = ("completed", conclusion)
        with file_lock(self.runtime.production_state.tick_lock):
            payload = self.runtime.production_state.load()
            records = self.runtime.production_state.records(payload)
            ended = self.runtime._tick_task(self.reader.show(wait_ref), records, payload, new_attempt_id())
            self.runtime.production_state.put_records(payload, records)
            self.runtime.production_state.save(payload)
        self.assertIn(ended["action"], {"wait-target-reached", "wait-ended"}, ended)
        return ended

    def mark(self, ref: str) -> dict[str, Any]:
        return self.reader.show(ref)["e2e"]

    def bells(self) -> list[Any]:
        return [e for e in OwnerEventStore(self.board.credentials).events() if e.kind == "e2e_after_merge"]

    def hotfixes(self) -> list[dict[str, Any]]:
        return [card for card in self.reader.list() if str(card.get("title") or "").startswith("Hotfix:")]

    def comments_on(self, ref: str, needle: str) -> list[str]:
        return [c["body"] for c in self.reader.show(ref)["comments"] if needle in c["body"]]

    def three_merged_in_one_run(self) -> list[str]:
        """Three cards of the sprint merge green in turn; the first pass covers all three."""
        cards = [self.done_card() for n in (1, 2, 3)]
        self.on_main(_sha("a"), _sha("b"), _sha("c"))
        for card, digit in zip(cards, "abc", strict=True):
            self.merge(card, _sha(digit))
        return cards

    @staticmethod
    def later(minutes: float) -> Any:
        return mock.patch.object(
            e2e_after_merge, "utcnow", return_value=datetime.now(UTC) + timedelta(minutes=minutes)
        )


class PlacementTests(E2eStageFixture, unittest.TestCase):
    def test_an_after_merge_project_runs_no_e2e_before_the_merge(self) -> None:
        self.arrange(e2e=AFTER_MERGE)
        self.to_green_review()

        parked = self.tick()

        self.assertNotIn("e2e", str(parked.get("action")), parked)
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.wait_cards(), [])
        self.assertNotIn("e2e", self.card())
        self.assertEqual(self.card()["state"], "assessment", "it parks as a project with no e2e does")


class QueueingTests(AfterMergeFixture, unittest.TestCase):
    def test_a_green_merge_queues_the_card_and_a_red_or_other_project_does_not(self) -> None:
        card = self.done_card()
        red = self.done_card()
        self.on_main(_sha("a"), _sha("b"))

        outcomes = self.merge(card, _sha("a"))
        self.merge(red, _sha("b"), result="red")

        self.assertIn("e2e-after-merge-queued", [o.get("action") for o in outcomes])
        self.assertEqual(self.pending(), [card])
        # A second publish of the same watch queues nothing twice.
        self.merge(card, _sha("a"))
        self.assertEqual(self.pending(), [card])
        # A project whose e2e runs before the merge queues nothing.
        self.catalog._adapter["validation"]["e2e"] = {"workflow": "e2e.yml"}
        other = self.done_card()
        self.merge(other, _sha("b"))
        self.assertEqual(self.pending(), [card])

    def test_an_unreadable_adapter_keeps_the_watch_until_the_card_is_queued(self) -> None:
        card = self.done_card()
        self.on_main(_sha("a"))
        with mock.patch.object(
            self.catalog, "adapter", side_effect=HostError("adapters/ummanu.yaml is unavailable")
        ):
            outcomes = self.merge(card, _sha("a"))
            # Still unreadable on the next pass: kept again, nothing published twice.
            again = self.watch_pass()

        [kept] = [o for o in outcomes if o.get("action") == "e2e-after-merge-not-queued"]
        self.assertEqual(kept["status"], "degraded")
        self.assertIn("adapters/ummanu.yaml is unavailable", kept["reason"])
        self.assertIn("e2e-after-merge-not-queued", [o.get("action") for o in again])
        self.assertEqual(self.watched(), [card])
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.published(card)), 1, "the green fact is published once")

        # The adapter is back: the next pass queues the card once and drops the watch.
        back = self.watch_pass()

        self.assertIn("e2e-after-merge-queued", [o.get("action") for o in back])
        self.assertEqual(self.pending(), [card])
        self.assertEqual(self.watched(), [])
        self.assertEqual(len(self.published(card)), 1)
        self.watch_pass()
        self.assertEqual(self.pending(), [card])

    def test_a_malformed_e2e_declaration_keeps_the_watch(self) -> None:
        card = self.done_card()
        self.on_main(_sha("a"))
        self.catalog._adapter["validation"]["e2e"] = {**AFTER_MERGE, "placement": "sideways"}

        outcomes = self.merge(card, _sha("a"))

        [kept] = [o for o in outcomes if o.get("action") == "e2e-after-merge-not-queued"]
        self.assertIn("placement 'sideways'", kept["reason"])
        self.assertEqual(self.watched(), [card])
        self.assertEqual(self.pending(), [])
        # Repaired, the card is queued and the watch goes.
        self.catalog._adapter["validation"]["e2e"] = dict(AFTER_MERGE)
        self.watch_pass()
        self.assertEqual((self.pending(), self.watched()), ([card], []))

    def test_a_replayed_enqueue_after_a_partial_save_queues_once(self) -> None:
        card = self.done_card()
        self.on_main(_sha("a"))
        save = self.runtime.save_records

        def lost_after_the_queue(payload: dict[str, Any], records: dict[str, Any]) -> None:
            # The dispatcher dies at the save that would drop the watch: the queued card is on disk.
            if card not in watches(payload):
                raise OSError("the dispatcher died before the watch drop was saved")
            save(payload, records)

        with (
            mock.patch.object(self.runtime, "save_records", side_effect=lost_after_the_queue),
            self.assertRaises(OSError),
        ):
            self.merge(card, _sha("a"))
        self.assertEqual((self.pending(), self.watched()), ([card], [card]))

        self.watch_pass()

        self.assertEqual((self.pending(), self.watched()), ([card], []))
        # Once the card has moved on (covered, then green), a lost watch drop replayed still queues nothing.
        self.am_tick()
        run = self.run_of(card)
        self.conclude("success", run.wait_ref)
        self.am_tick()
        self.merge(card, _sha("a"))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.watched(), [])

    def test_three_merges_during_a_run_are_covered_by_exactly_one_next_run(self) -> None:
        first = self.done_card()
        self.on_main(_sha("a"))
        self.merge(first, _sha("a"))
        [started] = [o for o in self.am_tick() if o["action"] == "e2e-after-merge-waiting"]
        self.assertEqual(started["covered"], [first])
        run = self.run_of(first)

        later = [self.done_card() for n in (2, 3, 4)]
        self.on_main(_sha("b"), _sha("c"), _sha("d"))
        for card, digit in zip(later, "bcd", strict=True):
            self.merge(card, _sha(digit))
        # One run in flight: they wait, and nothing else is dispatched.
        waiting = self.am_tick()
        self.assertEqual([o["action"] for o in waiting], ["e2e-after-merge-waiting"])
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(self.pending(), later)
        self.assertEqual(self.mark(later[0])["state"], "pending")

        self.conclude("success", run.wait_ref)
        self.am_tick()

        self.assertEqual(len(self.host.dispatches), 2, "exactly one next run")
        carrier = later[-1]
        second = self.run_of(carrier)
        self.assertEqual(second.sha, _sha("d"))
        self.assertEqual([item["ref"] for item in second.covered], later)
        self.assertEqual([item["merge_sha"] for item in second.covered], [_sha("b"), _sha("c"), _sha("d")])
        self.assertEqual(self.pending(), [])
        for card in later:
            self.assertEqual(self.mark(card)["state"], f"covered by {second.run_url}")
            self.assertEqual(self.mark(card)["carrier"], carrier)

    def test_a_card_whose_post_merge_ci_is_not_green_yet_is_not_covered(self) -> None:
        old, newer = self.done_card(), self.done_card()
        self.on_main(_sha("a"), _sha("b"))
        # The newer merge's CI is green first; the older one's watch has no result yet.
        self.merge(newer, _sha("b"))

        self.am_tick()

        run = self.run_of(newer)
        self.assertEqual([item["ref"] for item in run.covered], [newer])
        self.assertNotIn("e2e", self.reader.show(old))
        # Once green it joins the pending set for the next run, although it is an ancestor of the last.
        self.merge(old, _sha("a"))
        self.assertEqual(self.pending(), [old])

    def test_a_merge_off_the_target_s_line_stays_pending(self) -> None:
        on, off = self.done_card(), self.done_card()
        self.on_main(_sha("b"))
        self.merge(off, _sha("a"))  # not on main's line: `diverged` against the target
        self.merge(on, _sha("b"))

        self.am_tick()

        self.assertEqual([item["ref"] for item in self.run_of(on).covered], [on])
        self.assertEqual(self.pending(), [off])

    def test_one_project_in_flight_does_not_block_another(self) -> None:
        mine = self.done_card()
        theirs = self.done_card(project="codegen", sprint="")
        self.on_main(_sha("a"), _sha("e"))
        self.merge(mine, _sha("a"))
        self.am_tick()
        self.assertEqual(len(self.host.dispatches), 1)

        self.merge(theirs, _sha("e"), project="codegen", repo=OTHER_REPO)
        outcomes = self.am_tick()

        self.assertEqual(len(self.host.dispatches), 2)
        by_project = {o["project"]: o["action"] for o in outcomes}
        self.assertEqual(
            by_project, {"ummanu": "e2e-after-merge-waiting", "codegen": "e2e-after-merge-waiting"}
        )
        self.assertEqual(
            self.host.dispatches[1]["path"], f"repos/{OTHER_REPO}/actions/workflows/e2e.yml/dispatches"
        )
        self.assertEqual(self.run_of(theirs).charged_to, "cards")


class ExactShaTests(AfterMergeFixture, unittest.TestCase):
    def test_the_run_is_dispatched_on_a_dispatcher_owned_branch_at_the_target_and_the_branch_is_deleted(
        self,
    ) -> None:
        cards = self.three_merged_in_one_run()
        # `main` moved past the target before the dispatch: the branch still points at the target.
        self.on_main(_sha("f"))

        self.am_tick()

        run = self.run_of(cards[-1])
        self.assertTrue(run.git_ref.startswith("pipeline-e2e/"), run.git_ref)
        self.assertIn(run.dispatch_id, run.git_ref)
        [dispatch] = self.host.dispatches
        self.assertEqual(dispatch["ref"], run.git_ref)
        self.assertEqual(dispatch["inputs"], {"suite": "mega-noop", "sha": _sha("c")})
        self.assertEqual(self.host.ref_log, [("create", run.git_ref, _sha("c"))])
        self.assertEqual((run.head_sha, run.git_ref_state), (_sha("c"), "created"))

        self.conclude("success", run.wait_ref)
        self.assertIn(run.git_ref, self.host.refs, "the branch lives while the run does")
        self.am_tick()

        self.assertNotIn(run.git_ref, self.host.refs)
        self.assertEqual(self.host.ref_log[-1], ("delete", run.git_ref, ""))
        self.assertEqual(self.run_of(cards[-1]).git_ref_state, "deleted")
        self.assertEqual(self.queue(), {}, "nothing left for the project")

    def concluded_with_refused_delete(self, *refusals: str) -> tuple[list[str], Any]:
        cards = self.three_merged_in_one_run()
        self.am_tick()
        run = self.run_of(cards[-1])
        self.conclude("success", run.wait_ref)
        self.host.delete_refusals = list(refusals)
        return cards, run

    def test_a_422_on_a_branch_already_gone_is_confirmed_absent_and_dropped(self) -> None:
        cards, run = self.concluded_with_refused_delete()
        del self.host.refs[run.git_ref]  # somebody deleted it first: GitHub answers the DELETE 422

        outcomes = self.am_tick()

        self.assertNotIn("e2e-after-merge-ref-cleanup", [o["action"] for o in outcomes])
        self.assertEqual(self.queue(), {})
        self.assertEqual(self.run_of(cards[-1]).git_ref_state, "deleted")

    def test_a_422_while_the_branch_still_exists_stays_recorded_and_is_deleted_later(self) -> None:
        cards, run = self.concluded_with_refused_delete("refused")

        outcomes = self.am_tick()

        [kept] = [o for o in outcomes if o["action"] == "e2e-after-merge-ref-cleanup"]
        self.assertEqual(kept["status"], "degraded")
        self.assertIn("still exists", kept["reason"])
        self.assertIn(run.git_ref, self.host.refs)
        self.assertEqual([item["ref"] for item in self.queue()["cleanup"]], [run.git_ref])
        self.assertEqual(self.run_of(cards[-1]).git_ref_state, "created")

        self.am_tick()

        self.assertNotIn(run.git_ref, self.host.refs)
        self.assertEqual(self.host.ref_log[-1], ("delete", run.git_ref, ""))
        self.assertEqual(self.queue(), {})
        self.assertEqual(self.run_of(cards[-1]).git_ref_state, "deleted")

    def test_a_422_whose_confirming_read_fails_stays_recorded(self) -> None:
        cards, run = self.concluded_with_refused_delete("unreadable")

        outcomes = self.am_tick()

        [kept] = [o for o in outcomes if o["action"] == "e2e-after-merge-ref-cleanup"]
        self.assertIn(run.git_ref, kept["reason"])
        self.assertEqual([item["ref"] for item in self.queue()["cleanup"]], [run.git_ref])
        self.assertEqual(self.run_of(cards[-1]).git_ref_state, "created")
        self.am_tick()
        self.assertEqual(self.queue(), {})

    def test_a_run_on_another_sha_is_blocked_and_never_attached(self) -> None:
        cards = self.three_merged_in_one_run()
        self.host.ran_on = _sha("9")

        outcomes = self.am_tick()

        self.assertIn("e2e-after-merge-blocked", [o["action"] for o in outcomes])
        run = self.run_of(cards[-1])
        self.assertEqual(run.resolution, "blocked")
        self.assertIn("not attached", run.closing_reason)
        self.assertEqual(run.wait_ref, "", "no wait card is created for it")
        self.assertEqual(self.comments_on(cards[0], "E2E after merge — green"), [])
        self.assertEqual(self.hotfixes(), [])
        self.assertEqual(len(self.bells()), 1)
        self.assertEqual(sorted(self.pending()), sorted(cards))
        self.assertEqual(self.mark(cards[0])["state"], "pending")
        self.assertNotIn(run.git_ref, self.host.refs)

    def test_a_crash_between_the_branch_and_the_dispatch_never_dispatches(self) -> None:
        cards = self.three_merged_in_one_run()
        self.host.crash_after_ref = True

        with self.assertRaises(SimulatedCrash):
            self.am_tick()

        self.host.crash_after_ref = False
        restarted = self._runtime()
        run = self.run_of(cards[-1])
        self.assertEqual((run.dispatch, run.git_ref_state), ("intent", ""))
        # Within the settle window nothing is looked up; after it, nothing is found; nothing is dispatched.
        self.assertEqual(self.am_tick(restarted)[0]["action"], "e2e-after-merge-identifying")
        with self.later(7):
            self.assertEqual(self.am_tick(restarted)[0]["action"], "e2e-after-merge-identifying")
        self.assertEqual(self.host.dispatches, [])
        with self.later(16):
            ended = self.am_tick(restarted)
        self.assertIn("e2e-after-merge-blocked", [o["action"] for o in ended])
        crashed = self.run_of(cards[-1], 0)
        self.assertIn("could not be identified", crashed.closing_reason)
        self.assertEqual(
            [d for d in self.host.dispatches if d["ref"] == run.git_ref],
            [],
            "the crashed intent is never dispatched",
        )
        # The recorded branch is cleaned up, and the cards go back for the next run, charged anew.
        self.assertNotIn(run.git_ref, self.host.refs)
        self.assertEqual(crashed.git_ref_state, "deleted")
        self.assertEqual(len(self.bells()), 1)
        [(_, next_ref, _)] = [
            entry for entry in self.host.ref_log if entry[0] == "create" and entry[1] != run.git_ref
        ]
        self.assertEqual([d["ref"] for d in self.host.dispatches], [next_ref])
        self.assertEqual([item["ref"] for item in self.run_of(cards[-1]).covered], cards)

    def test_a_crash_after_the_dispatch_is_recovered_without_a_second_dispatch(self) -> None:
        cards = self.three_merged_in_one_run()
        self.host.dispatch_answer = "crash"

        with self.assertRaises(SimulatedCrash):
            self.am_tick()

        self.host.dispatch_answer = "ok"
        with self.later(6):
            outcomes = self.am_tick(self._runtime())
        self.assertEqual([o["action"] for o in outcomes], ["e2e-after-merge-waiting"])
        run = self.run_of(cards[-1])
        self.assertEqual((run.identified_by, run.head_sha), ("recovery", _sha("c")))
        self.assertEqual(len(self.host.dispatches), 1)


class BudgetTests(AfterMergeFixture, unittest.TestCase):
    def standing_refusal(self) -> None:
        SprintWriter(self.board, data_dir=self.data_dir).record_owner_decisions(
            role="po", actor="po", reference=SPRINT, request_id="no-more-after-merge",
            entries=[{"id": "stop-am", "scope": "sprint", "kind": "e2e_refusal", "value": "no_more_e2e",
                      "quotation": "No more e2e in this sprint. Preserve paid runs."}],
        )

    def assert_standing_declined(self, cards) -> None:
        for card in cards:
            self.assertEqual(self.mark(card)["state"], "declined")
            self.assertEqual(self.mark(card)["decision"], f"{SPRINT}/stop-am")
            self.assertTrue(self.comments_on(card, "No more e2e in this sprint."))
        self.assertEqual([event for event in OwnerEventStore(self.board.credentials).events() if event.event_class == "needs_owner"], [])

    def test_standing_refusal_with_remaining_budget_and_later_card_dispatches_nothing(self) -> None:
        self.standing_refusal()
        cards = self.three_merged_in_one_run()
        self.am_tick()
        self.assert_standing_declined(cards)
        late = self.done_card()
        self.on_main(_sha("d"))
        self.merge(late, _sha("d"))
        self.am_tick()
        self.assert_standing_declined([late])
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.sprint_budget()["used"], 0)
        self.assertEqual([card for card in self.reader.list() if card.get("type") == "decision"], [])

    def test_standing_refusal_at_spent_budget_creates_no_decision(self) -> None:
        self.spend_sprint(3)
        self.standing_refusal()
        cards = self.three_merged_in_one_run()
        self.am_tick()
        self.assert_standing_declined(cards)
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.sprint_budget()["used"], 3)
        self.assertEqual([card for card in self.reader.list() if card.get("type") == "decision"], [])

    def test_pending_after_merge_budget_wait_consumes_refusal_without_joining_late_card(self) -> None:
        self.spend_sprint(3)
        cards = self.three_merged_in_one_run()
        self.assertEqual(self.am_tick()[0]["action"], "e2e-after-merge-budget-waiting")
        [decision] = [card for card in self.reader.list() if card.get("type") == "decision"]
        self.standing_refusal()
        late = self.done_card()
        self.on_main(_sha("d"))
        self.merge(late, _sha("d"))
        self.am_tick()
        self.assert_standing_declined([*cards, late])
        self.assertEqual(self.queue()["budget_waits"], [])
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual([card["ref"] for card in self.reader.list() if card.get("type") == "decision"], [decision["ref"]])
        self.assertEqual(self.comments_on(decision["ref"], f"{late} ("), [])

    def sprint_budget(self) -> dict[str, Any]:
        return self.reader.sprint_e2e_budget(SPRINT) or {}

    def spend_sprint(self, runs: int) -> None:
        for n in range(runs):
            self.reader.client.call(
                "chargeSprintE2e",
                sprint_ref=SPRINT,
                task_ref="ummanu-511",
                dispatch_id=f"d-{n}",
                at=_now(),
            )

    def test_the_run_is_charged_once_to_the_newest_covered_card_s_open_sprint(self) -> None:
        cards = self.three_merged_in_one_run()

        self.am_tick()

        run = self.run_of(cards[-1])
        self.assertEqual(run.charged_to, SPRINT)
        budget = self.sprint_budget()
        self.assertEqual(budget["used"], 1)
        self.assertEqual(
            [(c["card"], c["dispatch_id"]) for c in budget["charges"]], [(cards[-1], run.dispatch_id)]
        )
        for card in cards:
            self.assertEqual(
                e2e_state(self.reader.show(card)).after_merge.charged, [], "no card cap is charged"
            )
        # `sprint status` counts it, and says it was an after-merge run.
        shown = SprintReader(self.board, data_dir=self.data_dir).show(SPRINT)  # type: ignore[arg-type]
        self.assertEqual(shown["e2e"]["after_merge"], 1)
        self.assertEqual(shown["e2e"]["summary"], "e2e: 1 of 3 (1 after merge)")

    def test_outside_a_sprint_every_covered_card_s_cap_is_charged_together(self) -> None:
        cards = [self.done_card(sprint="") for n in (1, 2)]
        self.on_main(_sha("a"), _sha("b"))
        for card, digit in zip(cards, "ab", strict=True):
            self.merge(card, _sha(digit))

        self.am_tick()

        run = self.run_of(cards[-1])
        self.assertEqual(run.charged_to, "cards")
        for card in cards:
            self.assertEqual(e2e_state(self.reader.show(card)).after_merge.charged, [run.dispatch_id])
            self.assertEqual(self.mark(card)["runs_dispatched"], 1)

    def older_capped_newer_not(self) -> tuple[str, str, dict[str, Any]]:
        """The reviewer's batch: outside every sprint, the older card (PO origin) at its cap, the newer one
        not; one pass charges none, dispatches nothing and cuts one decision for the batch."""
        capped = self.done_card(sprint="", origin=True)
        fresh = self.done_card(sprint="")
        self.on_main(_sha("a"), _sha("b"))
        self.merge(capped, _sha("a"))
        self.merge(fresh, _sha("b"))
        state = e2e_state(self.reader.show(capped))
        # Its own cap was spent by three earlier after-merge runs no sprint paid for.
        state.after_merge = AfterMergeMark(merge_sha=_sha("a"), charged=["x-1", "x-2", "x-3"])
        self.writer.record_e2e_state(
            role="dispatcher", actor="ummanu-pilot", reference=capped, state=state.text()
        )

        [waiting] = self.am_tick()

        self.assertEqual(waiting["action"], "e2e-after-merge-budget-waiting", waiting)
        [decision] = [card for card in self.reader.list() if card.get("type") == "decision"]
        self.assertEqual(waiting["decisions"], [decision["ref"]])
        return capped, fresh, decision

    def hand_to_owner(self, decision: str, answer: str) -> str:
        self.board.move(self.board.key_of(decision), "in_progress")
        self.writer.handover(
            role="po",
            actor="po",
            reference=decision,
            to="owner",
            reason="money",
            request_id=f"handover-{decision}",
        )
        return str(
            self.writer.comment(
                role="owner", actor="owner", reference=decision, body=answer, request_id="owner"
            )["event_id"]
        )

    def test_outside_a_sprint_one_spent_cap_charges_none_and_one_decision_owns_the_batch(self) -> None:
        capped, fresh, decision = self.older_capped_newer_not()

        self.assertEqual((self.host.dispatches, self.host.ref_log), ([], []))
        self.assertEqual(e2e_state(self.reader.show(fresh)).after_merge.charged, [], "none charged")
        self.assertEqual(e2e_state(self.reader.show(capped)).after_merge.charged, ["x-1", "x-2", "x-3"])
        self.assertEqual(
            origin_field.po_origin(decision), {"session": "po-session-7", "request": "po-request-7"}
        )
        self.assertFalse(decision.get("sprint"))
        # Both are named, each with its cap: the capped one needs the raise.
        body = decision["description"]
        [capped_line] = [line for line in body.splitlines() if line.startswith(f"- {capped} waits")]
        [fresh_line] = [line for line in body.splitlines() if line.startswith(f"- {fresh} waits")]
        self.assertIn("cap spent, 3 of 3 runs: needs a raise", capped_line)
        self.assertIn("cap 0 of 3 runs, not spent", fresh_line)
        self.assertIn(f"task e2e-budget --ref {capped} --role po", body)
        self.assertNotIn(f"task e2e-budget --ref {fresh} ", body)
        for card in (capped, fresh):
            self.assertEqual(self.mark(card)["mark"], f"e2e: budget spent, waiting on {decision['ref']}")
        self.assertEqual(sorted(self.pending()), sorted([capped, fresh]))
        # Re-checked each tick; nothing is dispatched or cut again while the decision is open.
        self.assertEqual(self.am_tick()[0]["action"], "e2e-after-merge-budget-waiting")
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(len([c for c in self.reader.list() if c.get("type") == "decision"]), 1)
        # A card queued while it is open joins it and is listed there.
        late = self.done_card(sprint="")
        self.on_main(_sha("c"))
        self.merge(late, _sha("c"))
        self.am_tick()
        self.am_tick()
        [joined] = self.comments_on(decision["ref"], f"- {late} waits")
        self.assertIn("joins the batch", joined)
        self.assertEqual(self.mark(late)["mark"], f"e2e: budget spent, waiting on {decision['ref']}")
        self.assertEqual(self.host.dispatches, [])

    def test_a_raise_of_the_spent_cap_runs_the_whole_batch_once(self) -> None:
        capped, fresh, decision = self.older_capped_newer_not()
        answer = self.hand_to_owner(decision["ref"], "e2e budget: raise 1")

        self.writer.raise_e2e_cap(role="po", actor="po", reference=capped, authorized_by=answer)
        # The batch decision authorizes only the cards it names spent.
        with self.assertRaises(TaskError) as refused:
            self.writer.raise_e2e_cap(role="po", actor="po", reference=fresh, authorized_by=answer)
        self.assertEqual(refused.exception.code, "authorization_refused")
        self.am_tick()

        self.assertEqual(len(self.host.dispatches), 1)
        run = self.run_of(fresh)
        self.assertEqual([item["ref"] for item in run.covered], [capped, fresh])
        self.assertEqual(run.charged_to, "cards")
        self.assertEqual(
            e2e_state(self.reader.show(capped)).after_merge.charged, ["x-1", "x-2", "x-3", run.dispatch_id]
        )
        self.assertEqual(e2e_state(self.reader.show(fresh)).after_merge.charged, [run.dispatch_id])
        self.assertEqual(self.pending(), [])

    def test_a_decision_with_no_raise_declines_the_whole_batch(self) -> None:
        capped, fresh, decision = self.older_capped_newer_not()
        self.hand_to_owner(decision["ref"], "e2e budget: no")
        self.writer.complete(
            role="po",
            actor="po",
            reference=decision["ref"],
            kind="decision",
            body="## Decision\n\nThe owner said no.\n\n## How to verify\n\n`ummanu task show`\n",
            request_id="po-complete",
        )

        self.am_tick()

        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.pending(), [])
        for card in (capped, fresh):
            view = self.mark(card)
            self.assertEqual((view["state"], view["decision"]), ("declined", decision["ref"]))
            [comment] = self.comments_on(card, "## E2E after merge — declined")
            self.assertIn(f"the decision {decision['ref']} was completed without a raise", comment)
        self.assertEqual(self.queue(), {})

    def test_a_batch_no_po_session_owns_is_declined_whole_with_the_bell(self) -> None:
        capped, fresh = self.done_card(sprint=""), self.done_card(sprint="")
        self.on_main(_sha("a"), _sha("b"))
        self.merge(capped, _sha("a"))
        self.merge(fresh, _sha("b"))
        state = e2e_state(self.reader.show(capped))
        state.after_merge = AfterMergeMark(merge_sha=_sha("a"), charged=["x-1", "x-2", "x-3"])
        self.writer.record_e2e_state(
            role="dispatcher", actor="ummanu-pilot", reference=capped, state=state.text()
        )

        [declined] = self.am_tick()

        self.assertEqual(declined["action"], "e2e-after-merge-declined", declined)
        self.assertEqual([c for c in self.reader.list() if c.get("type") == "decision"], [])
        for card in (capped, fresh):
            self.assertEqual(self.mark(card)["state"], "declined")
        [bell] = [e for e in OwnerEventStore(self.board.credentials).events() if e.kind == "e2e_budget_spent"]
        self.assertEqual(bell.subject_ref, capped)
        self.assertEqual(self.pending(), [])

    def test_a_spent_sprint_budget_dispatches_nothing_and_names_every_covered_card(self) -> None:
        self.spend_sprint(3)
        cards = self.three_merged_in_one_run()

        [waiting] = self.am_tick()

        self.assertEqual(waiting["action"], "e2e-after-merge-budget-waiting", waiting)
        self.assertEqual((self.host.dispatches, self.host.ref_log), ([], []))
        self.assertEqual(self.sprint_budget()["used"], 3)
        [decision] = [card for card in self.reader.list() if card.get("type") == "decision"]
        self.assertEqual(decision["sprint"], SPRINT)
        for card in cards:
            self.assertIn(f"- {card} waits", decision["description"])
            self.assertEqual(self.mark(card)["mark"], f"e2e: budget spent, waiting on {decision['ref']}")
            self.assertNotIn("covered_by", {k for k, v in self.mark(card).items() if v})
        self.assertIn("The e2e run budget of sprint:1031 is spent: 3 of 3 runs.", decision["description"])
        # A card queued while the decision is open joins it with one comment.
        late = self.done_card()
        self.on_main(_sha("d"))
        self.merge(late, _sha("d"))
        self.am_tick()
        self.am_tick()
        self.assertEqual(len(self.comments_on(decision["ref"], f"{late} (")), 1)
        self.assertEqual(self.mark(late)["mark"], f"e2e: budget spent, waiting on {decision['ref']}")
        # A raise lets the next pass dispatch one run covering all four.
        with self.board.transaction():
            self.board._execute("UPDATE sprints SET e2e_budget = 4 WHERE ref = %s", (SPRINT,))
        self.am_tick()
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(len(self.run_of(late).covered), 4)


class OutcomeTests(AfterMergeFixture, unittest.TestCase):
    def run_to(self, conclusion: str, **arrange: Any) -> tuple[list[str], Any]:
        cards = self.three_merged_in_one_run() if not arrange else arrange["cards"]()
        self.am_tick()
        run = self.run_of(cards[-1])
        self.host.jobs = [
            {"name": "mega", "conclusion": "failure", "html_url": run.run_url, "steps": ["Run e2e suite"]}
        ]
        self.conclude(conclusion, run.wait_ref)
        self.am_tick()
        return cards, self.run_of(cards[-1], 0)

    def test_green_comments_on_every_covered_card(self) -> None:
        cards, run = self.run_to("success")

        for card in cards:
            [comment] = self.comments_on(card, "## E2E after merge — green")
            self.assertIn(run.run_url, comment)
            self.assertIn(_sha("c"), comment)
            for other in cards:
                self.assertIn(other, comment)
            self.assertEqual(self.mark(card)["state"], "green")
            self.assertEqual(self.mark(card)["placement"], "after_merge")
            self.assertEqual(self.mark(card)["run"], run.run_url)
        self.assertEqual((self.hotfixes(), self.bells()), ([], []))
        # A replayed pass comments nothing twice.
        self.am_tick()
        self.assertEqual(len(self.comments_on(cards[0], "## E2E after merge — green")), 1)

    def assertHotfixEvidence(self, hotfix: dict[str, Any], run: Any, cards: list[str]) -> None:
        self.assertEqual(hotfix["type"], "code")
        body = hotfix["description"]
        for needle in (run.run_url, "conclusion: failure", "job «mega»", '"Run e2e suite"', _sha("c")):
            self.assertIn(needle, body)
        self.assertIn("stand 2 never answered its health check", body, "the bounded --log-failed fragment")
        for card, digit in zip(cards, "abc", strict=True):
            self.assertIn(f"- {card}: merged as `{_sha(digit)}`", body)
        for card in cards:
            self.assertEqual(self.mark(card)["state"], f"red -> {hotfix['ref']}")
            self.assertEqual(len(self.comments_on(card, "## E2E after merge — red")), 1)

    def test_a_failure_in_an_open_sprint_cuts_one_hotfix_there_and_wakes_the_observer(self) -> None:
        cards, run = self.run_to("failure")

        [hotfix] = self.hotfixes()
        self.assertHotfixEvidence(hotfix, run, cards)
        self.assertEqual((hotfix["sprint"], hotfix["state"]), (SPRINT, "ready"))
        [created] = [e for e in self.writer.audit.events(hotfix["ref"]) if e.get("kind") == "created"]
        self.assertEqual(created["payload"]["budget_event"], "hotfix")
        self.assertTrue(is_significant_card_event(created, linked_refs={hotfix["ref"]}))
        self.assertEqual(self.bells(), [])
        self.assertEqual(self.pending(), [], "a red run does not requeue its cards")

    def test_a_failure_outside_a_sprint_goes_to_the_newest_card_s_po_origin(self) -> None:
        def cards() -> list[str]:
            made = [self.done_card(sprint=""), self.done_card(sprint="", origin=True)]
            self.on_main(_sha("a"), _sha("b"), _sha("c"))
            for card, digit in zip(made, "ab", strict=True):
                self.merge(card, _sha(digit))
            return made

        _made, run = self.run_to("failure", cards=cards)

        [hotfix] = self.hotfixes()
        self.assertEqual((hotfix["sprint"], hotfix["state"]), (None, "ready"))
        self.assertEqual(
            origin_field.po_origin(hotfix), {"session": "po-session-7", "request": "po-request-7"}
        )
        self.assertIn(run.run_url, hotfix["description"])
        self.assertEqual(self.bells(), [])

    def test_a_failure_nobody_owns_is_blocked_at_once_and_rings_the_bell(self) -> None:
        def cards() -> list[str]:
            made = [self.done_card(sprint="")]
            self.on_main(_sha("a"))
            self.merge(made[0], _sha("a"))
            return made

        self.run_to("failure", cards=cards)

        [hotfix] = self.hotfixes()
        self.assertEqual((hotfix["sprint"], hotfix["state"]), (None, "blocked"))
        self.assertIsNone(origin_field.po_origin(hotfix))
        blocked = [
            e
            for e in self.writer.audit.events(hotfix["ref"])
            if (e.get("transition") or {}).get("target") == "blocked"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertIn("after-merge e2e red, no sprint or origin owns it", blocked[0]["reason"])
        [bell] = self.bells()
        self.assertEqual((bell.event_class, bell.subject_ref), ("needs_owner", hotfix["ref"]))

    def test_a_replayed_red_outcome_cuts_no_second_hotfix(self) -> None:
        cards, run = self.run_to("failure")
        [hotfix] = self.hotfixes()
        # A crash after the hotfix create, before the run recorded it: the next pass acts again.
        state = e2e_state(self.reader.show(cards[-1]))
        [recorded] = state.after_merge_runs
        recorded.hotfix, recorded.acted = "", False
        self.writer.record_e2e_state(
            role="dispatcher", actor="ummanu-pilot", reference=cards[-1], state=state.text()
        )
        with file_lock(self.runtime.production_state.tick_lock):
            payload = self.runtime.production_state.load()
            queues(payload)["ummanu"] = {
                "pending": [],
                "run": {"carrier": cards[-1], "dispatch_id": run.dispatch_id, "sha": run.sha, "entries": []},
                "budget_waits": [],
                "cleanup": [],
            }
            self.runtime.production_state.save(payload)

        self.am_tick()

        self.assertEqual([card["ref"] for card in self.hotfixes()], [hotfix["ref"]])
        self.assertEqual(self.run_of(cards[-1]).hotfix, hotfix["ref"])
        self.assertEqual(len(self.comments_on(cards[0], "## E2E after merge — red")), 1)

    def test_cancelled_or_timed_out_cuts_no_hotfix_rings_once_and_requeues(self) -> None:
        for conclusion in ("cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                self.tearDown()
                self.setUp()
                cards, run = self.run_to(conclusion)

                self.assertEqual(run.resolution, "requeued")
                self.assertEqual(self.hotfixes(), [])
                [bell] = self.bells()
                self.assertEqual((bell.event_class, bell.subject_ref), ("needs_owner", cards[-1]))
                self.assertIn(conclusion, bell.text)
                for card in cards:
                    [comment] = self.comments_on(card, "## E2E after merge — requeued")
                    self.assertIn(f"concluded {conclusion}", comment)
                # Pending again, so the next run covers them again, and is charged as usual.
                self.assertEqual(len(self.host.dispatches), 2)
                again = self.run_of(cards[-1])
                self.assertNotEqual(again.dispatch_id, run.dispatch_id)
                self.assertEqual([item["ref"] for item in again.covered], cards)
                self.assertEqual(self.mark(cards[0])["state"], f"covered by {again.run_url}")
                self.assertEqual(self.reader.sprint_e2e_budget(SPRINT)["used"], 2)

    def test_a_passed_deadline_is_handled_like_any_other_non_verdict(self) -> None:
        cards = self.three_merged_in_one_run()
        self.am_tick()
        run = self.run_of(cards[-1])
        with mock.patch(
            "ummanu.dispatch.wait_cards.utcnow", return_value=datetime.now(UTC) + timedelta(hours=3)
        ):
            self.host.run_answer = ("in_progress", None)
            with file_lock(self.runtime.production_state.tick_lock):
                payload = self.runtime.production_state.load()
                records = self.runtime.production_state.records(payload)
                self.runtime._tick_task(self.reader.show(run.wait_ref), records, payload, new_attempt_id())
                self.runtime.production_state.put_records(payload, records)
                self.runtime.production_state.save(payload)

        self.am_tick()

        acted = self.run_of(cards[-1], 0)
        self.assertEqual(
            (acted.resolution, (acted.result or {}).get("outcome")), ("requeued", "deadline_passed")
        )
        self.assertEqual(self.hotfixes(), [])
        self.assertEqual(len(self.bells()), 1)
        self.assertIn("deadline_passed", self.comments_on(cards[0], "## E2E after merge — requeued")[0])


class VisibilityTests(AfterMergeFixture, unittest.TestCase):
    def test_task_show_says_pending_then_covered_then_the_result(self) -> None:
        card = self.done_card()
        self.on_main(_sha("a"))
        self.merge(card, _sha("a"))
        # A pass that only marks the queued card, as one whose start is deferred does.
        with mock.patch.object(e2e_after_merge, "_start", return_value=None):
            self.am_tick()
        view = self.mark(card)
        self.assertEqual(
            (view["placement"], view["state"], view["merge_sha"]), ("after_merge", "pending", _sha("a"))
        )

        self.am_tick()
        run = self.run_of(card)
        view = self.mark(card)
        self.assertEqual(view["state"], f"covered by {run.run_url}")
        self.assertEqual(view["run"], run.run_url)
        [shown] = view["after_merge_runs"]
        self.assertEqual(
            (shown["ref"], shown["covered"]), (run.git_ref, [{"ref": card, "merge_sha": _sha("a")}])
        )


if __name__ == "__main__":
    unittest.main()
