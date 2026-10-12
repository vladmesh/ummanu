"""The post-merge CI watch: a release wakes the observer on the base's CI result, not on its merge.

Unit level, no Docker and no network: `gh` is a scripted host, the board is a recording writer
whose request ids are idempotent the way the audit's are, and the observer significance predicate
(`ummanu.tasks.is_significant_observer_event`) is the judge of what wakes the observer.
"""

from __future__ import annotations

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

from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.board.models import Actor, CardState, EntityKind, Event
from ummanu.board.transitions import transition_for
from ummanu.dispatch import post_merge, release_lifecycle
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.observer import render_observer_wake_context
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError, MergeLanding
from ummanu.tasks import (
    RELEASE_MERGE_KEY,
    TaskError,
    is_significant_card_event,
    is_significant_observer_event,
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
        "Observer decision: release.",
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
    status for the merge commit; anything else is its raw answer.
    """

    def __init__(self, *, check_runs: Any = (), status: Any = (), logs: dict[str, str] | None = None,
                 required: list[str] | None = None, merge_commit: str = SHA) -> None:
        self.check_runs = check_runs
        self.status = status
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
    return post_merge.open_watch(task, payload, landing, workspace=workspace, now=now)


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
            step="assessment", move_reason="Observer decision: release.", decision="release",
        )

    def test_a_merge_records_a_durable_watch_before_done_and_marks_the_done(self) -> None:
        self.runtime.host.complete_green.return_value = MergeLanding(
            sha=SHA, base="main", path="github-pr", ci="github", branch=BRANCH
        )
        outcome = self.release({"ref": REF, "type": "code", "project": "sample", "sprint": SPRINT})
        self.assertEqual(outcome["to"], "done")
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

    def test_a_research_release_opens_no_watch(self) -> None:
        with mock.patch.object(release_lifecycle, "missing_completion_evidence", return_value=""):
            self.release({"ref": REF, "type": "research", "project": "sample", "sprint": SPRINT})
        self.runtime.host.complete_green.assert_not_called()
        self.assertEqual(self.payload.get(post_merge.WATCHES_KEY) or {}, {})
        self.assertIsNone(self.accounting.terminal_effect.call_args.kwargs.get("release_merge"))


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

        tree = ast.parse(inspect.getsource(post_merge))
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
        if args[:3] == ["gh", "pr", "view"] and "baseRefName" in args:
            return _completed("main\n")
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
            ("local-CI push", "local", False, MergeLanding(OTHER_SHA, "main", "push", "local")),
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
