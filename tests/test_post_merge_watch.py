"""The post-merge CI watch: a release wakes the observer on the base's CI result, not on its merge.

Unit level, no Docker and no network: `gh` is a scripted host, the board is a recording writer
whose request ids are idempotent the way the audit's are, and the observer significance predicate
(`ummanu.tasks.is_significant_observer_event`) is the judge of what wakes the observer.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests import source_trees
from tests.fakes.marker_audit import MarkerAudit
from tests.fakes.tick_board import CountingStore
from tests.observer_identity import as_observer, bind_observer
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.board import sprint_dod
from ummanu.board.backend import record_key
from ummanu.board.budget_candidates import is_candidate
from ummanu.board.models import Actor, CardState, EntityKind, Event, EventKind
from ummanu.board.observer_routing import AUTOMATIC_ASSESSMENT_REASON
from ummanu.board.origin_outbox import owed_target
from ummanu.board.transitions import transition_for
from ummanu.dispatch import post_merge, release_lifecycle
from ummanu.dispatch.gate_receipt import mint_gate_receipt
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.observer import render_observer_wake_context
from ummanu.dispatch.origin_returns import render_origin_input
from ummanu.dispatch.state import DispatcherRecord, PersistedGateReceipt
from ummanu.dispatch.types import HostError, MergeLanding
from ummanu.tasks import (
    RELEASE_MERGE_KEY,
    TaskError,
    TaskWriter,
    is_merged_release,
    is_significant_card_event,
    is_significant_observer_event,
    recorded_card_transition,
)

REF = "ummanu-9001"
SPRINT = "sprint:77"
SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = "example-org/sample"
BRANCH = "pipeline/ummanu-9001"


def _done(*, actor: str = "dispatcher", release_merge: dict | None = None, source=CardState.ASSESSMENT) -> dict:
    declaration = transition_for(EntityKind.CARD, source, CardState.DONE)
    data = {RELEASE_MERGE_KEY: release_merge} if release_merge is not None else {}
    return Event(
        f"board-event-done-{actor}-{bool(release_merge)}",
        declaration.event_kind,
        EntityKind.CARD,
        REF,
        Actor(actor, actor),
        "Observer decision: accept.",
        datetime(2026, 9, 25, tzinfo=UTC),
        source_state=source.value,
        target_state=CardState.DONE.value,
        data=data,
    ).to_record(f"request-done-{actor}-{bool(release_merge)}")


def _comment(*, role: str = "dispatcher", ref: str = REF, fact: dict | None = None) -> dict:
    payload: dict[str, Any] = {"marker": role, "body_sha256": "0" * 64}
    if fact is not None:
        payload["post_merge_ci"] = fact
    return {
        "event_id": f"evt-{role}-{ref}-{bool(fact)}",
        "kind": "commented",
        "outcome": "success",
        "ref": ref,
        "actor": {"role": role, "id": role},
        "payload": payload,
    }


class _Writer:
    """The card writer's `post_merge_ci`, idempotent by request id like the audit it stands for."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.by_request: dict[str, dict] = {}
        self.fail_next: BaseException | None = None

    def settle_post_merge_ci(self, *, actor, reference, merge, result_request_id, request_id, complete=False):
        if complete:
            event = self.by_request[result_request_id]
            assert event["payload"]["post_merge_ci"]["result"] == "green"
        return {"action": "unchanged"}

    def post_merge_ci(self, *, actor, reference, body, fact, request_id) -> dict:
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise failure
        event = {
            "event_id": f"evt-{len(self.events)}",
            "request_id": request_id,
            "kind": "commented",
            "outcome": "success",
            "ref": reference,
            "actor": {"role": "dispatcher", "id": actor},
            "payload": {"marker": "dispatcher", "body_sha256": "x", "post_merge_ci": dict(fact)},
            "body": body,
        }
        existing = self.by_request.get(request_id)
        if existing is not None:
            # The audit's replay rule: the same request id must claim the same operation.
            if existing["payload"] != event["payload"] or existing["ref"] != reference:
                raise TaskError("request_conflict", "request id belongs to another operation", 3)
            return {"replayed": True}
        self.by_request[request_id] = event
        self.events.append(event)
        return {"replayed": False}


class _SprintWriter:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.by_request: dict[str, dict] = {}
        self.fail_next: BaseException | None = None

    def comment(self, *, role, actor, reference, body, request_id) -> dict:
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise failure
        if request_id in self.by_request:
            return {"replayed": True}
        event = {
            "event_id": f"sprint-evt-{len(self.events)}",
            "request_id": request_id,
            "kind": "commented",
            "outcome": "success",
            "ref": reference,
            "actor": {"role": role, "id": actor},
            "payload": {"body_sha256": "x"},
            "body": body,
        }
        self.by_request[request_id] = event
        self.events.append(event)
        return {"replayed": False}


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _check(name: str, *, status: str = "completed", conclusion: str | None = "success", run: str = "501",
           head_sha: str | None = SHA) -> dict:
    item: dict[str, Any] = {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "details_url": f"https://github.com/{REPO}/actions/runs/{run}/job/1",
    }
    if head_sha is not None:
        item["head_sha"] = head_sha
    return item


class _GhHost:
    """`gh` answers keyed by what is asked; an exception answer is raised as the tool failing.

    `check_runs` as a list is served the way GitHub serves it: in pages of the requested size, each
    carrying the list's length as `total_count`. As a callable it answers each page number itself;
    anything else is the raw answer for every page. `status` as a list is a well-formed combined
    status for the merge commit; anything else is its raw answer. `dynamic_runs` as a list holds the
    ids of the commit's `dynamic` workflow runs; anything else is their raw answer.
    """

    def __init__(self, *, check_runs: Any = (), status: Any = (), logs: dict[str, str] | None = None,
                 required: list[str] | None = None, merge_commit: str = SHA, dynamic_runs: Any = ()) -> None:
        self.check_runs = check_runs
        self.status = status
        self.dynamic_runs = dynamic_runs
        self.logs = logs or {}
        self.merge_commit = merge_commit
        self.calls: list[list[str]] = []
        validation: dict[str, Any] = {"ci": "github"}
        if required is not None:
            validation["required_checks"] = required
        self.catalog = SimpleNamespace(
            adapter=lambda project: {"validation": validation},
            binding=lambda project: {"repo": "/srv/project"},
        )

    @staticmethod
    def _answer(value: Any) -> subprocess.CompletedProcess:
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, subprocess.CompletedProcess):
            return value
        return _completed(value if isinstance(value, str) else json.dumps(value))

    def _check_runs_page(self, query: dict[str, str]) -> Any:
        page = int(query.get("page", "1"))
        if callable(self.check_runs):
            return self.check_runs(page)
        if isinstance(self.check_runs, (list, tuple)):
            size = int(query.get("per_page", "30"))
            items = list(self.check_runs)
            return {"total_count": len(items), "check_runs": items[(page - 1) * size : page * size]}
        return self.check_runs

    def run_capture(self, args, label, *, cwd=None):
        self.calls.append(list(args))
        if args[:3] == ["gh", "repo", "view"]:
            return _completed(REPO + "\n")
        if args[:2] == ["gh", "api"]:
            path, _, raw_query = args[2].partition("?")
            query = dict(part.split("=", 1) for part in raw_query.split("&") if "=" in part)
            if path.endswith("/check-runs"):
                return self._answer(self._check_runs_page(query))
            if path.endswith("/status"):
                if isinstance(self.status, (list, tuple)):
                    return self._answer({"sha": SHA, "total_count": len(self.status), "statuses": list(self.status)})
                return self._answer(self.status)
            if path.endswith("/actions/runs") and query.get("event") == "dynamic" and query.get("head_sha") == SHA:
                if isinstance(self.dynamic_runs, (list, tuple)):
                    runs = [{"id": run_id, "event": "dynamic"} for run_id in self.dynamic_runs]
                    return self._answer({"total_count": len(runs), "workflow_runs": runs})
                return self._answer(self.dynamic_runs)
        if args[:3] == ["gh", "run", "view"] and "--log-failed" in args:
            return _completed(self.logs.get(args[3], ""))
        raise AssertionError(f"unexpected gh call {args}")

    def _run(self, args, label, *, cwd=None):
        self.calls.append(list(args))
        if args[:3] == ["gh", "pr", "view"] and "mergeCommit" in args:
            return _completed(self.merge_commit + "\n")
        raise HostError(f"unexpected command {args}")


class _NoCallHost(_GhHost):
    def run_capture(self, args, label, *, cwd=None):
        raise AssertionError(f"an absent watch asks nothing, not {args}")


def _runtime(host: Any) -> SimpleNamespace:
    runtime = SimpleNamespace(
        host=host,
        catalog=host.catalog,
        owner="ummanu-dispatcher",
        writer=_Writer(),
        reader=SimpleNamespace(show=lambda ref: {"state": "done"}),
        sprint_writer=_SprintWriter(),
        saved=[],
    )
    runtime.save_records = lambda payload, records: runtime.saved.append(json.loads(json.dumps(payload)))
    return runtime


