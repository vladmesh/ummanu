"""secretary-1722: a dispatcher record written on Orca is refused by every verb, and blocks its card.

The dispatcher host places every card in a git worktree and raises every head on `local-pty`. A
record from before that — its workspace an Orca worktree under the Orca workspaces root, or a head
run that is a legacy record (`orca-legacy`, or no runtime at all) — still loads. What these tests
hold is that nothing acts on it: launch, delivery, stop and teardown all raise one typed error,
`LegacyDispatcherRecord`, before any child process runs and before the head's backend is asked for
anything; and that the dispatcher turns that refusal into a Blocked card whose reason names the
record, rather than retrying it as a host fault or re-placing the card silently.

The observer's own verbs are held the same way in `tests.test_observer_git_workspace`.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.dispatcher_fixtures import DispatcherRuntimeFixture, SupervisedBackend, supervised_run
from tests.fakes.dispatcher import FakeCatalog
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import CAUSE_WORKSPACE_CONTRACT, FAILURE_CLASS_TASK, classify_bring_up_failure
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import LegacyDispatcherRecord
from ummanu.runtime.head import operations as head_ops
from ummanu.runtime.head_runtimes import ORCA_LEGACY_RUNTIME

REF = "ummanu-1722"
WORKER = f"{REF}-worker"
TASK = {"ref": REF, "project": "ummanu", "description": "d", "workspace": {"base_branch": "main"}}


def _legacy_run(run_id: str, *, role: str, runtime: str) -> dict[str, Any]:
    """A durable run as the Orca era wrote it: `orca-legacy` by name, or no runtime key at all."""
    run = head_ops.HeadRun(
        run_id=run_id,
        spec=head_ops.HeadSpec(profile_id="codex", adapter="codex", runtime=runtime or ORCA_LEGACY_RUNTIME),
        workspace="",
        task_ref=head_ops.TaskRef.card(REF),
        role=role,
        handle=f"term:{run_id}",
        leaf=f"leaf:{run_id}",
    ).to_json()
    if not runtime:
        run.pop("head_runtime", None)
    return run


class HostRefusesLegacyRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.orca_root = self.root / "orca-workspaces"
        env = mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.orca_root)})
        env.start()
        self.addCleanup(env.stop)
        self.host = CommandHostRuntime(  # type: ignore[arg-type]
            FakeCatalog(),
            self.root / "data",
            mode="real",
            production_runtime=registered_production_runtime(self.root),
        )
        self.backend = SupervisedBackend().install(self.host)
        self.children: list[list[str]] = []
        for name in ("_run", "run_capture"):
            patched = mock.patch.object(
                self.host, name, side_effect=lambda args, *_a, **_k: self.children.append(list(args))
            )
            patched.start()
            self.addCleanup(patched.stop)
        self.git_workspace = self.root / "data" / "workspaces" / "ummanu" / WORKER
        self.orca_workspace = self.orca_root / "ummanu" / WORKER
        for workspace in (self.git_workspace, self.orca_workspace):
            workspace.mkdir(parents=True)

    def _record(self, workspace: Path, **fields: Any) -> DispatcherRecord:
        record = DispatcherRecord(
            worker=WORKER,
            workspace=str(workspace),
            handle="term:worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
            worker_run={"adapter": "codex", "codex_mode": "tui"},
            review_handle="term:reviewer",
        )
        for name, value in fields.items():
            setattr(record, name, value)
        return record

    def fixtures(self) -> dict[str, tuple[DispatcherRecord, str]]:
        """Each shape of legacy record, and what its refusal has to name."""
        supervised = {
            "worker_head_run": supervised_run("run-w", task_ref=head_ops.TaskRef.card(REF)),
            "review_head_run": supervised_run("run-r", role="reviewer", task_ref=head_ops.TaskRef.card(REF)),
        }
        return {
            "an Orca worktree": (self._record(self.orca_workspace, **supervised), str(self.orca_workspace)),
            "an orca-legacy worker run": (
                self._record(
                    self.git_workspace,
                    worker_head_run=_legacy_run("run-w", role="worker", runtime=ORCA_LEGACY_RUNTIME),
                    review_head_run=supervised["review_head_run"],
                ),
                "worker head run run-w",
            ),
            "a reviewer run with no runtime": (
                self._record(
                    self.git_workspace,
                    worker_head_run=supervised["worker_head_run"],
                    review_head_run=_legacy_run("run-r", role="reviewer", runtime=""),
                ),
                "reviewer head run run-r",
            ),
        }

    def verbs(self, record: DispatcherRecord) -> dict[str, Any]:
        intent = {"role": "review", "head_run": dict(record.review_head_run), "workspace": record.workspace}
        return {
            "launch: rework": lambda: self.host.restart_worker(TASK, record),
            "launch: reviewer": lambda: self.host.start_review(TASK, record),
            "deliver: report prompt": lambda: self.host.prompt_worker_report(TASK, record),
            "deliver: retained continuation": lambda: self.host.resume_worker(TASK, record),
            "deliver: retained reviewer nudge": lambda: self.host.nudge_review_delivery(TASK, record, intent),
            "stop: worker": lambda: self.host.stop_head(record, "worker"),
            "stop: reviewer": lambda: self.host.stop_review(record),
            "stop: freeze": lambda: self.host.freeze_worker(record),
            "stop: workspace": lambda: self.host.stop_workspace(record),
            "tear down": lambda: self.host.teardown(record),
        }

    def test_every_verb_refuses_every_legacy_record_before_anything_runs(self) -> None:
        for shape, (record, named) in self.fixtures().items():
            before = record.to_json()
            for verb, call in self.verbs(record).items():
                with self.subTest(record=shape, verb=verb):
                    with self.assertRaises(LegacyDispatcherRecord) as refused:
                        call()
                    self.assertIn("legacy dispatcher record", str(refused.exception))
                    self.assertIn(named, str(refused.exception))
                    self.assertEqual(refused.exception.bring_up_cause, CAUSE_WORKSPACE_CONTRACT)
            self.assertEqual(record.to_json(), before, f"{shape}: the record was rewritten")
        self.assertEqual(self.children, [], "no child process ran, and so no `orca` argv either")
        self.assertEqual(
            (self.backend.starts, self.backend.deliveries, self.backend.stops), ([], [], []),
            "the head's backend was never asked",
        )
        self.assertTrue(self.orca_workspace.is_dir(), "an Orca worktree is never torn down")

    def test_the_best_effort_stop_absorbs_the_refusal_and_stops_nothing(self) -> None:
        for shape, (record, _named) in self.fixtures().items():
            with self.subTest(record=shape):
                self.assertIsNone(self.host.stop(record))
        self.assertEqual((self.children, self.backend.stops), ([], []))

    def test_a_record_that_is_not_legacy_is_driven(self) -> None:
        """The control: the same verbs on a supervised record in its git worktree reach the backend."""
        record = self._record(
            self.git_workspace,
            worker_head_run=supervised_run("run-w", task_ref=head_ops.TaskRef.card(REF)),
            review_head_run=supervised_run("run-r", role="reviewer", task_ref=head_ops.TaskRef.card(REF)),
        )

        self.host.stop_head(record, "worker")
        self.host.stop_review(record)

        self.assertEqual([run_id for run_id, _actor in self.backend.stops], ["run-w", "run-r"])

    def test_the_refusal_is_a_card_contract_failure_not_a_host_fault(self) -> None:
        record, _named = self.fixtures()["an Orca worktree"]
        with self.assertRaises(LegacyDispatcherRecord) as refused:
            self.host.restart_worker(TASK, record)

        failure = classify_bring_up_failure(refused.exception, record, "worker", stage="rework", attempt_id="a1")

        self.assertEqual(failure.failure_class, FAILURE_CLASS_TASK)
        self.assertEqual(failure.cause, CAUSE_WORKSPACE_CONTRACT)


class LegacyRecordBlocksTheCardTests(DispatcherRuntimeFixture, unittest.TestCase):
    """What the dispatcher does with the refusal: the card goes Blocked, with the reason on it."""

    def _record_of(self, ref: str = "ummanu-510") -> DispatcherRecord:
        return self.runtime.production_state.records(self.runtime.production_state.load())[ref]

    def refusal(self) -> LegacyDispatcherRecord:
        """The refusal exactly as the real host raises it for this card's record."""
        record = self._record_of()
        orca = Path(self.data_dir) / "orca-workspaces" / "ummanu" / record.worker
        record.workspace = str(orca)
        host = CommandHostRuntime(FakeCatalog(), Path(self.data_dir) / "host", mode="real")  # type: ignore[arg-type]
        with (
            mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_WORKSPACES_ROOT": str(orca.parents[1])}),
            self.assertRaises(LegacyDispatcherRecord) as refused,
        ):
            host.teardown(record)
        return refused.exception

    def test_a_rework_on_a_legacy_record_blocks_the_card_naming_the_record(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        refusal = self.refusal()
        self.host.fail_restart_error = refusal
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="red",
            body="fix it",
            request_id="review-red",
        )

        result = self._park_and_decide("rework")

        self.assertEqual(result["status"], "blocked")
        task = self.reader.show("ummanu-510")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("legacy dispatcher record", task["comments"][-1]["body"])
        self.assertIn(refusal.subject, task["comments"][-1]["body"])
        self.assertEqual(self.host.torn_down, [], "a legacy record is never torn down")

    def test_a_release_whose_teardown_is_refused_blocks_the_card_naming_the_record(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        refusal = self.refusal()
        self.host.teardown = mock.Mock(side_effect=refusal)  # type: ignore[method-assign]
        self.tick()
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="green",
            body="ok",
            request_id="review-green",
        )

        result = self._park_and_decide("accept")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "release cleanup refused")
        task = self.reader.show("ummanu-510")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("release cleanup refused: legacy dispatcher record", task["comments"][-1]["body"])
        self.assertIn(refusal.subject, task["comments"][-1]["body"])


if __name__ == "__main__":
    unittest.main()