def _workspace(root: Path, workflow: str | None) -> str:
    workspace = root / "ws"
    (workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    if workflow is not None:
        (workspace / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
    return str(workspace)


PUSH_MAIN = "on:\n  pull_request:\n  push:\n    branches: [main]\njobs: {}\n"


def _watch(payload: dict, *, ci: str = "github", workspace: str = "", sha: str = SHA, now: float = 1000.0) -> dict:
    landing = MergeLanding(sha=sha, base="main", path="github-pr", ci=ci, branch=BRANCH)
    task = {"ref": REF, "project": "sample", "sprint": SPRINT}
    return post_merge.open_watch(task, payload, landing, workspace=workspace,
                                release={"attempt_id": "attempt-1", "attempt_round": 1, "report_generation": 1}, now=now)


def _significant(events: list[dict]) -> list[dict]:
    return [
        event
        for event in events
        if is_significant_observer_event(event, linked_refs={REF}, sprint_ref=SPRINT)
    ]


MERGED = {"base": "main", "merge_sha": SHA, "path": "github-pr"}


class WakePredicateTests(unittest.TestCase):
    """The one enforcement place: `is_significant_card_event`."""

    def test_which_done_and_which_result_wake_the_observer(self) -> None:
        fact = {"result": "green", "card": REF, "base": "main", "merge_sha": SHA}
        table = [
            ("dispatcher release that merged", _done(release_merge=MERGED), False),
            ("merged release, sha not read back yet", _done(release_merge={"base": "main", "merge_sha": ""}), False),
            ("dispatcher release that merged nothing", _done(), True),
            ("manual PO Done", _done(actor="po", source=CardState.BLOCKED), True),
            ("steward Done", _done(actor="steward", source=CardState.BLOCKED), True),
            ("a PO Done cannot borrow the marker", _done(actor="po", release_merge=MERGED, source=CardState.BLOCKED), True),
            ("post-merge result", _comment(fact=fact), True),
            ("ordinary dispatcher comment", _comment(), False),
            ("result shape from another role", _comment(role="worker", fact=fact), False),
            ("unknown result", _comment(fact={**fact, "result": "maybe"}), False),
            ("result on an unlinked card", _comment(ref="other-1", fact=fact), False),
        ]
        for label, event, expected in table:
            with self.subTest(label):
                self.assertIs(is_significant_card_event(event, linked_refs={REF}), expected)
                self.assertIs(
                    is_significant_observer_event(event, linked_refs={REF}, sprint_ref=SPRINT), expected
                )

    def test_the_dispatcher_sprint_comment_is_not_a_second_wake(self) -> None:
        self.assertFalse(
            is_significant_observer_event(_comment(ref=SPRINT), linked_refs={REF}, sprint_ref=SPRINT)
        )


class ReleaseOpensTheWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.record = DispatcherRecord(
            worker="w", workspace=_workspace(self.root, PUSH_MAIN), handle="", head="codex",
            review_head="claude", attempt_id="attempt-1", comment_baseline=0, review_baseline=0,
            state="assessment", claimed_at=0.0,
        )
        self.records = {REF: self.record}
        self.payload: dict = {}
        self.runtime = mock.Mock()
        self.runtime.owner = "ummanu-dispatcher"
        self.order: list[str] = []
        self.runtime.save_records.side_effect = lambda payload, records: self.order.append(
            "save:" + ",".join(sorted(payload.get(post_merge.WATCHES_KEY) or {}))
        )
        self.accounting = mock.Mock()
        self.accounting.terminal_effect.side_effect = lambda *a, **k: self.order.append("done")
        patcher = mock.patch.object(release_lifecycle, "attempt_accounting", self.accounting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def release(self, task: dict) -> dict:
        return release_lifecycle.release_effect(
            self.runtime, task, self.record, self.records, self.payload, "attempt-1",
            step="assessment", move_reason="Observer decision: accept.", decision="accept",
        )

    def test_a_merge_records_a_durable_watch_before_pending_and_marks_the_release(self) -> None:
        self.runtime.host.complete_green.return_value = MergeLanding(
            sha=SHA, base="main", path="github-pr", ci="github", branch=BRANCH
        )
        outcome = self.release({"ref": REF, "type": "code", "project": "sample", "sprint": SPRINT})
        self.assertEqual(outcome["to"], "post_merge_ci")
        watch = self.payload[post_merge.WATCHES_KEY][REF]
        self.assertEqual(
            {key: watch[key] for key in ("ref", "project", "base", "merge_sha", "sprint", "absent_reason")},
            {"ref": REF, "project": "sample", "base": "main", "merge_sha": SHA, "sprint": SPRINT, "absent_reason": ""},
        )
        self.assertIsInstance(watch["started_at"], float)
        # The watch is on disk before the Done move; the record is dropped only afterwards.
        self.assertEqual(self.order[0], f"save:{REF}")
        self.assertLess(self.order.index(f"save:{REF}"), self.order.index("done"))
        kwargs = self.accounting.terminal_effect.call_args.kwargs
        self.assertEqual((kwargs["target"], kwargs["terminal_state"]), ("post_merge_ci", "done"))
        self.assertEqual(kwargs["release_merge"], {"base": "main", "merge_sha": SHA, "path": "github-pr"})
        self.assertNotIn(REF, self.records)

    def test_a_replayed_release_keeps_the_first_watch(self) -> None:
        self.runtime.host.complete_green.return_value = MergeLanding(
            sha=SHA, base="main", path="github-pr", ci="github", branch=BRANCH
        )
        self.accounting.terminal_effect.side_effect = RuntimeError("crash between merge and Done")
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.release({"ref": REF, "type": "code", "project": "sample", "sprint": SPRINT})
        first = dict(self.payload[post_merge.WATCHES_KEY][REF])
        # The restart reads the production state back from disk.
        self.payload = json.loads(json.dumps(self.payload))
        self.accounting.terminal_effect.side_effect = None
        self.release({"ref": REF, "type": "code", "project": "sample", "sprint": SPRINT})
        self.assertEqual(self.payload[post_merge.WATCHES_KEY][REF], first)

    def test_a_release_that_merged_nothing_opens_no_watch_and_marks_nothing(self) -> None:
        self.runtime.host.complete_green.return_value = None  # automerge off, noop mode
        self.release({"ref": REF, "type": "code", "project": "sample", "sprint": SPRINT})
        self.assertEqual(self.payload.get(post_merge.WATCHES_KEY) or {}, {})
        self.assertIsNone(self.accounting.terminal_effect.call_args.kwargs.get("release_merge"))

    def test_an_unreadable_landed_commit_retains_the_round_without_repeating_the_merge(self) -> None:
        self.runtime.host.complete_green.return_value = MergeLanding(
            sha="", base="main", path="github-pr", ci="github", branch=BRANCH
        )
        self.runtime.host.landed_merge_commit.return_value = ""
        task = {"ref": REF, "type": "code", "project": "sample", "sprint": SPRINT}
        self.assertEqual(self.release(task)["status"], "pending")
        self.runtime.host.teardown.assert_not_called()
        self.assertIn(REF, self.records)
        self.assertEqual(post_merge.reconcile_post_merge_watches(self.runtime, self.payload, self.records), [])
        self.runtime.writer.post_merge_ci.assert_not_called()
        self.runtime.host.landed_merge_commit.return_value = SHA
        self.assertEqual(self.release(task)["to"], "post_merge_ci")
        self.runtime.host.complete_green.assert_called_once()
        self.runtime.host.teardown.assert_called_once()
        self.runtime.host.landed_merge_commit.assert_called_with(self.record, path="github-pr", branch=BRANCH)

    def test_another_or_unproven_release_cannot_reuse_a_landed_watch(self) -> None:
        for changed in ({"attempt_id": "another-attempt"}, {"attempt_round": 1},
                        {"report_generation": 1}, {"legacy": True}):
            with self.subTest(changed=changed):
                self.payload, self.records = {}, {REF: self.record}
                self.runtime.host.complete_green.return_value = MergeLanding(SHA, "main", "github-pr", "github", BRANCH)
                task = {"ref": REF, "type": "code", "project": "sample", "sprint": SPRINT}
                self.release(task)
                watch = self.payload[post_merge.WATCHES_KEY][REF]
                if "legacy" in changed:
                    del watch["release"]
                else:
                    watch["release"].update(changed)
                before = json.loads(json.dumps(watch))
                self.runtime.host.reset_mock()
                self.accounting.reset_mock()
                outcome = self.release(task)
                self.assertEqual((outcome["status"], outcome["reason"]),
                                 ("blocked", "post-merge watch release identity mismatch"))
                self.runtime.host.complete_green.assert_not_called()
                self.runtime.host.teardown.assert_not_called()
                self.assertEqual(self.accounting.terminal_effect.call_args.kwargs["target"], "blocked")
                self.assertEqual(self.payload[post_merge.WATCHES_KEY][REF], before)

    def test_a_research_release_opens_no_watch(self) -> None:
        with mock.patch.object(release_lifecycle, "missing_completion_evidence", return_value=""):
            self.release({"ref": REF, "type": "research", "project": "sample", "sprint": SPRINT})
        self.runtime.host.complete_green.assert_not_called()
        self.assertEqual(self.payload.get(post_merge.WATCHES_KEY) or {}, {})
        self.assertIsNone(self.accounting.terminal_effect.call_args.kwargs.get("release_merge"))


class _ReleaseFixture:
    """A real writer over the in-memory board, its card released into Post-merge CI by `release`."""

    def prepare(self, *, managed=False, reviewed=True, path="github-pr"):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = CountingStore(root)
        store.meta[1].update(sprint_ref="sprint:1" if managed else "", review="required" if reviewed else "skipped",
                             claim='{"worker":"worker"}', resolved_head="worker", resolved_review_head="reviewer")
        store.meta[record_key("sprint", "sprint:1")]["sprint_reservations"] = '["alpha"]'
        audit = MarkerAudit(root)
        self.enterContext(mock.patch("ummanu.board.sql_audit.SqlTaskAudit", return_value=audit))
        runtime = _runtime(_GhHost(check_runs=[_check("unit", status="in_progress", conclusion=None)]))
        runtime.writer = TaskWriter(store, data_dir=root)
        runtime.reader = runtime.writer.reader
        runtime.audit = audit
        for role, kind, marker in (("worker", "card.reported", "report:done"),
                                   ("reviewer", "card.verdict", "review:green")):
            if role == "reviewer" and not reviewed:
                continue
            audit.append(role, Event(role, EventKind(kind), EntityKind.CARD, "demo-1", Actor(role, role),
                                     "candidate evidence", datetime.now(UTC), data={
                                         "marker": marker, "body": "candidate evidence", "status": marker.split(":")[1],
                                     }).to_record(role))
        writer = runtime.writer
        writer.move(role="dispatcher", actor=runtime.owner, reference="demo-1", target="ci",
                    reason="report ready", request_id="ci")
        if reviewed:
            writer.move(role="dispatcher", actor=runtime.owner, reference="demo-1", target="review",
                        reason="gate green", request_id="review")
        writer.move(role="dispatcher", actor=runtime.owner, reference="demo-1", target="assessment",
                    reason="wait for observer" if managed else AUTOMATIC_ASSESSMENT_REASON, request_id="park")
        if managed:
            bind_observer(self, "sprint:1")
            writer.decide(role="observer", actor="observer", reference="demo-1", kind="accept",
                          body="release the candidate", request_id="accept")
        runtime.host.complete_green = mock.Mock(return_value=MergeLanding(SHA, "main", path, "github", BRANCH))
        runtime.host.teardown = mock.Mock(return_value={"status": "ok"})
        runtime.host.mode = "real"
        runtime.host.head_commit = mock.Mock(return_value=SHA)
        runtime.host.stop = mock.Mock()
        record = DispatcherRecord.from_json({
            "worker": "worker", "head": "worker", "review_head": "reviewer" if reviewed else "", "state": "assessment",
            "attempt_id": "release-round", "workspace": _workspace(root, PUSH_MAIN), "gate_state": "green",
        })
        record.gate_attestation = PersistedGateReceipt.from_value(mint_gate_receipt(
            validated_sha=SHA, base_sha=OTHER_SHA, gate_mode="github", check_set_identity="unit",
            required_checks=[{"name": "unit", "conclusion": "SUCCESS"}],
        ))
        self.runtime, self.store, self.record, self.root = runtime, store, record, root
        self.payload, self.records = {}, {"demo-1": record}
        self.decision = "accept" if managed else ""
        return runtime

    def release(self):
        return release_lifecycle.release_effect(
            self.runtime, self.runtime.reader.show("demo-1"), self.record, self.records, self.payload, "tick",
            step="assessment", move_reason="release admitted", decision=self.decision,
            verdict="green" if self.record.review_head else "missing",
        )

    def reconcile(self, now=1100):
        watch = self.payload[post_merge.WATCHES_KEY].get("demo-1")
        if watch is not None:
            watch["started_at"] = 1000
        return post_merge.reconcile_post_merge_watches(self.runtime, self.payload, self.records, now=now)

    def transitions(self):
        return [event for event in self.runtime.audit.events("demo-1") if recorded_card_transition(event)]


class PostMergeLifecycleTests(_ReleaseFixture, unittest.TestCase):
    def test_managed_automatic_and_skipped_merges_wait_without_a_round_then_finish_exact_green(self):
        for managed, reviewed, path in ((True, True, "github-pr"), (True, False, "push"),
                                        (False, True, "push"), (False, False, "github-pr")):
            with self.subTest(managed=managed, reviewed=reviewed, path=path):
                runtime = self.prepare(managed=managed, reviewed=reviewed, path=path)
                self.assertEqual(self.release()["to"], "post_merge_ci")
                task = runtime.reader.show("demo-1")
                self.assertIsNone(task["claim"]["worker"])
                self.assertNotIn("demo-1", [card["ref"] for card in runtime.reader.capacity_peers()])
                self.assertEqual(self.records, {})
                release = self.transitions()[-1]
                self.assertEqual((release["kind"], owed_target(release)), ("card.released", ""))
                self.assertEqual(release["data"]["release_merge"], {"base": "main", "merge_sha": SHA, "path": path})
                self.assertFalse(is_significant_card_event(release, linked_refs={"demo-1"}))
                self.assertFalse(is_candidate(release))
                self.assertEqual(self.reconcile(), [])
                self.assertEqual(runtime.reader.show("demo-1")["state"], "post_merge_ci")
                runtime.host.check_runs = [_check("unit")]
                if managed:
                    self.store.meta[record_key("sprint", "sprint:1")]["sprint_status"] = "stopped"
                self.assertEqual(self.reconcile()[0]["result"], "green")
                done = self.transitions()[-1]
                self.assertEqual((done["kind"], owed_target(done)), ("card.moved", "done"))
                self.assertEqual(runtime.reader.show("demo-1")["state"], "done")
                facts = [event for event in runtime.audit.events("demo-1") if event.get("kind") == "commented"]
                self.assertEqual(done["data"]["post_merge_ci_event"], facts[-1]["event_id"])
                self.assertFalse(is_significant_card_event(done, linked_refs={"demo-1"}))
                self.assertEqual(self.payload[post_merge.WATCHES_KEY], {})
                runtime.host.complete_green.assert_called_once()
                runtime.host.teardown.assert_called_once()

    def test_manual_moves_and_uncommitted_or_wrong_green_cannot_complete(self):
        runtime = self.prepare()
        self.release()
        for role in ("po", "observer", "steward", "worker", "reviewer", "dispatcher"):
            with self.subTest(role=role), self.assertRaises(TaskError):
                runtime.writer.move(role=role, actor=role, reference="demo-1", target="done",
                                    reason="manual completion", request_id="manual-" + role)
        watch = self.payload[post_merge.WATCHES_KEY]["demo-1"]
        arguments = {"actor": runtime.owner, "reference": "demo-1", "merge": post_merge.release_merge_marker(watch),
                     "result_request_id": "result", "request_id": "finish", "complete": True}
        with self.assertRaises(TaskError):
            runtime.writer.settle_post_merge_ci(**arguments)
        staged = _comment(ref="demo-1", fact={"card": "demo-1", "base": "main", "merge_sha": SHA, "result": "green"})
        staged["request_id"] = "result"
        runtime.audit.stage("result", staged)
        with self.assertRaises(TaskError):
            runtime.writer.settle_post_merge_ci(**arguments)
        runtime.audit.discard("result")
        runtime.writer.post_merge_ci(actor=runtime.owner, reference="demo-1", body="wrong merge",
                                     fact={"card": "demo-1", "base": "main", "merge_sha": OTHER_SHA, "result": "green"},
                                     request_id="result")
        with self.assertRaises(TaskError):
            runtime.writer.settle_post_merge_ci(**arguments)
        self.assertEqual(runtime.reader.show("demo-1")["state"], "post_merge_ci")

    def test_non_green_results_remain_pending_with_actionable_single_wakes(self):
        for result in ("red", "absent", "timeout"):
            with self.subTest(result=result):
                runtime = self.prepare(managed=True)
                self.release()
                if result == "red":
                    runtime.host.check_runs = [_check("unit", conclusion="failure", run="901")]
                    runtime.host.logs = {"901": "unit\tRun tests\t##[error]boom\n"}
                if result == "absent":
                    self.payload[post_merge.WATCHES_KEY]["demo-1"]["absent_reason"] = "no push workflow"
                outcomes = self.reconcile(4600 if result == "timeout" else 1100)
                self.assertEqual(outcomes[-1]["result"], result)
                self.assertEqual(runtime.reader.show("demo-1")["state"], "post_merge_ci")
                wakes = [event for event in runtime.audit.events("demo-1")
                         if event.get("kind") == "commented" and is_significant_card_event(event, linked_refs={"demo-1"})]
                self.assertEqual(len(wakes), 1)
                fact = wakes[0]["payload"]["post_merge_ci"]
                if result == "red":
                    self.assertEqual((fact["failed_checks"], fact["classification"]), (["unit"], "product"))
                    self.assertIn("901", fact["runs"][0]["url"])
                else:
                    self.assertTrue(fact["reason"])
                self.assertEqual(len(runtime.sprint_writer.events), 1)
                self.assertEqual(self.records, {})

    def test_published_green_survives_finalizer_failure_and_effect_reply_loss(self):
        for after_effect in (False, True):
            with self.subTest(after_effect=after_effect):
                runtime = self.prepare()
                self.release()
                runtime.host.check_runs = [_check("unit")]
                settle = runtime.writer.settle_post_merge_ci

                def interrupted(*, after_effect=after_effect, settle=settle, **kwargs):
                    if after_effect:
                        settle(**kwargs)
                    raise OSError("finalizer reply lost")

                with (mock.patch.object(runtime.writer, "settle_post_merge_ci", side_effect=interrupted),
                      mock.patch("ummanu.dispatch.e2e_after_merge.enqueue") as enqueue):
                    self.assertEqual(self.reconcile()[0]["status"], "degraded")
                    enqueue.assert_not_called()
                self.payload = json.loads(json.dumps(runtime.saved[-1]))
                self.assertTrue(self.payload[post_merge.WATCHES_KEY]["demo-1"]["published"])
                self.assertEqual(self.reconcile()[0]["result"], "green")
                self.assertEqual(runtime.reader.show("demo-1")["state"], "done")
                self.assertEqual(sum(recorded_card_transition(event)[1] == "done" for event in self.transitions()), 1)
                self.assertEqual(sum(event.get("kind") == "commented" for event in runtime.audit.events("demo-1")), 1)
                watch = runtime.saved[-2][post_merge.WATCHES_KEY]["demo-1"]
                with self.assertRaises(TaskError):
                    runtime.writer.settle_post_merge_ci(
                        actor=runtime.owner, reference="demo-1", merge={**post_merge.release_merge_marker(watch), "path": "push"},
                        result_request_id=post_merge._request_id(watch, "card"),
                        request_id=post_merge._request_id(watch, "done"), complete=True,
                    )

    def test_failed_teardown_watch_cannot_release_a_reworked_candidate(self):
        runtime = self.prepare(managed=True)
        runtime.host.teardown.side_effect = HostError("cleanup provenance refused")
        self.assertEqual(self.release()["status"], "blocked")
        self.assertEqual(runtime.reader.show("demo-1")["state"], "blocked")
        self.assertEqual(self.records, {})
        watch = self.payload[post_merge.WATCHES_KEY]["demo-1"]
        watch["result"] = {"card": "demo-1", "base": "main", "merge_sha": SHA, "result": "green"}
        post_merge.publish(runtime, watch)
        watch["published"] = True

        runtime.writer.decide(role="observer", actor="observer", reference="demo-1", kind="rework",
                              body="implement another candidate", request_id="rework")
        runtime.writer.move(role="dispatcher", actor=runtime.owner, reference="demo-1", target="in_progress",
                            reason="rework candidate", request_id="resume")
        self.record = DispatcherRecord.from_json({**self.record.to_json(), "attempt_id": "rework-round",
                                                 "attempt_round": 2, "report_generation": 2})
        self.records = {"demo-1": self.record}
        self.record.gate_attestation = PersistedGateReceipt.from_value(mint_gate_receipt(
            validated_sha=OTHER_SHA, base_sha=SHA, gate_mode="github", check_set_identity="unit",
            required_checks=[{"name": "unit", "conclusion": "SUCCESS"}],
        ))
        runtime.host.head_commit.return_value = OTHER_SHA
        for state in ("ci", "review", "assessment"):
            runtime.writer.move(role="dispatcher", actor=runtime.owner, reference="demo-1", target=state,
                                reason="reviewed rework", request_id="rework-" + state)
        runtime.writer.decide(role="observer", actor="observer", reference="demo-1", kind="accept",
                              body="release new candidate", request_id="accept-rework")
        runtime.host.teardown.side_effect = None
        runtime.host.complete_green.reset_mock()
        runtime.host.teardown.reset_mock()
        self.assertEqual(self.release()["reason"], "post-merge watch release identity mismatch")
        self.assertEqual(runtime.reader.show("demo-1")["state"], "blocked")
        runtime.host.complete_green.assert_not_called()
        runtime.host.teardown.assert_not_called()
        self.assertEqual(self.payload[post_merge.WATCHES_KEY]["demo-1"], watch)
        with (mock.patch.object(runtime.writer, "settle_post_merge_ci") as settle,
              mock.patch("ummanu.dispatch.e2e_after_merge.enqueue") as enqueue):
            self.assertEqual(self.reconcile()[0]["action"], "post-merge-ci-release-superseded")
            self.assertEqual(self.reconcile(), [])
            settle.assert_not_called()
            enqueue.assert_not_called()
        self.assertFalse(any(recorded_card_transition(event)[1] in {"post_merge_ci", "done"}
                             for event in self.transitions()))
        facts = [event["payload"]["post_merge_ci"] for event in runtime.audit.events("demo-1")
                 if event.get("payload", {}).get("post_merge_ci")]
        self.assertEqual(facts, [watch["result"]])

    def test_blocked_release_publishes_once_after_pending_and_publication_retry_without_settling(self):
        runtime = self.prepare(managed=True)
        runtime.host.teardown.side_effect = HostError("cleanup provenance refused")
        self.release()
        with (mock.patch.object(runtime.writer, "settle_post_merge_ci") as settle,
              mock.patch("ummanu.dispatch.e2e_after_merge.enqueue") as enqueue):
            self.assertEqual(self.reconcile(), [])
            runtime.host.check_runs = [_check("unit")]
            runtime.sprint_writer.fail_next = HostError("sprint reply unavailable")
            self.assertEqual(self.reconcile()[0]["status"], "degraded")
            self.payload = json.loads(json.dumps(runtime.saved[-1]))
            self.assertEqual(self.reconcile()[0]["action"], "post-merge-ci-release-superseded")
            self.assertEqual(self.reconcile(), [])
            settle.assert_not_called()
            enqueue.assert_not_called()
        self.assertEqual(runtime.reader.show("demo-1")["state"], "blocked")
        facts = [event for event in runtime.audit.events("demo-1") if event.get("payload", {}).get("post_merge_ci")]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["payload"]["post_merge_ci"]["merge_sha"], SHA)
        self.assertEqual(len(runtime.sprint_writer.events), 1)
        self.assertEqual(self.payload[post_merge.WATCHES_KEY], {})

    def test_live_released_done_watch_upgrade_uses_committed_result_without_rewriting_history(self):
        for recorded, published in ((None, False), ("green", False), ("green", True), ("red", False), ("red", True)):
            with self.subTest(recorded=recorded, published=published):
                runtime = self.prepare()
                self.release()
                release = self.transitions()[-1]
                request = release["request_id"]
                runtime.audit._committed[request]["transition"]["target"] = "done"
                self.store.rows[1]["column_id"] = 7
                before = runtime.audit.events("demo-1")
                watch = self.payload[post_merge.WATCHES_KEY]["demo-1"]
                del watch["release"]
                if recorded is not None:
                    watch["result"] = {"card": "demo-1", "base": "main", "merge_sha": SHA, "result": recorded}
                if published:
                    post_merge.publish(runtime, watch)
                    watch["published"] = True
                self.reconcile()
                self.assertEqual(runtime.audit.events("demo-1")[:len(before)], before)
                self.assertEqual(runtime.reader.show("demo-1")["state"], "done" if recorded == "green" else "post_merge_ci")
                if recorded == "green":
                    self.assertEqual(len(self.transitions()), 4)
                elif recorded is None:
                    runtime.host.check_runs = [_check("unit")]
                    self.reconcile()
                    self.assertEqual(runtime.reader.show("demo-1")["state"], "done")
                runtime.host.complete_green.assert_called_once()
                runtime.host.teardown.assert_called_once()

    def test_historical_done_without_a_live_watch_keeps_its_history(self):
        runtime = self.prepare()
        runtime.host.complete_green.return_value = None
        self.assertEqual(self.release()["to"], "done")
        before = runtime.audit.events("demo-1")
        self.assertEqual(self.reconcile(), [])
        self.assertEqual(runtime.audit.events("demo-1"), before)
        self.assertEqual(runtime.reader.show("demo-1")["state"], "done")


ISSUE = "issue:0123456789abcdef0123"


class ManualDispositionTests(_ReleaseFixture, unittest.TestCase):
    """`task dispose-post-merge`: the explicit human exit of a non-green Post-merge CI wait."""

    def prepare(self, *, managed=True, reviewed=True, path="github-pr"):
        runtime = super().prepare(managed=managed, reviewed=reviewed, path=path)
        rows, meta = self.store.rows, self.store.meta
        for number, reference, values in (
            (90, ISSUE, {"record_type": "issue", "issue_product": "shop", "issue_kind": "bug", "issue_priority": "P2"}),
            (91, "product:shop", {"record_type": "product", "product_id": "shop", "product_projects": '["alpha"]'}),
            (92, "issue:other-product", {"record_type": "issue", "issue_product": "other", "issue_kind": "bug",
                                         "issue_priority": "P2"}),
            (93, "product:other", {"record_type": "product", "product_id": "other", "product_projects": '["beta"]'}),
        ):
            rows[number] = {"id": number, "reference": reference, "title": reference, "description": "",
                            "project_id": rows[1]["project_id"], "column_id": 1, "position": number, "is_active": 1}
            meta[number] = values
        self.store.add(94, "ready", project="alpha")
        self.store.add(95, "ready", project="beta")
        self.store.add(96, "done", project="alpha", closed=True)
        return runtime

    def wait_on(self, result):
        """Release, publish one committed `result` and return its event id; the card waits in Post-merge CI."""
        self.release()
        runtime = self.runtime
        if result == "red":
            runtime.host.check_runs = [_check("unit", conclusion="failure", run="901")]
            runtime.host.logs = {"901": "unit\tRun tests\t##[error]boom\n"}
        if result == "absent":
            self.payload[post_merge.WATCHES_KEY]["demo-1"]["absent_reason"] = "no push workflow"
        self.assertEqual(self.reconcile(4600 if result == "timeout" else 1100)[-1]["result"], result)
        self.assertEqual(runtime.reader.show("demo-1")["state"], "post_merge_ci")
        return self.facts()[-1]["event_id"]

    def facts(self):
        return [event for event in self.runtime.audit.events("demo-1") if event.get("payload", {}).get("post_merge_ci")]

    def dispose(self, event_id, *, role="observer", action="ignore-flake", follow_up="", **kwargs):
        return self.runtime.writer.dispose_post_merge_ci(
            role=role, actor=role, reference="demo-1", action=action, reason=kwargs.pop("reason", "known flaky runner"),
            result_event=event_id, follow_up=follow_up, request_id=kwargs.pop("request_id", "disposition"), **kwargs,
        )

    def test_each_action_closes_each_non_green_result_in_done_with_its_evidence(self):
        for result in ("red", "absent", "timeout"):
            for action, follow_up in (("ignore-flake", ""), ("issue", ISSUE), ("hotfix", "demo-94")):
                with self.subTest(result=result, action=action):
                    runtime = self.prepare()
                    event_id = self.wait_on(result)
                    before = len(self.store.comments.get(1, []))
                    disposed = self.dispose(event_id, action=action, follow_up=follow_up)
                    self.assertEqual((disposed["action"], disposed["replayed"]), ("disposed", False))
                    self.assertEqual(runtime.reader.show("demo-1")["state"], "done")
                    done = self.transitions()[-1]
                    self.assertEqual(done["event_id"], disposed["event_id"])
                    self.assertEqual((done["kind"], recorded_card_transition(done), owed_target(done)),
                                     ("card.moved", ("post_merge_ci", "done"), "done"))
                    self.assertEqual(done["actor"]["role"], "observer")
                    self.assertEqual(done["data"]["post_merge_disposition"], {
                        "action": action, "reason": "known flaky runner", **({"ref": follow_up} if follow_up else {})})
                    self.assertEqual(done["data"]["post_merge_ci_event"], event_id)
                    self.assertEqual(done["data"]["release_merge"], {"base": "main", "merge_sha": SHA, "path": "github-pr"})
                    self.assertIn(f"Post-merge CI {result} for {SHA} stays non-green", done["reason"])
                    self.assertEqual([fact["payload"]["post_merge_ci"]["result"] for fact in self.facts()], [result])
                    self.assertEqual(len(self.store.comments[1]), before + 1)
                    self.assertFalse(is_significant_card_event(done, linked_refs={"demo-1"}))
                    self.assertFalse(is_merged_release(done))
                    self.assertEqual((self.records, self.payload[post_merge.WATCHES_KEY]), ({}, {}))
                    self.assertNotIn("e2e", runtime.reader.show("demo-1"))
                    dod = sprint_dod.section(sprint_dod.new_items(["ship"]), events=runtime.audit.events("demo-1"),
                                             cards=[{"ref": "demo-1", "dod_item": "d1", "state": "done"}])
                    self.assertEqual(dod["in_main"], 1)
                    self.assertEqual(dod["items"][0]["delivery"]["merged"][0]["event_id"],
                                     self.transitions()[-2]["event_id"])
                    events = runtime.audit.events("demo-1")
                    text = render_origin_input(runtime.reader.show("demo-1"), {"session": "po-1"}, events,
                                               events.index(done))
                    self.assertIn("## Post-merge CI disposition", text)
                    self.assertIn(f"merged {SHA} onto main", text)

    def test_a_lost_reply_replays_and_a_changed_or_late_request_cannot_override(self):
        runtime = self.prepare()
        event_id = self.wait_on("red")
        first = self.dispose(event_id, action="issue", follow_up=ISSUE)
        history, comments = runtime.audit.events("demo-1"), list(self.store.comments[1])
        replay = self.dispose(event_id, action="issue", follow_up=ISSUE)
        self.assertEqual((replay["event_id"], replay["replayed"]), (first["event_id"], True))
        self.assertEqual((runtime.audit.events("demo-1"), self.store.comments[1]), (history, comments))
        for changed in ({"action": "ignore-flake", "follow_up": ""}, {"action": "issue", "follow_up": ISSUE,
                        "reason": "another reason"}, {"action": "hotfix", "follow_up": "demo-94"},
                        {"action": "issue", "follow_up": ISSUE, "role": "po"}):
            with self.subTest(changed=changed), self.assertRaises(TaskError) as refused:
                self.dispose(event_id, **changed)
            self.assertEqual(refused.exception.code, "request_conflict")
        with self.assertRaises(TaskError) as late:
            self.dispose(event_id, action="ignore-flake", request_id="late-disposition")
        self.assertEqual(late.exception.code, "transition_forbidden")
        self.assertEqual(runtime.audit.events("demo-1"), history)

    def test_wrong_results_pending_and_wrong_follow_ups_leave_the_wait_untouched(self):
        runtime = self.prepare()
        self.release()
        self.assertEqual(self.reconcile(), [])
        writer = runtime.writer
        cases = [("pending", {}, "validation")]
        staged = _comment(ref="demo-1", fact={"card": "demo-1", "base": "main", "merge_sha": SHA, "result": "red"})
        staged["event_id"], staged["request_id"] = "staged-red", "staged-red"
        runtime.audit.stage("staged-red", staged)
        cases.append(("staged-red", {}, "validation"))
        for request, fact, code in (("wrong-sha", {"merge_sha": OTHER_SHA, "result": "red"}, "validation"),
                                    ("other-card", {"card": "demo-2", "result": "red"}, "validation"),
                                    ("other-base", {"base": "develop", "result": "timeout"}, "validation"),
                                    ("green", {"result": "green"}, "transition_forbidden")):
            writer.post_merge_ci(actor=runtime.owner, reference="demo-1", body=request, request_id=request,
                                 fact={"card": "demo-1", "base": "main", "merge_sha": SHA, **fact})
            cases.append((next(event["event_id"] for event in self.facts() if event["request_id"] == request),
                          {}, code))
        writer.post_merge_ci(actor=runtime.owner, reference="demo-2", body="other", request_id="demo-2-red",
                             fact={"card": "demo-2", "base": "main", "merge_sha": SHA, "result": "red"})
        foreign = next(event["event_id"] for event in runtime.audit.events("demo-2") if event.get("request_id") == "demo-2-red")
        cases.append((foreign, {}, "validation"))
        writer.post_merge_ci(actor=runtime.owner, reference="demo-1", body="red", request_id="red",
                             fact={"card": "demo-1", "base": "main", "merge_sha": SHA, "result": "red"})
        red = self.facts()[-1]["event_id"]
        cases += [
            (red, {"action": "skip"}, "validation"),
            (red, {"reason": "  "}, "validation"),
            (red, {"action": "issue"}, "validation"),
            (red, {"action": "hotfix"}, "validation"),
            (red, {"follow_up": ISSUE}, "validation"),
            (red, {"action": "hotfix", "follow_up": "demo-1"}, "validation"),
            (red, {"action": "issue", "follow_up": "issue:other-product"}, "validation"),
            (red, {"action": "issue", "follow_up": "demo-94"}, "validation"),
            (red, {"action": "issue", "follow_up": "issue:missing"}, "not_found"),
            (red, {"action": "hotfix", "follow_up": ISSUE}, "validation"),
            (red, {"action": "hotfix", "follow_up": "demo-95"}, "validation"),
            (red, {"action": "hotfix", "follow_up": "demo-96"}, "validation"),
            (red, {"action": "hotfix", "follow_up": "demo-404"}, "not_found"),
        ]
        history = runtime.audit.events("demo-1")
        for index, (event_id, arguments, code) in enumerate(cases):
            with self.subTest(event_id=event_id, arguments=arguments), self.assertRaises(TaskError) as refused:
                self.dispose(event_id, request_id=f"attempt-{index}", **arguments)
            self.assertEqual(refused.exception.code, code)
        self.assertEqual(runtime.reader.show("demo-1")["state"], "post_merge_ci")
        self.assertEqual([event for event in runtime.audit.events("demo-1") if recorded_card_transition(event)],
                         [event for event in history if recorded_card_transition(event)])
        self.store.rows[90]["is_active"] = 0
        with self.assertRaises(TaskError) as closed:
            self.dispose(red, action="issue", follow_up=ISSUE, request_id="closed-issue")
        self.assertEqual(closed.exception.code, "validation")

    def test_only_the_cards_observer_or_the_po_within_its_authority_disposes(self):
        runtime = self.prepare()
        event_id = self.wait_on("red")
        for role in ("worker", "reviewer", "dispatcher", "steward", "retro"):
            with self.subTest(role=role), self.assertRaises(TaskError) as refused:
                self.dispose(event_id, role=role)
            self.assertEqual(refused.exception.code, "role_forbidden")
        with as_observer("sprint:2"), self.assertRaises(TaskError) as foreign:
            self.dispose(event_id, request_id="foreign-observer")
        self.assertEqual(foreign.exception.code, "observer_sprint_mismatch")
        with self.assertRaises(TaskError) as reserved:
            self.dispose(event_id, role="po", request_id="po-without-override")
        self.assertEqual(reserved.exception.code, "sprint_write_forbidden")
        for role in ("po", "observer"):
            with self.subTest(generic_move=role), self.assertRaises(TaskError) as generic:
                runtime.writer.move(role=role, actor=role, reference="demo-1", target="done",
                                    reason="manual completion", request_id="generic-" + role,
                                    sprint_override=role == "po", sprint_override_reason="owner")
            self.assertEqual(generic.exception.code, "transition_forbidden")
            self.assertIn("task dispose-post-merge", generic.exception.message)
        self.assertEqual(runtime.reader.show("demo-1")["state"], "post_merge_ci")
        disposed = self.dispose(event_id, role="po", sprint_override=True, sprint_override_reason="owner accepted risk")
        self.assertEqual(runtime.reader.show("demo-1")["state"], "done")
        self.assertEqual(self.transitions()[-1]["actor"], {"role": "po", "id": "po"})
        self.assertFalse(is_significant_card_event(self.transitions()[-1], linked_refs={"demo-1"}))
        self.assertEqual(disposed["replayed"], False)

    def test_the_observer_cannot_dispose_a_card_outside_its_sprint(self):
        runtime = self.prepare(managed=False)
        bind_observer(self, "sprint:1")
        event_id = self.wait_on("absent")
        with self.assertRaises(TaskError) as refused:
            self.dispose(event_id, request_id="observer-outside-sprint")
        self.assertEqual(refused.exception.code, "sprint_write_forbidden")
        self.assertEqual(self.dispose(event_id, role="po")["task"]["state"], "done")
        self.assertEqual(runtime.reader.show("demo-1")["state"], "done")

    def test_a_live_watch_after_manual_settlement_finishes_publication_and_retires(self):
        runtime = self.prepare()
        self.release()
        runtime.host.check_runs = [_check("unit", conclusion="failure", run="901")]
        runtime.sprint_writer.fail_next = HostError("sprint reply unavailable")
        self.assertEqual(self.reconcile()[0]["status"], "degraded")
        self.payload = json.loads(json.dumps(runtime.saved[-1]))
        watch = self.payload[post_merge.WATCHES_KEY]["demo-1"]
        self.assertFalse(watch.get("published"))
        disposed = self.dispose(self.facts()[-1]["event_id"], action="hotfix", follow_up="demo-94")
        settled = runtime.writer.settle_post_merge_ci(
            actor=runtime.owner, reference="demo-1", merge=post_merge.release_merge_marker(watch),
            result_request_id=post_merge._request_id(watch, "card"), request_id=post_merge._request_id(watch, "await"))
        self.assertEqual(settled["action"], "unchanged")
        with self.assertRaises(TaskError):
            runtime.writer.settle_post_merge_ci(
                actor=runtime.owner, reference="demo-1", merge={**post_merge.release_merge_marker(watch), "merge_sha": OTHER_SHA},
                result_request_id=post_merge._request_id(watch, "card"), request_id=post_merge._request_id(watch, "await"))
        outcomes = self.reconcile()
        self.assertEqual((outcomes[-1]["status"], outcomes[-1]["result"]), ("ok", "red"))
        self.assertEqual(self.payload[post_merge.WATCHES_KEY], {})
        self.assertEqual(runtime.reader.show("demo-1")["state"], "done")
        self.assertEqual(self.transitions()[-1]["event_id"], disposed["event_id"])
        self.assertEqual((len(self.facts()), len(runtime.sprint_writer.events)), (1, 1))
        self.assertNotIn("e2e", runtime.reader.show("demo-1"))
        self.assertEqual(self.reconcile(), [])

    def test_the_result_wake_names_the_disposition_handle_and_the_disposition_wakes_nobody(self):
        runtime = self.prepare()
        event_id = self.wait_on("timeout")
        wake = observer_wake_context(runtime.audit.events("demo-1"))
        self.assertIn(f"--result-event {event_id}", wake)
        self.dispose(event_id)
        events = runtime.audit.events("demo-1")
        released = events.index(self.transitions()[-2])
        self.assertEqual([event["event_id"] for event in events[released:]
                          if is_significant_card_event(event, linked_refs={"demo-1"})], [event_id])
        runtime.host.check_runs = [_check("unit")]
        green = post_merge.wake_facts([{**self.facts()[-1], "payload": {
            **self.facts()[-1]["payload"], "post_merge_ci": {**self.facts()[-1]["payload"]["post_merge_ci"],
                                                              "result": "green"}}}])
        self.assertNotIn("dispose-post-merge", render_observer_wake_context(
            {"ref": "sprint:1", "comments": []}, change="linked-card", post_merge=green))

    def test_the_cli_drives_the_writer_and_replays_its_request_id(self):
        from ummanu.cli import build_parser

        self.prepare()
        event_id = self.wait_on("red")
        (self.root / "reason.md").write_text("tracked in the product backlog", encoding="utf-8")

        def run(*extra):
            argv = ["task", "dispose-post-merge", "--ref", "demo-1", "--result-event", event_id,
                    "--data-dir", str(self.root), "--reason-file", str(self.root / "reason.md"), *extra]
            args = build_parser().parse_args(argv)
            out, err = io.StringIO(), io.StringIO()
            with (mock.patch("ummanu.task_commands._instance", return_value=str(self.root)),
                  mock.patch("ummanu.task_commands.card_client", return_value=self.store),
                  contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
                code = args.handler(args)
            return code, json.loads(out.getvalue() or err.getvalue())

        code, refused = run("--role", "worker", "--actor", "worker", "--action", "issue",
                            "--follow-up", ISSUE, "--request-id", "cli-worker")
        self.assertEqual((code, refused["error"]["code"]), (3, "role_forbidden"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["task", "dispose-post-merge", "--ref", "demo-1", "--role", "observer",
                                       "--result-event", event_id, "--action", "issue", "--reason", "r"])
        for replayed in (False, True):
            code, answer = run("--role", "observer", "--actor", "observer", "--action", "issue",
                               "--follow-up", ISSUE, "--request-id", "cli-disposition")
            self.assertEqual((code, answer["action"], answer["replayed"]), (0, "disposed", replayed))
        done = self.transitions()[-1]
        self.assertEqual(done["data"]["post_merge_disposition"],
                         {"action": "issue", "reason": "tracked in the product backlog", "ref": ISSUE})
        self.assertEqual(sum(recorded_card_transition(event)[1] == "done" for event in self.transitions()), 1)


def observer_wake_context(events):
    return render_observer_wake_context({"ref": "sprint:1", "comments": []}, change="linked-card",
                                        post_merge=post_merge.wake_facts(events))

class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_pending_while_ci_runs_then_exactly_one_green_wake(self) -> None:
        host = _GhHost(check_runs=[_check("unit", status="in_progress", conclusion=None), _check("lint")])
        runtime = _runtime(host)
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        stream = [_done(release_merge={"base": "main", "merge_sha": SHA, "path": "github-pr"})]
        self.assertEqual(post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1100.0), [])
        self.assertEqual(_significant(stream + runtime.writer.events), [])
        host.check_runs = [_check("unit"), _check("lint", run="502")]
        outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1200.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["green"])
        self.assertEqual(payload[post_merge.WATCHES_KEY], {})
        # Later ticks have nothing left to say.
        self.assertEqual(post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1300.0), [])
        woken = _significant(stream + runtime.writer.events + runtime.sprint_writer.events)
        self.assertEqual(len(woken), 1)
        fact = woken[0]["payload"]["post_merge_ci"]
        self.assertEqual(fact["result"], "green")
        self.assertEqual(sorted(run["id"] for run in fact["runs"]), ["501", "502"])
        self.assertEqual(fact["merge_sha"], SHA)
        # The sprint's evidence: one dispatcher comment naming card, commit, result and run.
        [sprint_comment] = runtime.sprint_writer.events
        self.assertEqual(sprint_comment["ref"], SPRINT)
        for needle in (REF, SHA, "GREEN", f"https://github.com/{REPO}/actions/runs/501"):
            self.assertIn(needle, sprint_comment["body"])
        # The full 40-hex merge commit, in the sprint comment and in the card event text alike.
        self.assertIn(f"`{SHA}`", sprint_comment["body"])
        self.assertIn(f"`{SHA}`", woken[0]["body"])

    def test_red_carries_runs_failed_checks_and_the_gate_classification(self) -> None:
        infra_log = (
            "build\tSet up job\tGetting action download info\n"
            "build\tSet up job\t##[error]Failed to download action 'x'. Response status code does not "
            "indicate success: 503 (Service Unavailable).\n"
        )
        product_log = "unit\tRun tests\t##[error]AssertionError: 1 != 2\n"
        table = [
            ("product", {"777": product_log}, [_check("unit", conclusion="failure", run="777")], "product"),
            ("infrastructure", {"778": infra_log}, [_check("build", conclusion="failure", run="778")], "infrastructure"),
            (
                "mixed is product",
                {"777": product_log, "778": infra_log},
                [_check("unit", conclusion="failure", run="777"), _check("build", conclusion="failure", run="778")],
                "product",
            ),
        ]
        for label, logs, failing, classification in table:
            with self.subTest(label):
                host = _GhHost(check_runs=[_check("lint", run="776"), *failing], logs=logs)
                runtime = _runtime(host)
                payload: dict = {}
                _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
                post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1100.0)
                [event] = _significant(runtime.writer.events)
                fact = event["payload"]["post_merge_ci"]
                self.assertEqual(fact["result"], "red")
                self.assertEqual(fact["classification"], classification)
                self.assertEqual(fact["failed_checks"], sorted(item["name"] for item in failing))
                self.assertIn("777" if "777" in logs else "778", [run["id"] for run in fact["runs"]])
                text = render_observer_wake_context(
                    {"ref": SPRINT, "comments": []}, change="linked-card", post_merge=[fact]
                )
                self.assertIn(f"Post-merge CI RED for {REF}", text)
                self.assertIn("not a plain Done", text)
                self.assertIn(f"classification: {classification}", text)
                self.assertIn(fact["runs"][0]["id"], text)
                self.assertIn(f"https://github.com/{REPO}/commit/{SHA}", text)

    def test_absent_is_decided_at_once_without_asking_github(self) -> None:
        table = [
            ("local CI project", "local", PUSH_MAIN),
            ("no push trigger at all", "github", "on:\n  pull_request:\njobs: {}\n"),
            ("push to another branch only", "github", "on:\n  push:\n    branches: [release/*]\njobs: {}\n"),
            ("push of tags only", "github", "on:\n  push:\n    tags: ['v*']\njobs: {}\n"),
        ]
        for label, ci, workflow in table:
            with self.subTest(label):
                runtime = _runtime(_NoCallHost())
                payload: dict = {}
                watch = _watch(payload, ci=ci, workspace=_workspace(self.root / label.replace(" ", "-"), workflow))
                self.assertTrue(watch["absent_reason"])
                outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1000.0)
                self.assertEqual([outcome["result"] for outcome in outcomes], ["absent"])
                [event] = _significant(runtime.writer.events)
                self.assertEqual(event["payload"]["post_merge_ci"]["result"], "absent")

    def test_a_push_trigger_or_an_unreadable_workflow_set_is_not_absent(self) -> None:
        for label, workflow in (
            ("push to main", PUSH_MAIN),
            ("bare push", "on: [push]\njobs: {}\n"),
            ("unparsable", "on: [push\n"),
            ("no workflow files", None),
        ):
            with self.subTest(label):
                watch = _watch({}, workspace=_workspace(self.root / label.replace(" ", "-"), workflow))
                self.assertEqual(watch["absent_reason"], "")

    def test_the_ceiling_resolves_timeout_with_its_env_override(self) -> None:
        host = _GhHost(check_runs=[_check("unit", status="queued", conclusion=None)])
        with mock.patch.dict(os.environ, {post_merge.CEILING_ENV: "120"}):
            runtime = _runtime(host)
            payload: dict = {}
            _watch(payload, workspace=_workspace(self.root, PUSH_MAIN), now=1000.0)
            self.assertEqual(post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1119.0), [])
            outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1120.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["timeout"])
        [event] = _significant(runtime.writer.events)
        fact = event["payload"]["post_merge_ci"]
        self.assertEqual(fact["runs"][0]["id"], "501")
        self.assertIn("2 min", fact["reason"])
        self.assertEqual(post_merge.DEFAULT_CEILING_SECONDS, 3600)
        with mock.patch.dict(os.environ, {post_merge.CEILING_ENV: "not-a-number"}):
            self.assertEqual(post_merge.ceiling_seconds(), 3600)

    def _never_green_until_timeout(self, host: _GhHost) -> None:
        runtime = _runtime(host)
        payload: dict = {}
        watch = _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        for now in (1001.0, 2000.0, 4599.0):
            self.assertEqual(post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=now), [])
        self.assertTrue(watch["last_state"])
        outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=4600.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["timeout"])
        self.assertEqual(
            [event["payload"]["post_merge_ci"]["result"] for event in runtime.writer.events], ["timeout"]
        )

    def test_one_hostile_endpoint_holds_the_reading_whatever_the_other_says(self) -> None:
        """The product of both endpoints: one green and well-formed, the other hostile. Every cell
        is pending until the ceiling, then `timeout`, never green."""
        green_runs = [_check("unit")]
        green_status = [{"context": "ci/external", "state": "success"}]
        green_status_object = {"sha": SHA, "total_count": 1, "statuses": green_status}
        hostile_check_runs = {
            "empty": "",
            "non-JSON": "{not json",
            "HTML": "<html><body>rate limited</body></html>",
            "wrong shape": {"message": "ok", "check_runs": "none"},
            "missing SHA": {"total_count": 1, "check_runs": [_check("unit", head_sha=None)]},
            "wrong SHA": {"total_count": 1, "check_runs": [_check("unit", head_sha=OTHER_SHA)]},
            "null conclusion": {"total_count": 1, "check_runs": [_check("unit", conclusion=None)]},
            "transport error": HostError("gh: connection reset"),
            "5xx": _completed("", 1, "gh: Server Error (HTTP 502)"),
            "404": _completed("", 1, "gh: Not Found (HTTP 404)"),
        }
        hostile_status = {
            "empty": "",
            "non-JSON": "{not json",
            "HTML": "<html><body>rate limited</body></html>",
            "wrong shape": {"sha": SHA, "total_count": 1, "statuses": "none"},
            "missing SHA": {"total_count": 1, "statuses": green_status},
            "wrong SHA": {"sha": OTHER_SHA, "total_count": 1, "statuses": green_status},
            "null conclusion": {"sha": SHA, "total_count": 1, "statuses": [{"context": "ci/external", "state": None}]},
            "transport error": HostError("gh: connection reset"),
            "5xx": _completed("", 1, "gh: Server Error (HTTP 502)"),
            "404": _completed("", 1, "gh: Not Found (HTTP 404)"),
        }
        self.assertEqual(set(hostile_check_runs), set(hostile_status))
        for kind, answer in hostile_check_runs.items():
            with self.subTest(hostile="check-runs", kind=kind):
                self._never_green_until_timeout(_GhHost(check_runs=answer, status=green_status_object))
        for kind, answer in hostile_status.items():
            with self.subTest(hostile="status", kind=kind):
                self._never_green_until_timeout(_GhHost(check_runs=green_runs, status=answer))
        # The control: both endpoints green and well-formed is green.
        runtime = _runtime(_GhHost(check_runs=green_runs, status=green_status))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        self.assertEqual(
            [o["result"] for o in post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1001.0)],
            ["green"],
        )

    def test_a_dynamic_workflow_run_is_not_a_check_of_the_merge(self) -> None:
        """A failed Dependabot Updates job beside a green push CI is green; an unreadable list of the
        dynamic runs holds the reading."""
        checks = [_check("unit"), _check("Dependabot", conclusion="failure", run="777")]
        runtime = _runtime(_GhHost(check_runs=checks, dynamic_runs=[777]))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1001.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["green"])
        self.assertEqual([run["id"] for run in runtime.writer.events[0]["payload"]["post_merge_ci"]["runs"]], ["501"])

        self._never_green_until_timeout(_GhHost(check_runs=checks, dynamic_runs=HostError("gh: connection reset")))

    def test_check_runs_are_judged_only_across_every_page(self) -> None:
        """Page 1 is all green; a later page holds a pending and then a failed check. Partial pages
        are pending, the complete set is red, and nothing is ever green."""
        first_page = [_check(f"check-{index:03d}") for index in range(100)]
        later_pages = [
            ("the later page is missing from the answer", {"total_count": 101, "check_runs": []}),
            ("the later page is unreadable", HostError("gh: connection reset")),
            ("page 1 alone claims to be all", {"total_count": 100, "check_runs": []}),
            ("the later check is still running", {"total_count": 101, "check_runs": [_check("check-100", status="in_progress", conclusion=None)]}),
            ("the later check failed", {"total_count": 101, "check_runs": [_check("check-100", conclusion="failure", run="990")]}),
        ]
        runtime = _runtime(_GhHost(logs={"990": "check-100\tRun tests\t##[error]boom\n"}))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        results = []
        for tick, (label, later) in enumerate(later_pages, start=1):
            with self.subTest(label):

                def page(number: int, later: Any = later) -> Any:
                    return {"total_count": 101, "check_runs": first_page} if number == 1 else later

                runtime.host.check_runs = page
                outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1000.0 + tick)
                results.append([outcome["result"] for outcome in outcomes])
        self.assertEqual(results, [[], [], [], [], ["red"]])
        [event] = runtime.writer.events
        fact = event["payload"]["post_merge_ci"]
        self.assertEqual(fact["failed_checks"], ["check-100"])
        self.assertIn("990", [run["id"] for run in fact["runs"]])
        pages_asked = [call[2] for call in runtime.host.calls if call[:2] == ["gh", "api"] and "/check-runs" in call[2]]
        self.assertTrue(all("per_page=100" in path for path in pages_asked))
        self.assertIn("page=2", pages_asked[-1])

    def test_only_read_merge_ci_rolls_up_or_says_green(self) -> None:
        """The one enforcement place, checked on the source: no other function of the watch calls
        `_rollup`, and the only terminal `CiReading` values are built inside `read_merge_ci`."""
        import ast
        import inspect

        tree = source_trees.parse(inspect.getsource(post_merge))
        rollups, terminal = set(), set()
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_rollup":
                    rollups.add(function.name)
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "CiReading"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in {"green", "red"}
                ):
                    terminal.add(function.name)
        self.assertEqual(rollups, {"read_merge_ci"})
        self.assertEqual(terminal, {"read_merge_ci"})

    def test_a_required_check_that_never_ran_holds_the_watch(self) -> None:
        runtime = _runtime(_GhHost(check_runs=[_check("lint")], required=["unit"]))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        self.assertEqual(post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1001.0), [])

    def test_a_terminal_failure_is_red_while_another_required_check_was_never_posted(self) -> None:
        checks = [_check("fast", conclusion="failure")]
        runtime = _runtime(_GhHost(check_runs=checks, required=["fast", "matrix"]))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1001.0)
        [event] = _significant(runtime.writer.events)
        fact = event["payload"]["post_merge_ci"]
        self.assertEqual((fact["result"], fact["failed_checks"]), ("red", ["fast"]))

    def test_a_check_with_conclusion_and_completed_at_is_terminal_whatever_its_status(self) -> None:
        ghost = {**_check("detect", status="in_progress"), "completed_at": "2026-10-06T20:14:33Z"}
        runtime = _runtime(_GhHost(check_runs=[ghost]))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1001.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["green"])

    def test_a_conclusion_outside_the_github_vocabulary_holds_the_watch(self) -> None:
        runtime = _runtime(_GhHost(check_runs=[_check("unit", conclusion="mystery")]))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        self.assertEqual(post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1001.0), [])

    def test_an_unknown_merge_commit_is_read_from_the_pull_request_later(self) -> None:
        host = _GhHost(check_runs=[_check("unit")], merge_commit="")
        runtime = _runtime(host)
        payload: dict = {}
        watch = _watch(payload, workspace=_workspace(self.root, PUSH_MAIN), sha="")
        self.assertEqual(post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1001.0), [])
        host.merge_commit = SHA
        outcomes = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1002.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["green"])
        self.assertEqual(watch["merge_sha"], SHA)


class RestartAndReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_restart_mid_watch_keeps_it_and_its_start(self) -> None:
        host = _GhHost(check_runs=[_check("unit", status="in_progress", conclusion=None)])
        runtime = _runtime(host)
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN), now=1000.0)
        post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1100.0)
        restored = json.loads(json.dumps(payload))
        self.assertEqual(restored[post_merge.WATCHES_KEY][REF]["started_at"], 1000.0)
        host.check_runs = [_check("unit")]
        outcomes = post_merge.reconcile_post_merge_watches(runtime, restored, {}, now=1200.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["green"])
        self.assertEqual(len(_significant(runtime.writer.events)), 1)

    def test_a_crash_between_recording_and_wake_replays_one_record_and_one_wake(self) -> None:
        host = _GhHost(check_runs=[_check("unit", conclusion="failure", run="901")],
                       logs={"901": "unit\tRun tests\t##[error]boom\n"})
        runtime = _runtime(host)
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        # The dispatcher dies after the card event, before the sprint comment.
        runtime.sprint_writer.fail_next = KeyboardInterrupt("dispatcher killed")
        with self.assertRaises(KeyboardInterrupt):
            post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1100.0)
        on_disk = runtime.saved[-1]
        self.assertEqual(on_disk[post_merge.WATCHES_KEY][REF]["result"]["result"], "red")
        # After the restart the base went green (a rerun), but the recorded fact is the one published.
        host.check_runs = [_check("unit", run="902")]
        outcomes = post_merge.reconcile_post_merge_watches(runtime, on_disk, {}, now=1200.0)
        self.assertEqual([outcome["result"] for outcome in outcomes], ["red"])
        self.assertEqual(on_disk[post_merge.WATCHES_KEY], {})
        self.assertEqual(len(runtime.writer.events), 1)
        self.assertEqual(len(runtime.sprint_writer.events), 1)
        [woken] = _significant(runtime.writer.events + runtime.sprint_writer.events)
        self.assertEqual(woken["payload"]["post_merge_ci"]["runs"][0]["id"], "901")

    def test_a_board_refusal_keeps_the_result_for_the_next_tick(self) -> None:
        runtime = _runtime(_GhHost(check_runs=[_check("unit")]))
        payload: dict = {}
        _watch(payload, workspace=_workspace(self.root, PUSH_MAIN))
        runtime.writer.fail_next = TaskError("backend_error", "board store unavailable", 1)
        [degraded] = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1100.0)
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(payload[post_merge.WATCHES_KEY][REF]["result"]["result"], "green")
        [done] = post_merge.reconcile_post_merge_watches(runtime, payload, {}, now=1200.0)
        self.assertEqual(done["result"], "green")
        self.assertEqual(len(_significant(runtime.writer.events)), 1)


class WakeTextTests(unittest.TestCase):
    def test_each_result_is_stated_as_itself(self) -> None:
        run = [{"id": "31", "url": f"https://github.com/{REPO}/actions/runs/31"}]
        for result in ("green", "red", "absent", "timeout"):
            with self.subTest(result):
                fact = {"result": result, "card": REF, "base": "main", "merge_sha": SHA, "runs": run,
                        "failed_checks": ["unit"], "classification": "product"}
                text = render_observer_wake_context(
                    {"ref": SPRINT, "comments": []}, change="linked-card", post_merge=[fact]
                )
                self.assertIn(f"Post-merge CI {result.upper()} for {REF}", text)
                self.assertIn("31", text)
                self.assertEqual("failed checks: unit" in text, result == "red")

    def test_a_wake_without_a_result_is_unchanged(self) -> None:
        sprint = {"ref": SPRINT, "comments": []}
        self.assertEqual(
            render_observer_wake_context(sprint, change="linked-card", post_merge=[]),
            render_observer_wake_context(sprint, change="linked-card"),
        )


class _MergeHost(CommandHostRuntime):
    """`complete_green`'s two merge paths with every command recorded, none run."""

    def __init__(self, root: Path, *, ci: str, instance_repo: bool = False) -> None:
        catalog = SimpleNamespace(
            adapter=lambda project: {"validation": {"ci": ci}},
            integration_base=lambda project, override: "main",
            binding=lambda project: {"repo": str(root / ("instance" if instance_repo else "project"))},
            instance_dir=str(root / "instance"),
            project_default_branch=lambda project: "main",
        )
        super().__init__(catalog, root, mode="real", production_runtime=registered_production_runtime(root))  # type: ignore[arg-type]
        self.runs: list[list[str]] = []

    def _decide_workspace_environment_ownership(self, workspace: str) -> str:
        return ""

    def _require_production_runtime(self, phase: str) -> None:
        return None

    def _remote_git_checked(self, project, checkout, args, label):  # type: ignore[override]
        self.runs.append(["git", *args])

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self.runs.append(list(args))
        if args[:3] == ["gh", "pr", "view"] and "baseRefName,isDraft" in args:
            return _completed(json.dumps({"baseRefName": "main", "isDraft": False}))
        if args[:3] == ["gh", "pr", "view"] and "mergeCommit" in args:
            return _completed(SHA + "\n")
        if args[-2:] == ["rev-parse", BRANCH]:
            return _completed(OTHER_SHA + "\n")
        return _completed()


class MergePathLandingTests(unittest.TestCase):
    """Each of the two merge paths hands the release a landing, so each reaches the predicate."""

    def test_every_merge_path_reports_what_landed(self) -> None:
        table = [
            ("github pull request", "github", False, MergeLanding(SHA, "main", "github-pr", "github", BRANCH)),
            ("local-CI push", "local", False, MergeLanding(OTHER_SHA, "main", "push", "local", BRANCH)),
        ]
        for label, ci, instance, expected in table:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "instance").mkdir()
                (root / "project").mkdir()
                host = _MergeHost(root, ci=ci, instance_repo=instance)
                record = SimpleNamespace(workspace=str(root / "ws"))
                landing = host.complete_green({"ref": REF, "project": "sample", "workspace": {}}, record)
                self.assertEqual(landing, expected)

    def test_landed_commit_recovery_only_reads_the_original_merge_identity(self) -> None:
        for path, expected, command in (
            ("github-pr", SHA, ["gh", "pr", "view", BRANCH, "--json", "mergeCommit", "-q", ".mergeCommit.oid"]),
            ("push", OTHER_SHA, ["git", "-C", "/candidate", "rev-parse", BRANCH]),
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                host = _MergeHost(Path(tmp), ci="github" if path == "github-pr" else "local")
                actual = host.landed_merge_commit(SimpleNamespace(workspace="/candidate"), path=path, branch=BRANCH)
                self.assertEqual((actual, host.runs), (expected, [command]))

    def test_a_card_on_the_live_root_lands_nothing(self) -> None:
        """The instance-repository landing was the third path; it is gone, on either CI mode."""
        for ci in ("local", "github"):
            with self.subTest(ci), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "instance").mkdir()
                host = _MergeHost(root, ci=ci, instance_repo=True)
                record = SimpleNamespace(workspace=str(root / "ws"))
                with self.assertRaisesRegex(HostError, "nothing was merged: .*names the live root"):
                    host.complete_green({"ref": REF, "project": "sample", "workspace": {}}, record)
                self.assertEqual(host.runs, [])

    def test_automerge_off_lands_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"UMMANU_DISPATCHER_AUTOMERGE": "off"}
        ):
            host = _MergeHost(Path(tmp), ci="github")
            record = SimpleNamespace(workspace=str(Path(tmp) / "ws"))
            self.assertIsNone(host.complete_green({"ref": REF, "project": "sample", "workspace": {}}, record))


if __name__ == "__main__":
    unittest.main()
