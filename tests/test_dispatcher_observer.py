"""Observer-head lifecycle: the production tick against the sprint board (secretary-793)."""

# ruff: noqa: SIM117

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
import tomllib
import traceback
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.sql_cards import BOARD_ID
from ummanu.dispatch import observer_fence as dispatcher_observer_fence
from ummanu.dispatch.cleanup import CleanupOwner
from ummanu.dispatch.heartbeat import heartbeat_identity
from ummanu.dispatch.host import CommandHostRuntime, InstanceCatalog
from ummanu.dispatch.launch import infrastructure_action
from ummanu.dispatch.observer import (
    EVENT_DEFERRED,
    EVENT_LAUNCHED,
    EVENT_RELAUNCHED,
    EVENT_STOPPED,
    DeliveryStage,
    ObserverDelivery,
    ObserverLaunchAborted,
    ObserverRecord,
    ObserverWakeLiveness,
    _observer_event_state,
    _wake_for_event,
    load_observers,
    observer_alive,
    observer_launch_prompt,
    observer_pid_file,
    observer_request_id,
    observer_snapshot,
    observer_wake_max_attempts,
    put_observers,
    reconcile_observers,
    render_observer_prompt,
    stop_observer_head,
)
from ummanu.dispatch.observer_fence import EVENT_CLEARED, EVENT_FENCED
from ummanu.dispatch.production import (
    _budget_event_type,
    _production_claim_ready,
    _reconcile_sprint_budget,
)
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.tui import (
    DeliveryEvidence,
    claude_project_dir_name,
    prepare_claude_provider_progress_source,
    provider_progress_for_run,
)
from ummanu.dispatch.types import HostError, LegacyDispatcherRecord
from ummanu.dispatch.watchdog import initial_output_stall_seconds
from ummanu.dispatch.worker_lifecycle import head_run_binding
from ummanu.head_health import HeadReadiness
from ummanu.head_registry import canonical_heads
from ummanu.infra import git_worktree
from ummanu.runtime.role_env import (
    OBSERVER_GENERATION_ENV,
    OBSERVER_SPRINT_ENV,
    ROLE_ALLOWLIST,
    declared_observer_sprint,
    observer_binding,
    runtime_env,
)
from ummanu.sprint_observer import encode_observer, head_choice
from ummanu.sprints import (
    BUDGET_UNCHARGED_INFRASTRUCTURE,
    SprintReader,
    SprintWriter,
)
from ummanu.status import _observers as status_observers
from ummanu.tasks import TaskError, TaskReader, TaskWriter, _now, task_audit_for
from tests.fakes.dispatcher import (
    FakeCatalog,
    FakeHost,
    TwoOpenSprintAdmission,
    dispatcher_seed,
)
from tests.fakes.observer import (
    DEAD_PID,
    install_skill_registry,
)
from tests.dispatcher_fixtures import SupervisedBackend, supervised_run
from tests.fanout_fixtures import accepted_transport_run
from tests.observer_identity import as_observer, bind_observer
from tests.production_runtime_fixtures import registered_production_runtime
from tests.retired_board import LEGACY_ENV, legacy_runtime_lines
from tests.sprint_close_fixtures import close_decisions, settle_dispatcher_work
from tests.sql_backend_fixtures import card_store
from ummanu.runtime import codex_preflight
from ummanu.runtime.codex_preflight import ensure_codex_workspace_trusted
from ummanu.observer_root import observer_root_repo
from ummanu.runtime.head import HEAD_BUSY, HEAD_GONE, DeliverReceipt, HeadCommand, HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head import operations as head_ops


@contextlib.contextmanager
def unfiltered_audit_reads_raise() -> Iterator[list[str]]:
    """Every audit read under this scope must name a reference set, a window or a page bound.

    No exemption: the budget pass reads a page of its uncharged candidate set (secretary-1661).

    secretary-1658: nothing the production tick runs may read the whole committed audit. Both
    audit owners are patched at the class, so a read by any instance under the scope is seen; a
    violation is recorded (the tick swallows some exceptions into its own outcomes) and raised.
    """
    from ummanu.board.sql_audit import SqlTaskAudit

    violations: list[str] = []
    patches = []
    for owner in (SqlTaskAudit,):
        events = owner.events
        projection = owner._occurrence_projection_records
        candidates = owner.uncharged_budget_candidates

        def guarded_events(self, reference="", *, _events=events, **filters):  # type: ignore[no-untyped-def]
            if (
                not reference
                and filters.get("references") is None
                and filters.get("since") is None
            ):
                violations.append("".join(traceback.format_stack(limit=8)))
                raise AssertionError("an unfiltered audit read under the tick")
            return _events(self, reference, **filters)

        def guarded_projection(self, kinds=None, *, _projection=projection, **options):  # type: ignore[no-untyped-def]
            if kinds is None:
                violations.append("".join(traceback.format_stack(limit=8)))
                raise AssertionError("an unfiltered occurrence projection under the tick")
            return _projection(self, kinds, **options)

        def guarded_candidates(self, *, limit, _candidates=candidates):  # type: ignore[no-untyped-def]
            if not isinstance(limit, int) or not 0 < limit <= 1000:
                violations.append("".join(traceback.format_stack(limit=8)))
                raise AssertionError("an unbounded budget candidate read under the tick")
            return _candidates(self, limit=limit)

        patches += [
            mock.patch.object(owner, "events", guarded_events),
            mock.patch.object(owner, "_occurrence_projection_records", guarded_projection),
            mock.patch.object(owner, "uncharged_budget_candidates", guarded_candidates),
        ]
    for patch in patches:
        patch.start()
    try:
        yield violations
    finally:
        for patch in reversed(patches):
            patch.stop()


class ObserverLifecycleTests(TwoOpenSprintAdmission, unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        self.observer_skill = install_skill_registry(self.data_dir)
        env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
                "UMMANU_ROLE_SKILLS_MANIFEST": str(self.data_dir / "registry" / "manifest.toml"),
                "UMMANU_INSTANCE": str(self.data_dir / "registry" / "instance"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        # These tests call the writers as the head of `sprint:1`, which is the head the dispatcher
        # launches here; a test acting as another sprint's head binds its own.
        bind_observer(self, "sprint:1")
        (self.data_dir / "bodies").mkdir(parents=True, exist_ok=True)
        self.board = card_store(self, dispatcher_seed(), instance_dir=self.data_dir)
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.host.audit = task_audit_for(self.board)
        self.audit = task_audit_for(self.board)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            self.audit,
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
        )
        self._board_comment = self.writer.comment
        self.writer.comment = self._comment_with_semantic_test_event  # type: ignore[method-assign]

    # helpers -----------------------------------------------------------------

    def _comment_with_semantic_test_event(self, **kwargs: object) -> dict:
        """Keep delivery tests explicit after generic dispatcher comments stopped waking heads."""
        result = self._board_comment(**kwargs)
        request_id = str(kwargs.get("request_id") or "")
        test_wake = (
            request_id.endswith("-event")
            or request_id.startswith(("burst-", "cursor-event-", "hung-event-", "event-of-"))
            or request_id in {"after-burst-resume", "replacement-event", "event-during-active-turn"}
        )
        if not test_wake:
            return result
        reference = str(kwargs["reference"])
        self.audit.append(
            request_id + ":semantic",
            {
                "event_id": "evt_" + request_id + "_semantic",
                "request_id": request_id + ":semantic",
                "ref": reference,
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher", "id": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": _now(),
            },
        )
        return result

    def open_sprint(self, reference: str = "sprint:1", **metadata: object) -> None:
        # Every row declares its observer, so the fixture declares one too: the registry default,
        # which is the head these tests expect the dispatcher to bring up.
        metadata.setdefault("sprint_observer", encode_observer(head_choice(self.catalog.observer_head())))
        self.board.add_sprint(reference, status="open", **metadata)

    def close_sprint(self, reference: str = "sprint:1") -> None:
        self.board.save_sprint_metadata(reference, sprint_status="closed")

    def observers(self) -> dict:
        return load_observers(self.runtime.production_state.load())

    def actions(self, result: dict, step: str = "observer-reconcile") -> list[dict]:
        return [action for action in result["actions"] if action.get("step") == step]

    def expire_launch_grace(self, reference: str = "sprint:1") -> None:
        """Age a launch intent past the window in which a missing pid still counts as alive."""
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers[reference].launched_at = time.time() - initial_output_stall_seconds() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def kill_observer(self, reference: str = "sprint:1") -> None:
        record = self.observers()[reference]
        path = Path(record.pid_file)
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat.update(
            {
                "pid": DEAD_PID,
                "boot_id": "dead-process",
                "proc_starttime_ticks": "0",
            }
        )
        path.write_text(json.dumps(heartbeat), encoding="utf-8")

    def acknowledge_delivery(
        self,
        entry: dict[str, str],
        *,
        request_id: str,
        reference: str = "sprint:1",
    ) -> None:
        delivery = self.observers()[reference].delivery
        # The acknowledgement is this sprint's own head answering for what it was sent.
        with as_observer(reference):
            SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                reference=reference,
                entry=entry,
                request_id=request_id,
                delivery_id=delivery.delivery_id,
                through_event=delivery.through_event,
            )

    def real_host_delivering(self) -> SupervisedBackend:
        """Wake through the real host's `nudge_observer`, onto a recording supervised backend.

        Everything else about the observer stays the fake host's: this is about what the real
        delivery path writes, hands over and raises, not about how its backend types.
        """
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")
        backend = SupervisedBackend().install(real_host)
        self.host.nudge_observer = real_host.nudge_observer  # type: ignore[method-assign]
        return backend

    def expire_wake_retry(self, reference: str = "sprint:1") -> None:
        """Age a deferred wake past its backoff so the next tick retries the delivery."""
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers[reference].delivery.next_at = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def age_delivery(self, seconds: float, reference: str = "sprint:1", *, deadline: bool = False) -> None:
        """Put this delivery's send and hold that far in the past, as a slow head would.

        The acknowledgement deadline is left where it is unless `deadline` says otherwise, so a
        test can age a turn without also expiring the clock it is not about.
        """
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        delivery = observers[reference].delivery
        delivery.sent_at -= seconds
        delivery.held_since -= seconds
        if deadline:
            delivery.deadline -= seconds
            delivery.next_at -= seconds
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def expire_launch_retry(self, reference: str = "sprint:1") -> None:
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers[reference].launch_next_at = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def install_observer_provider_source(self, *, precontract: bool = False) -> None:
        """Give the retained fake observer the same persisted source shape production reads."""
        payload = self.runtime.production_state.load()
        record = load_observers(payload)["sprint:1"]
        current = HeadRun.from_json(record.head_run)
        policy = accepted_transport_run(
            record.head,
            role="observer",
            workspace=record.workspace,
            task_ref=HeadRun.from_json(record.head_run).task_ref,
            pid_file=record.pid_file,
            run_id=current.run_id,
        ).fanout_policy
        source: dict[str, object] = {
            "version": 1,
            "kind": "codex_session_event_jsonl",
            "state": "unbound",
            "root": str(self.data_dir / "codex-sessions"),
            "baseline": [],
        }
        if not precontract:
            source.update(codex_preflight.codex_provider_source_descriptor(current))
        record.head_run = current.with_fanout_policy(
            {
                **policy,
                "provider_source_required": True,
                "provider_source": source,
            }
        ).to_json()
        put_observers(payload, {"sprint:1": record})
        self.runtime.production_state.save(payload)

    def install_observer_claude_provider_source(self) -> Path:
        """Give the retained observer a current Claude pre-pane source descriptor."""
        payload = self.runtime.production_state.load()
        record = load_observers(payload)["sprint:1"]
        current = HeadRun.from_json(record.head_run)
        claude_run = HeadRun(
            run_id=current.run_id,
            spec=HeadSpec(profile_id="claude-observer", adapter="claude", model="opus"),
            workspace=record.workspace,
            task_ref=TaskRef.sprint("sprint:1"),
            role="observer",
            pid_file=record.pid_file,
        )
        record.head_run = prepare_claude_provider_progress_source(claude_run).to_json()
        put_observers(payload, {"sprint:1": record})
        self.runtime.production_state.save(payload)
        return Path(str(record.head_run["fanout_policy"]["provider_progress_source"]["root"]))

    @staticmethod
    def observer_progress(record: ObserverRecord, cursor: str) -> dict[str, str]:
        run_id, fingerprint = head_run_binding(record.head_run)
        return {
            "state": "observed",
            "admission": "accepted",
            "source": "fake-observer-session",
            "source_fingerprint": "a" * 32,
            "cursor": cursor,
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }


    # the tick reads no whole audit (secretary-1658) ------------------------------

    def test_no_tick_caller_reads_the_whole_audit(self) -> None:
        """Claim, advance, budget, usage and outcome recovery, the observer launch and a wake."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        # A budget event, so the budget pass charges under the guard rather than finding nothing.
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="red",
            body="guarded budget event",
            request_id="guard-red-review",
        )
        with unfiltered_audit_reads_raise() as violations:
            launched = self.runtime.production_tick()
            self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
            self.writer.comment(
                role="dispatcher",
                actor="dispatcher",
                reference="ummanu-510",
                body="card changed",
                request_id="guard-event",
            )
            nudged = self.runtime.production_tick()
            again = self.runtime.production_tick()

        self.assertEqual(violations, [], "\n\n".join(violations))
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual([row["action"] for row in self.actions(nudged)], ["observer-nudged"])
        self.assertEqual(
            [row["event_type"] for row in self.actions(launched, "sprint-budget")], ["red_review"]
        )
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 1)
        for result in (launched, nudged, again):
            self.assertNotIn(
                "sprint-budget", {str(error.get("step") or "") for error in result.get("errors") or []}
            )

    def test_links_that_keep_changing_leave_the_observer_state_unestablished(self) -> None:
        from ummanu.dispatch.observer import _observer_event_state

        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        record = self.observers()["sprint:1"]
        original = self.runtime.sprints.show
        linked = iter(range(100))

        def show(reference: str, **options: object) -> dict:
            sprint = original(reference, **options)
            # Every listing finds one more card linked than the last, as a burst of links would.
            sprint["cards"] = list(sprint.get("cards") or []) + [{"ref": f"linked-{next(linked)}"}]
            return sprint

        with unfiltered_audit_reads_raise() as violations, mock.patch.object(
            self.runtime.sprints, "show", side_effect=show
        ):
            state = _observer_event_state(self.runtime, "sprint:1", record)

        self.assertEqual(violations, [], "\n\n".join(violations))
        self.assertEqual(
            state,
            {"known": False, "pending": False, "reason": "linked cards changed while the audit was read"},
        )

    def test_a_cursor_outside_the_slice_is_found_by_its_own_key(self) -> None:
        from ummanu.dispatch.observer import _observer_event_state

        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        # A legacy cursor naming telemetry of a card that is not linked to this sprint (any more).
        # The id is found by its request id (the primary key) or by its typed event id.
        for request_id, event_id in (("unlinked-telemetry", "evt_unlinked_telemetry"), ("same-id", "same-id")):
            with self.subTest(cursor=event_id):
                record = {
                    "request_id": request_id,
                    "ref": "ummanu-9-unlinked",
                    "kind": "routing",
                    "outcome": "success",
                    "actor": {"role": "dispatcher", "id": "dispatcher"},
                    "payload": {},
                    "occurred_at": _now(),
                    "event_id": event_id,
                }
                self.audit.append(request_id, record)
                observer = self.observers()["sprint:1"]
                observer.delivery.acknowledged_through = event_id
                read: list[dict] = []
                original = SqlTaskAudit.events

                def recording(
                    audit: SqlTaskAudit, reference: str = "", *, _read=read, _original=original, **filters: object
                ) -> list[dict]:
                    _read.append(dict(filters))
                    return _original(audit, reference, **filters)

                with unfiltered_audit_reads_raise() as violations, mock.patch.object(
                    SqlTaskAudit, "events", recording
                ):
                    state = _observer_event_state(self.runtime, "sprint:1", observer)

                self.assertEqual(violations, [], "\n\n".join(violations))
                self.assertTrue(state["known"], state)
                self.assertIn("ummanu-9-unlinked", read[-1]["references"])

    # lifecycle ---------------------------------------------------------------

    def test_open_sprint_gets_one_observer_head(self) -> None:
        self.open_sprint()

        result = self.runtime.production_tick()

        actions = self.actions(result)
        self.assertEqual([action["action"] for action in actions], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.head, "codex-observer")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertTrue(record.workspace)
        self.assertTrue(record.handle)

    def test_the_head_is_launched_bound_to_its_own_record(self) -> None:
        """The binding a head writes with comes off the record it was brought up for.

        Sprint and generation together: the reference says whose cards this head may write, and
        the generation tells this lifecycle of that reference from the one before it, so a record
        rebuilt after a close and a reopen does not launch a head signing as the old one.
        """
        self.open_sprint()

        self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        self.assertEqual(
            self.host.observer_identities,
            [{OBSERVER_SPRINT_ENV: "sprint:1", OBSERVER_GENERATION_ENV: record.generation}],
        )
        self.assertTrue(record.generation)

        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.kill_observer()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="replacement needed",
            request_id="replacement-event",
        )
        self.runtime.production_tick()

        relaunched = self.observers()["sprint:1"]
        self.assertEqual(relaunched.launches, 2)
        # A respawn is the same head of the same sprint, so it carries the same identity.
        self.assertEqual(
            self.host.observer_identities[-1],
            {OBSERVER_SPRINT_ENV: "sprint:1", OBSERVER_GENERATION_ENV: record.generation},
        )

    def test_a_rotation_that_finds_the_head_still_working_parks_instead_of_stopping_it(self) -> None:
        """secretary-1462: the one stop that is conditional, refused because the head is not quiet.

        The relaunch decided this head was finished and is about to take its pane away. The head
        runtime checks the turn and this head's own epoch under the same lock the delivery takes,
        so a head that turns out to be mid-turn is not stopped and the sprint waits a tick rather
        than losing a live head's pane to its replacement.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.kill_observer()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="replacement needed",
            request_id="replacement-event",
        )
        self.host.observer_not_quiescent = True

        result = self.runtime.production_tick()

        self.assertIn("stop_observer_if_quiescent", self.host.calls)
        self.assertNotIn("stop_observer", self.host.calls)
        self.assertEqual(self.observers()["sprint:1"].launches, 1, "no replacement was brought up")
        deferred = [row for row in self.actions(result) if row["action"] == "observer-launch-deferred"]
        self.assertTrue(deferred, [row["action"] for row in self.actions(result)])
        self.assertIn("not quiet", deferred[0]["reason"])

    def test_a_rotation_stops_the_head_it_replaces_once_it_is_quiet(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.kill_observer()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="replacement needed",
            request_id="replacement-event",
        )

        self.runtime.production_tick()

        self.assertIn("stop_observer_if_quiescent", self.host.calls)
        self.assertEqual(self.observers()["sprint:1"].launches, 2)

    def test_the_rotation_hands_down_the_pid_evidence_that_made_it_judge_the_head_dead(self) -> None:
        """secretary-1462 round 7: the conditional stop is owed the liveness fact, not left to guess.

        The tick decides to replace this head because its pid heartbeat says the process is gone.
        The head runtime cannot read that for itself — Orca answers about panes, and a pane says
        `busy` for the wrapper shell a dead head leaves behind exactly as it does for a working one
        — so the fact travels with the epoch, read at the same judgement, and the runtime skips the
        pane probe instead of refusing the rotation on it forever.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.kill_observer()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="replacement needed",
            request_id="dead-head-event",
        )

        self.runtime.production_tick()

        self.assertEqual(
            [(ref, alive) for ref, _epoch, alive in self.host.observer_quiescent_stops],
            [("sprint:1", False)],
            "the rotation named the head's process dead, which is why it was rotating it",
        )
        self.assertEqual(self.observers()["sprint:1"].launches, 2)

    def test_a_head_that_refused_every_wake_is_replaced_even_when_it_looks_busy(self) -> None:
        """secretary-1462 round 5: an emergency replacement is not routed through the quiet stop.

        The bounded wake retries are spent, so this head is stuck or gone and the sprint is getting
        a new one. A conditional stop would refuse exactly here — the head looks busy, which is the
        reason it is being replaced — and the sprint would sit behind a backoff on a head that
        takes none of its prompts.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="stuck-head-event",
        )
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        # The head runtime would refuse a conditional stop of this head, however it is asked.
        self.host.observer_not_quiescent = True
        refusal = HostError("observer wake was not delivered: pane-stayed-ready")

        with mock.patch.object(self.host, "nudge_observer", side_effect=refusal):
            for _ in range(observer_wake_max_attempts() - 1):
                self.runtime.production_tick()
                self.expire_wake_retry()
            replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-relaunched")
        self.assertIn("replaced after 3 failed wakes", action["reason"])
        self.assertIn("stop_observer", self.host.calls)
        self.assertNotIn("stop_observer_if_quiescent", self.host.calls)
        self.assertEqual(self.observers()["sprint:1"].launches, 2)

    def test_a_head_making_no_provider_progress_is_replaced_even_when_it_looks_busy(self) -> None:
        """The other emergency replacement, and it is emergency for the same reason.

        This head is up and its pane is working; what it is not doing is any provider work. It is
        replaced *because* it looks busy and is not, so the stop that takes it down cannot be the
        one that refuses a head for looking busy.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source()
        self.host.observer_provider_progress = lambda record: self.observer_progress(  # type: ignore[method-assign]
            record, "cursor:stalled"
        )
        self.host.observer_status_result = {
            "last_activity": time.time(),
            "idle": False,
            "delivery_evidence": {"reason": "payload-left-in-composer"},
        }
        self.host.observer_not_quiescent = True
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="stalled-busy-head-event",
        )

        self.runtime.production_tick()
        self.runtime.production_tick()
        self.runtime.production_tick()
        terminal = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(terminal)], ["observer-relaunched"])
        self.assertEqual(self.host.calls.count("stop_observer"), 1)
        self.assertNotIn("stop_observer_if_quiescent", self.host.calls)
        self.assertEqual(self.observers()["sprint:1"].launches, 2)

    def test_the_rotation_carries_the_epoch_of_its_judgement_and_not_one_read_at_the_stop(
        self,
    ) -> None:
        """secretary-1462 round 5: activity between the judgement and the stop refuses the stop.

        The tick decides a head is finished long before it takes its pane away — here, from a dead
        pid. Reading the epoch in the argument list of the stop would fold everything that happened
        in between into the comparison and make it compare a value against itself. The epoch is
        read where the judgement is made, so a head that proved itself alive afterwards keeps its
        pane and the relaunch waits a tick.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.kill_observer()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="replacement needed",
            request_id="late-activity-event",
        )
        readiness = self.runtime.head_readiness

        def head_printed_something(head: str):
            # A wedge inside the launch, after the tick judged the head finished and before the
            # stop: the head did something. Deterministic, and it needs no second thread — this is
            # the one ordering the conditional stop exists to catch.
            self.host.observer_activity_epochs["sprint:1"] = (
                self.host.observer_activity_epochs.get("sprint:1", 0) + 1
            )
            return readiness(head)

        with mock.patch.object(self.runtime, "head_readiness", side_effect=head_printed_something):
            result = self.runtime.production_tick()

        self.assertIn("stop_observer_if_quiescent", self.host.calls)
        self.assertNotIn("stop_observer", self.host.calls)
        self.assertEqual(self.observers()["sprint:1"].launches, 1, "no replacement was brought up")
        deferred = [row for row in self.actions(result) if row["action"] == "observer-launch-deferred"]
        self.assertTrue(deferred, [row["action"] for row in self.actions(result)])
        self.assertIn("not quiet", deferred[0]["reason"])

    def _unbind_record(self, reference: str = "sprint:1") -> None:
        """Rewrite the record the way a state file written before the binding existed reads.

        The key is removed rather than set to False, because that is the on-disk shape a running
        installation is upgraded from, and `from_json` reading a missing key as bound is exactly
        the head that would be left running and refused.
        """
        payload = self.runtime.production_state.load()
        raw = payload["observers"][reference]
        raw.pop("bound", None)
        self.runtime.production_state.save(payload)
        self.assertFalse(self.observers()[reference].bound)

    def test_a_head_from_before_the_binding_is_retired_and_comes_back_bound(self) -> None:
        """The changeover: a live head nobody bound is stopped, and the next tick binds it.

        Its writes are all refused as `observer_identity_unbound`, and nothing it does can bind
        it: the binding is in the environment it started with. No probe of the process can ask,
        so the record is what says whether the head that is up carries one.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.assertTrue(self.observers()["sprint:1"].bound)
        first_generation = self.observers()["sprint:1"].generation
        self._unbind_record()
        # Alive, watched-looking on every other field, and unable to write: `status --json` says so
        # rather than leaving the operator to read the refusals.
        unbound_status = status_observers(self.runtime.production_state.load())[0]
        self.assertTrue(unbound_status["alive"])
        self.assertFalse(unbound_status["bound"])

        retired = self.runtime.production_tick()

        actions = self.actions(retired)
        self.assertEqual([action["action"] for action in actions], ["observer-stopped"])
        self.assertIn("predates the sprint binding", actions[0]["reason"])
        self.assertNotIn("sprint:1", self.observers())
        stopped = [event for event in self.audit.events() if event["kind"] == "observer_stopped"]
        self.assertIn("predates the sprint binding", stopped[-1]["payload"]["reason"])

        relaunched = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(relaunched)],
            ["observer-launched"],
        )
        record = self.observers()["sprint:1"]
        self.assertTrue(record.bound)
        self.assertNotEqual(record.generation, first_generation)
        self.assertEqual(
            self.host.observer_identities[-1],
            {OBSERVER_SPRINT_ENV: "sprint:1", OBSERVER_GENERATION_ENV: record.generation},
        )

    def test_a_bound_head_is_not_retired(self) -> None:
        """The changeover applies once. A head this dispatcher launched is left where it is."""
        self.open_sprint()
        self.runtime.production_tick()

        second = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(second)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(len(self.host.observer_identities), 1)

    def test_second_tick_does_not_launch_a_second_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        before = self.observers()["sprint:1"]

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].to_json(), before.to_json())

    def test_a_dead_pid_without_pending_card_work_is_brought_up_again(self) -> None:
        """secretary-1478: the bring-up is conditional on the head, not on the queue.

        The heartbeat this leaves behind is the one a rebooted host leaves: another boot's
        `boot_id` and a pid nothing is running under. Before this card the empty queue returned
        `observer-idle` and the tick stopped there, while the fence over this sprint's cards asked
        the same heartbeat and held on `observer_head_dead` — the fence waiting for a head, the
        reconciliation waiting for a card event nobody was left to generate
        (issue:5733409d4a74ad3ce8a8, 25 minutes on 2026-08-12, unwedged by hand).
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-relaunched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.state, "running")
        # The replacement carries no batch: nothing was owed to the head it replaced.
        self.assertEqual(record.delivery.stage, DeliveryStage.IDLE)
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        kinds = [event["kind"] for event in self.audit.events("sprint:1")]
        self.assertEqual(kinds, [EVENT_FENCED, EVENT_LAUNCHED, EVENT_FENCED, EVENT_RELAUNCHED])

    def test_the_fence_over_a_dead_head_clears_once_the_replacement_is_adopted(self) -> None:
        """secretary-1478: the scenario is only unwedged when this sprint's cards move again."""
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()

        self.runtime.production_tick()
        cleared = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(cleared)], ["observer-live"])
        self.assertEqual(
            [action["action"] for action in cleared["actions"] if action.get("step") == "observer-fence"],
            ["observer-fence-cleared"],
            "the fence this sprint sat behind is lifted by the adopted replacement",
        )
        kinds = [event["kind"] for event in self.audit.events("sprint:1")]
        self.assertEqual(
            kinds,
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_FENCED, EVENT_RELAUNCHED, EVENT_CLEARED],
        )

    def test_a_live_head_with_a_quiet_queue_is_still_left_alone(self) -> None:
        """secretary-1478 keeps the other half of the rule: a quiet queue never wakes a live head.

        An empty queue is a reason not to disturb a head that is there. It stopped being a reason
        to leave a sprint without one, and nothing else about it moved.
        """
        self.open_sprint()
        self.runtime.production_tick()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, [])
        self.assertEqual(self.host.observer_nudges, [])

    def test_an_unwritten_launch_identity_is_not_evidence_of_a_dead_head(self) -> None:
        """secretary-1478: only positive death brings a head up.

        A pid file nobody can read is not a head that died — it is a reader that cannot answer,
        and a bring-up on it is how a second head ends up beside a live one. The same asymmetry
        `LocalPtyHeadRuntime.start` keeps at the other end of this decision (secretary-1468).
        """
        self.open_sprint()
        self.runtime.production_tick()
        Path(self.observers()["sprint:1"].pid_file).unlink()
        self.expire_launch_grace()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-idle"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(self.host.stopped_observers, [])

    def test_an_unreadable_launch_identity_is_not_evidence_of_a_dead_head(self) -> None:
        """secretary-1478: a half-written or corrupt record answers nothing, so nothing is done."""
        self.open_sprint()
        self.runtime.production_tick()
        Path(self.observers()["sprint:1"].pid_file).write_text("{not json", encoding="utf-8")
        self.expire_launch_grace()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-idle"])
        self.assertEqual(self.observers()["sprint:1"].launches, 1)
        self.assertEqual(self.host.stopped_observers, [])

    def test_a_head_that_dies_at_every_bring_up_does_not_try_on_every_tick(self) -> None:
        """secretary-1478: the bring-up is bounded by the same backoff a failed launch persists.

        A successful launch clears the deferral counter, which is right for every other caller and
        wrong for this one: without a cooldown, a head that dies the moment it comes up would be
        replaced once per tick forever.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.runtime.production_tick()
        self.kill_observer()

        held = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(held)], ["observer-launch-deferred"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2, "the second bring-up waits out its backoff")
        self.assertGreater(record.launch_next_at, time.time())

        self.expire_launch_retry()
        retried = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(retried)], ["observer-relaunched"])
        after = self.observers()["sprint:1"]
        self.assertEqual(after.launches, 3)
        self.assertEqual(after.launch_attempts, 2)
        self.assertGreater(
            after.launch_next_at - time.time(),
            30,
            "the second cooldown is longer than the first: the backoff is exponential",
        )

    def test_a_bring_up_that_failed_over_a_dead_head_tries_again_when_its_backoff_expires(
        self,
    ) -> None:
        """secretary-1478 round 3: the quiet queue never answers for a record a later branch owns.

        The bring-up over a dead head can fail after the old pane is already gone: the teardown
        succeeds and the host refuses the replacement, which leaves the ordinary deferral —
        `deferred`, one attempt, a persisted backoff — and no head this record could call its own.
        The quiet-queue answer above that branch then read "no head that may be running, so not
        positively dead" and rewrote the record to `idle`, dropping the launch the deferral still
        owed. `prepare_observer` was never called again and the fence over this sprint's cards
        never cleared: the card's own defect, reached by another route.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.host.fail_observer_reason = "orca refused the replacement terminal"

        deferred = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(deferred)], ["observer-launch-deferred"]
        )
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 1, "the replacement never came up")
        self.assertEqual(record.launch_attempts, 1)
        self.assertGreater(record.launch_next_at, time.time())
        attempted = self.host.calls.count("prepare_observer")

        self.host.fail_observer_reason = ""
        self.expire_launch_retry()
        retried = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(retried)], ["observer-relaunched"])
        self.assertEqual(
            self.host.calls.count("prepare_observer"),
            attempted + 1,
            "the expired backoff is a launch this tick owes, not a quiet queue to answer",
        )
        after = self.observers()["sprint:1"]
        self.assertEqual(after.launches, 2)
        self.assertEqual(after.state, "running")

        cleared = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(cleared)], ["observer-live"])
        self.assertEqual(
            [action["action"] for action in cleared["actions"] if action.get("step") == "observer-fence"],
            ["observer-fence-cleared"],
            "the sprint the failed bring-up left fenced moves again once the retry is adopted",
        )

    def test_a_failed_bring_up_over_a_dead_head_is_not_retried_on_every_tick(self) -> None:
        """secretary-1478 round 3: obligation 1 holds for the unsuccessful bring-up too.

        The retry the branch above restores is the persisted deferral backoff, not a per-tick one:
        a host that refuses every replacement costs one `prepare_observer` per backoff window.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.host.fail_observer_reason = "orca refused the replacement terminal"

        self.runtime.production_tick()
        attempted = self.host.calls.count("prepare_observer")
        held = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(held)], ["observer-launch-deferred"])
        self.assertEqual(self.host.calls.count("prepare_observer"), attempted)
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launch_attempts, 1)

        self.expire_launch_retry()
        self.runtime.production_tick()

        self.assertEqual(self.host.calls.count("prepare_observer"), attempted + 1)
        after = self.observers()["sprint:1"]
        self.assertEqual(after.launch_attempts, 2, "the refused retry continues the same series")
        self.assertGreater(
            after.launch_next_at - time.time(),
            30,
            "the second window is longer than the first: the backoff is exponential",
        )

    def test_a_replacement_that_stuck_clears_the_cooldown_it_left(self) -> None:
        """secretary-1478: the bound is on heads that keep dying, not on the sprint's whole life."""
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.runtime.production_tick()

        self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        self.assertEqual(record.launch_attempts, 0)
        self.assertEqual(record.launch_next_at, 0.0)
        self.assertEqual(record.deferred_reason, "")

    def test_a_dead_head_is_not_brought_up_while_the_pipeline_drains(self) -> None:
        """secretary-1478: a drain claims nothing new, and a bring-up is new work."""
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launch-skipped"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.deferred_reason, "pipeline is draining")
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_head_stopped_by_a_pause_is_not_brought_up_by_its_dead_pid(self) -> None:
        """secretary-1478: the pause marks keep their meaning under the new bring-up rule.

        A record stopped by a freeze keeps its pane facts here so the recovery branch would see a
        head it could replace; the pause check in front of that branch, and the drain below it,
        are what leave it alone.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        payload = self.runtime.production_state.load()
        record = load_observers(payload)["sprint:1"]
        record.state = "stopped-by-pause"
        record.stopped_reason = "host maintenance"
        record.paused_at = time.time()
        put_observers(payload, {"sprint:1": record})
        self.runtime.production_state.save(payload)

        outcomes = reconcile_observers(self.runtime, payload, pause_mode="drain")

        self.assertEqual([row["action"] for row in outcomes], ["observer-launch-skipped"])
        self.assertEqual(load_observers(payload)["sprint:1"].launches, 1)
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_finished_observer_queue_is_quiet_without_a_new_card_event(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 31 * 60,
            "idle": True,
        }

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-idle")
        self.assertIn("finished its turn", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, [])
        self.assertEqual(self.host.observer_nudges, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "idle-grace")
        self.assertTrue(record.idle_since)
        status = status_observers(self.runtime.production_state.load())[0]
        self.assertEqual(status["state"], "idle-grace")
        self.assertIsNotNone(status["idle_since"])
        self.assertIn("finished its turn", status["idle_reason"])
        sprint_status = self.runtime.sprints.status("sprint:1", observer=status)
        self.assertEqual(sprint_status["observer"]["state"], "idle-grace")

        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        resumed = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(resumed)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "running")

    def test_a_claude_head_is_nudged_and_acknowledges_with_its_causal_marker(self) -> None:
        """The gap this closes: a claude observer never showed Codex's completed-queue screen.

        The wake runs against the real host, and the batch is closed only by the observer's own
        resume naming this delivery.
        """
        self.catalog.profiles["claude-observer"] = {
            "adapter": "claude",
            "model": "opus",
            "resource": "claude-sub",
            "runtime": "local-pty",
        }
        self.catalog.role_defaults["observer"] = "claude-observer"
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.assertEqual(self.observers()["sprint:1"].head, "claude-observer")
        backend = self.real_host_delivering()
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}

        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="claude-observer-event",
        )
        woke = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(woke)], ["observer-nudged"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)
        [(_run, pointer, subject)] = backend.deliveries
        self.assertEqual(subject, "observer-wake")
        self.assertNotIn("\n", pointer.text)
        self.assertLessEqual(len(pointer.text.encode("utf-8")), 256)
        document = (Path(self.observers()["sprint:1"].workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn(f"delivery_id: {delivery.delivery_id}", document)
        self.assertIn(f"through_event: {delivery.through_event}", document)

        entry = {
            "selected_step": "read board",
            "selected_why": "card changed",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "resume",
        }
        self.acknowledge_delivery(entry, request_id="claude-observer-ack")
        acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.IDLE)
        self.assertTrue(delivery.acknowledged_delivery_id)
        self.assertTrue(delivery.acknowledged_resume_id)
        self.assertEqual(len(backend.deliveries), 1, "an acknowledged batch is not delivered again")

    def test_a_wake_the_head_never_took_is_an_explicit_refusal(self) -> None:
        """Retry exhaustion reaches the tick and the delivery record instead of being silent."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        backend = self.real_host_delivering()
        backend.deliver_refusal = DeliverReceipt(status=HEAD_GONE, reason="the prompt did not start a turn")
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="swallowed-wake-event",
        )

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-wake-deferred")
        self.assertEqual(action["status"], "degraded")
        self.assertIn("observer wake was not delivered", action["reason"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
        self.assertIn("did not start a turn", delivery.reason)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(self.observers()["sprint:1"].state, "wake-deferred")
        owed = (delivery.delivery_id, delivery.through_event)

        # Retries are bounded: the last one hands the batch to the replacement path instead of
        # growing the backoff on a head that takes none of its prompts.
        self.expire_wake_retry()
        second = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(second)], ["observer-wake-deferred"])
        self.assertEqual(self.observers()["sprint:1"].delivery.attempts, 2)

        self.expire_wake_retry()
        replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-relaunched")
        self.assertIn("replaced after 3 failed wakes", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.state, "running")
        # The replacement carries the batch that was owed, so a resume still acknowledges it.
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual((record.delivery.delivery_id, record.delivery.through_event), owed)
        self.assertEqual(record.delivery.method, "launch")
        self.assertEqual(record.delivery.attempts, 0)
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_CLEARED, EVENT_RELAUNCHED],
        )

    def test_wake_failures_survive_acknowledgement_as_sprint_evidence(self) -> None:
        """sprint:1402: wakes were refused, the batch was later acknowledged, and the sprint's
        own telemetry then said no delivery had ever failed.

        The batch's retry counter is reset by the acknowledgement and must be — the next batch
        gets its own bounded retries. The sprint's evidence is not: attempts, failures and the
        last failure reason are cumulative over the sprint and outlive both the acknowledgement
        and the head, because the closing resume is written from them.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="evidence-first-event",
        )
        # The head is standing at its prompt: a wake is delivered rather than waited for.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        refusal = HostError("observer wake was not delivered: pane-stayed-ready")
        refusal.evidence = {
            "subject": "observer-wake",
            "stage": "payload_written",
            "payload_bytes": 1315,
            "payload_sha256": "0123456789abcdef",
            "reason": "payload-left-in-composer",
        }

        with mock.patch.object(self.host, "nudge_observer", side_effect=refusal):
            deferred = self.runtime.production_tick()

        action = self.actions(deferred)[0]
        self.assertEqual(action["action"], "observer-wake-deferred")
        self.assertEqual(action["wake_failures"], 1)
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
        self.assertEqual((delivery.wake_attempts, delivery.wake_failures), (1, 1))
        self.assertEqual(delivery.last_failure_method, "observer-wake")
        self.assertEqual(delivery.last_evidence["reason"], "payload-left-in-composer")
        # The evidence is the size and the hash of the payload, never the payload.
        self.assertEqual(delivery.last_evidence["payload_bytes"], 1315)

        # The retry lands, and the observer answers for the batch it was finally given.
        self.expire_wake_retry()
        woke = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(woke)], ["observer-nudged"])
        entry = {
            "selected_step": "read board",
            "selected_why": "card changed",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "resume",
        }
        self.acknowledge_delivery(entry, request_id="evidence-ack")
        acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.IDLE)
        self.assertTrue(delivery.acknowledged_resume_id)
        # The batch is closed and its retry counter with it.
        self.assertEqual(delivery.attempts, 0)
        # The sprint's evidence is not closed by an acknowledgement.
        self.assertEqual((delivery.wake_attempts, delivery.wake_failures), (2, 1))
        self.assertIn(str(refusal), delivery.last_failure_reason)
        self.assertEqual(delivery.last_evidence["stage"], "payload_written")
        # Both readers of this state say so: the operator's own status row, and the sprint
        # status the observer itself reads when it writes the closing resume.
        operator = status_observers(self.runtime.production_state.load())[0]
        self.assertEqual(operator["delivery_failures"], 1)
        self.assertIn("observer-wake:", operator["delivery_last_failure"])
        row = observer_snapshot(self.runtime.production_state.load())[0]
        self.assertEqual(
            self.runtime.sprints.status("sprint:1", observer=row)["observer"]["delivery"]["wake_failures"],
            1,
        )

    def test_the_closing_turn_is_given_the_delivery_counts_it_has_to_report(self) -> None:
        """A head cannot see a wake that never reached it, so the counts are handed to it.

        Both routes carry them, because either head may be the one that writes the closeout: the
        wake message a live head is given, and the launch document a replacement head reads. A
        reviewer bring-up is a different subject and is not counted here — "the reviewer came up"
        was exactly the answer that hid these failures.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="closeout-evidence-event",
        )
        # The head is standing at its prompt: a wake is delivered rather than waited for.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        with mock.patch.object(
            self.host, "nudge_observer", side_effect=HostError("observer wake was not delivered")
        ):
            self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        backend = self.real_host_delivering()
        self.expire_wake_retry()
        self.runtime.production_tick()

        # The wake carries what the sprint had lost before it: this delivery is not history yet.
        # The first launch's own prompt is one of those attempts — every prompt this sprint put in
        # front of its head is counted, not only the ones carrying a card-event batch.
        [(_run, pointer, _subject)] = backend.deliveries
        self.assertNotIn("\n", pointer.text)
        wake_document = (Path(record.workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn("observer delivery so far: 2 attempt(s), 1 failed (1 wake, 0 launch)", wake_document)
        self.assertIn("closing resume", wake_document)

        document = render_observer_prompt(
            self.runtime.sprints.show("sprint:1", include_resume_freshness=False),
            delivery=self.observers()["sprint:1"].delivery,
        )
        self.assertIn("## Delivery evidence", document)
        self.assertIn("3 attempt(s), 1 failed (1 wake, 0 launch)", document)
        self.assertIn("do not retry delivery yourself", document)

    def test_a_first_launch_that_lost_its_prompt_is_counted_and_kept(self) -> None:
        """The normal first launch carries no card-event batch, and still delivers a prompt.

        `delivery_event_id` is set only when a launch is replacing an unacknowledged batch, so an
        initial bring-up whose prompt stayed in the composer used to leave every delivery counter
        at zero. A later successful launch and the closing resume then reported that no observer
        delivery had ever failed, which is exactly what this card exists to stop.
        """
        self.open_sprint()
        evidence = {
            "subject": "observer-launch",
            "handle": "observer:sprint:1",
            "stage": "payload_written",
            "payload_bytes": 512,
            "payload_sha256": "abcdef0123456789",
            "reason": "payload-left-in-composer",
        }
        with mock.patch.object(
            self.host,
            "prepare_observer",
            side_effect=ObserverLaunchAborted("observer bring-up failed", evidence=evidence),
        ):
            failed = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(failed)], ["observer-launch-deferred"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual((delivery.launch_delivery_attempts, delivery.launch_delivery_failures), (1, 1))
        self.assertEqual((delivery.wake_attempts, delivery.wake_failures), (0, 0))
        self.assertEqual(delivery.last_failure_method, "observer-launch")
        self.assertEqual(delivery.last_evidence["reason"], "payload-left-in-composer")
        # A launch with no batch owed writes no retry state: there is nothing to redeliver.
        self.assertEqual(delivery.stage, DeliveryStage.IDLE)

        # The next launch comes up, and the sprint still says a prompt was lost before it.
        self.expire_launch_retry()
        launched = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(launched)], ["observer-launched"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual((delivery.launch_delivery_attempts, delivery.launch_delivery_failures), (2, 1))
        self.assertEqual(status_observers(self.runtime.production_state.load())[0]["delivery_failures"], 1)

    def test_a_busy_delivery_wait_preserves_the_observer_and_its_pending_ack(self) -> None:
        """A stale status-to-send race does not make the owned observer disposable."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="busy-wake-event",
        )
        record = self.observers()["sprint:1"]
        backend = self.real_host_delivering()
        # The status said it was at its prompt; by the time the wake reached it, it was in a turn.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        backend.deliver_refusal = DeliverReceipt(status=HEAD_BUSY, reason="this head is in a turn")

        held = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(held)], ["observer-wake-busy"])
        self.assertEqual(backend.deliveries, [], "a busy wait sends no duplicate prompt")
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, [])
        after = self.observers()["sprint:1"]
        self.assertEqual(
            (after.handle, after.leaf, after.workspace), (record.handle, record.leaf, record.workspace)
        )
        self.assertEqual(after.delivery.stage, DeliveryStage.WAITING_FOR_IDLE)
        self.assertTrue(after.delivery.delivery_id)
        self.assertTrue(after.delivery.through_event)
        self.assertEqual((after.delivery.wake_attempts, after.delivery.wake_failures), (0, 0))
        self.assertEqual(after.delivery.last_evidence["readiness_state"], "busy")

        delivery_id = after.delivery.delivery_id
        backend.deliver_refusal = None
        sent = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(sent)], ["observer-nudged"])
        pending = self.observers()["sprint:1"].delivery
        self.assertEqual(pending.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(pending.delivery_id, delivery_id)
        self.assertEqual((pending.wake_attempts, pending.wake_failures), (1, 0))

    def test_a_resume_for_a_refused_delivery_stops_the_retry(self) -> None:
        """A wake refused after its prompt landed is not sent twice.

        The delivery marker only exists in a prompt that reached the head, so a resume naming it
        is proof of the turn whatever the dispatcher saw of the send.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        backend = self.real_host_delivering()
        backend.deliver_refusal = DeliverReceipt(status=HEAD_GONE, reason="the supervisor stopped answering")
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="post-send-refusal-event",
        )

        refused = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(refused)], ["observer-wake-deferred"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)

        # The head had the prompt after all, and says so with this delivery's own markers.
        entry = {
            "selected_step": "read board",
            "selected_why": "card changed",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "resume",
        }
        self.acknowledge_delivery(entry, request_id="post-send-refusal-ack")
        backend.deliver_refusal = None
        self.expire_wake_retry()
        acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.IDLE)
        self.assertTrue(delivery.acknowledged_resume_id)
        # No second turn for a batch the observer has already answered.
        self.assertEqual(backend.deliveries, [])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_an_adopted_head_with_no_handle_is_replaced_not_waited_on(self) -> None:
        """A tick that died before recording the handle leaves a head nothing can be sent to.

        Its pid is alive, so the lifecycle keeps it, and a card event then has nowhere to go. That
        must reach the delivery's bounded failure path rather than waiting for a readiness the
        record can never be asked for.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()
        adopted = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(adopted)], ["observer-adopted"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.handle, "")
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")

        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="unaddressable-head-event",
        )
        self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]

        first = self.runtime.production_tick()

        action = self.actions(first)[0]
        self.assertEqual(action["action"], "observer-wake-deferred")
        self.assertEqual(action["status"], "degraded")
        self.assertIn("observer record names no terminal to read", action["reason"])
        self.assertEqual(self.observers()["sprint:1"].delivery.attempts, 1)

        self.expire_wake_retry()
        self.runtime.production_tick()
        self.expire_wake_retry()
        replaced = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replaced)], ["observer-relaunched"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertTrue(record.handle)
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(record.delivery.method, "launch")

    def test_a_terminal_orca_will_not_answer_for_also_ends_in_a_replacement(self) -> None:
        """The other external failure of a wake: bounded retries, then the same replacement path."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="unreadable-terminal-event",
        )

        with mock.patch.object(
            self.host, "observer_status", side_effect=HostError("orca terminal list failed")
        ):
            first = self.runtime.production_tick()
            self.assertEqual([row["action"] for row in self.actions(first)], ["observer-wake-deferred"])
            self.expire_wake_retry()
            self.runtime.production_tick()
            self.expire_wake_retry()
            replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-relaunched")
        self.assertIn("observer terminal could not be read", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_a_replacement_that_cannot_come_up_keeps_both_reasons(self) -> None:
        """The escalation does not hide why the wake failed nor why its replacement did not launch."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="failed-replacement-event",
        )

        with mock.patch.object(
            self.host, "observer_status", side_effect=HostError("orca terminal list failed")
        ):
            self.runtime.production_tick()
            self.expire_wake_retry()
            self.runtime.production_tick()
            self.expire_wake_retry()
            self.host.fail_observer_reason = "orca worktree create failed"
            replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertIn("observer terminal could not be read", action["reason"])
        self.assertIn("that replacement did not come up", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        # The batch is not lost with the failed replacement: it goes back on the bounded retry.
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
        self.assertEqual(delivery.attempts, 1)

    def test_a_head_with_no_provider_source_is_woken_rather_than_held_by_an_episode(self) -> None:
        """A bound episode on a run that carries no source cannot hold the batch forever.

        Only the Codex preflight takes a pre-pane baseline, but a delivery boundary opens an
        episode for any adapter. Honouring that episode made every probe answer `unavailable`
        with no ladder to end it: production sprint:1406 held one batch for 69 straight degraded
        ticks while its live Claude observer sat idle.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        payload = self.runtime.production_state.load()
        record = load_observers(payload)["sprint:1"]
        self.assertNotIn("provider_source", HeadRun.from_json(record.head_run).fanout_policy)
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
        self.assertTrue(record.wake_liveness.bound)
        put_observers(payload, {"sprint:1": record})
        self.runtime.production_state.save(payload)
        self.host.observer_provider_progress = lambda _record: {  # type: ignore[method-assign]
            "state": "unavailable",
            "reason": "Claude provider source has no bound v1 baseline",
        }
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="observer-no-source-event",
        )

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_finished_observer_queue_is_nudged_once_for_a_linked_card_event(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 2,
            "idle": True,
        }
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="observer-event",
        )

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-nudged")
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertTrue(record.delivery.delivery_id)
        # The head took the prompt and is working the batch, which is what a pane reports after a
        # nudge: no second delivery goes out while it is busy.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        repeated = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(repeated)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_event_waiting_for_an_active_queue_is_nudged_when_that_queue_finishes(self) -> None:
        """The watchdog is not a delay between a normal turn completing and its event wake."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed while observer was working",
            request_id="event-during-active-turn",
        )

        waiting = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-waiting"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.WAITING_FOR_IDLE)
        self.assertTrue(record.delivery.pending_from)
        self.assertEqual(self.host.observer_nudges, [])

        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        delivered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(delivered)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_resume_acknowledges_the_event_and_prevents_a_second_wake_after_restart(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="ack-event",
        )
        self.runtime.production_tick()
        delivery_id = self.observers()["sprint:1"].delivery.delivery_id
        event_id = self.observers()["sprint:1"].delivery.through_event
        entry = {
            "selected_step": "check board",
            "selected_why": "card changed",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "resume",
        }
        self.acknowledge_delivery(entry, request_id="ack-resume")

        acknowledged = self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.acknowledged_through, event_id)
        self.assertEqual(record.delivery.acknowledged_delivery_id, delivery_id)
        self.assertEqual(record.delivery.acknowledged_resume_id, self.audit.events()[-1]["event_id"])
        self.assertEqual(record.delivery.stage, DeliveryStage.IDLE)
        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        restarted = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(restarted)], ["observer-idle"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_only_the_active_delivery_marker_acknowledges_an_event(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="marker-event",
        )
        self.runtime.production_tick()
        delivery = self.observers()["sprint:1"].delivery
        entry = {
            "selected_step": "read board",
            "selected_why": "card changed",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "resume",
        }
        writer = SprintWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        writer.resume(
            role="observer",
            actor="observer",
            reference="sprint:1",
            entry=entry,
            request_id="unrelated-resume",
        )
        writer.resume(
            role="observer",
            actor="observer",
            reference="sprint:1",
            entry=entry,
            request_id="wrong-delivery",
            delivery_id="delivery-old",
            through_event=delivery.through_event,
        )
        writer.resume(
            role="observer",
            actor="observer",
            reference="sprint:1",
            entry=entry,
            request_id="wrong-through",
            delivery_id=delivery.delivery_id,
            through_event="evt-old",
        )
        # The head is mid-turn writing these resumes, so nothing here is idle evidence.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}

        pending = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(pending)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(self.observers()["sprint:1"].delivery.delivery_id, delivery.delivery_id)

        self.acknowledge_delivery(entry, request_id="matching-marker")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        self.assertEqual(self.observers()["sprint:1"].delivery.acknowledged_delivery_id, delivery.delivery_id)

    def test_crash_before_nudge_keeps_an_unrelated_resume_unacknowledged(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="crash-before-nudge-event",
        )

        with (
            mock.patch.object(
                self.host, "nudge_observer", side_effect=KeyboardInterrupt("crash before nudge")
            ),
            self.assertRaisesRegex(KeyboardInterrupt, "crash before nudge"),
        ):
            self.runtime.production_tick()

        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.DELIVERY_INTENT)
        self.assertEqual(self.host.observer_nudges, [])
        entry = {
            "selected_step": "wait",
            "selected_why": "the board is quiet",
            "rejected_alternatives": "relaunch",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "wait",
        }
        SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            reference="sprint:1",
            entry=entry,
            request_id="old-turn-resume",
        )

        recovered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(recovered)], ["observer-wake-pending"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.DELIVERY_INTENT)
        self.assertEqual(record.delivery.delivery_id, delivery.delivery_id)
        self.assertEqual(self.host.observer_nudges, [])

    def test_resume_before_a_same_second_event_does_not_acknowledge_it(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        same_second = _now()
        entry = {
            "selected_step": "wait",
            "selected_why": "board is quiet",
            "rejected_alternatives": "relaunch",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "wait",
            "recorded_at": same_second,
        }
        SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            reference="sprint:1",
            entry=entry,
            request_id="same-second-resume",
        )
        self.audit.append(
            "same-second-card-event",
            {
                "event_id": "evt_same_second_card",
                "request_id": "same-second-card-event",
                "ref": "ummanu-510",
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": same_second,
            },
        )

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertNotEqual(
            self.observers()["sprint:1"].delivery.acknowledged_through, "evt_same_second_card"
        )

    def test_new_event_after_woken_turn_is_delivered_after_its_resume(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="first",
            request_id="first-wake-event",
        )
        self.runtime.production_tick()
        entry = {
            "selected_step": "read board",
            "selected_why": "first event",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "wait",
        }
        self.acknowledge_delivery(entry, request_id="first-wake-resume")
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="second",
            request_id="second-wake-event",
        )

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])

    def test_a_nudge_b_resume_a_c_delivers_a_second_batch_through_c(self) -> None:
        """A resume for A cannot absorb B or C appended after A's delivery intent."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="first",
            request_id="burst-first",
        )
        self.runtime.production_tick()
        first_id = self.observers()["sprint:1"].delivery.through_event
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="coalesced",
            request_id="burst-second",
        )
        # B lands while the head is still working A's batch.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        waiting = self.runtime.production_tick()
        second_id = self.audit.events()[-1]["event_id"]

        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-pending"])
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.observers()["sprint:1"].delivery.through_event, first_id)
        entry = {
            "selected_step": "read board",
            "selected_why": "coalesced burst",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "wait",
        }
        self.acknowledge_delivery(entry, request_id="burst-resume")
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="after resume",
            request_id="after-burst-resume",
        )
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}

        delivered = self.runtime.production_tick()
        record = self.observers()["sprint:1"]

        self.assertEqual([row["action"] for row in self.actions(delivered)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])
        self.assertEqual(record.delivery.acknowledged_through, first_id)
        self.assertEqual(record.delivery.through_event, self.audit.events()[-1]["event_id"])

    def test_owner_cutoff_outweighs_a_stale_resume_even_with_reversed_event_times(self) -> None:
        """sprint:1432 order: plan, owner cutoff, then an acknowledgement retaining the plan."""
        self.open_sprint()
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        sprint_writer = SprintWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        stale = {
            "selected_step": "cut ummanu-1592 next",
            "selected_why": "the previous card is nearly done",
            "rejected_alternatives": "closing before the remaining cut",
            "current_task": "ummanu-1591",
            "dod_state": "one cut remains",
            "next_safe_step": "create ummanu-1592",
            "recorded_at": "2099-01-01T00:00:00Z",
        }
        with mock.patch("ummanu.sprints._now", return_value="2099-01-01T00:00:00Z"):
            sprint_writer.resume(
                role="observer",
                actor="observer",
                reference="sprint:1",
                entry=stale,
                request_id="sprint-1432-old-plan",
            )
        cutoff = "After ummanu-1591, close the sprint. Do not create any new cards."
        # The wall clock goes backwards. Audit append order, not timestamps, defines the cursor.
        with mock.patch("ummanu.sprints._now", return_value="2000-01-01T00:00:00Z"):
            cutoff_result = sprint_writer.comment(
                role="po",
                actor="owner",
                reference="sprint:1",
                body=cutoff,
                request_id="sprint-1432-owner-cutoff",
            )

        woke = self.runtime.production_tick()
        record = self.observers()["sprint:1"]

        self.assertEqual([row["action"] for row in self.actions(woke)], ["observer-nudged"])
        self.assertEqual(record.delivery.through_event, cutoff_result["event_id"])
        wake_sprint, change = self.host.observer_wake_contexts[-1]
        self.assertEqual(change, "sprint-entity")
        self.assertEqual(
            [comment["body"] for comment in wake_sprint["comments"]],
            ["[sprint:resume]\ncut ummanu-1592 next", "[po]\n" + cutoff],
        )

        # The stale turn proves receipt of the cutoff batch without changing its old semantic plan.
        stale["recorded_at"] = "1999-01-01T00:00:00Z"
        self.acknowledge_delivery(stale, request_id="sprint-1432-stale-ack")
        self.kill_observer()
        replaced = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replaced)], ["observer-relaunched"])
        prompt = (Path(self.observers()["sprint:1"].workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn("### Comment 2 (applicable PO/owner decision)", prompt)
        self.assertIn(cutoff, prompt)
        self.assertIn('"next_safe_step": "create ummanu-1592"', prompt)
        self.assertLess(prompt.index(cutoff), prompt.index('"next_safe_step"'))
        self.assertIn("continuity context, not authority over an owner decision", prompt)

    def test_replacement_launch_delivers_pending_event_without_a_second_nudge(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.kill_observer()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="replacement needed",
            request_id="replacement-event",
        )
        pending_id = self.audit.events()[-1]["event_id"]

        replacement = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replacement)], ["observer-relaunched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(record.delivery.method, "launch")
        self.assertEqual(record.delivery.through_event, pending_id)
        launch_prompt = (Path(record.workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn(record.delivery.delivery_id, launch_prompt)
        self.assertIn(record.delivery.through_event, launch_prompt)
        # The replacement head is working the batch its launch prompt carried.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}

        repeated = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(repeated)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, [])

    def test_burst_of_card_events_coalesces_to_one_observer_nudge(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        # A declared row is fenced until its head is adopted, so the card joins the sprint
        # once the observer is up, the way a card does in production.
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        for request_id in ("burst-one", "burst-two"):
            self.writer.comment(
                role="dispatcher",
                actor="dispatcher",
                reference="ummanu-510",
                body=request_id,
                request_id=request_id,
            )

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        # The batch's last card event, named explicitly: the tick's own fence line and any
        # dispatcher bookkeeping comment (e.g. a vitality note) are written after it and
        # are not what the delivery is cut through.
        batch_events = [
            event["event_id"]
            for event in self.audit.events("ummanu-510")
            if event["event_id"].startswith("evt_burst-two")
        ]
        self.assertEqual(
            self.observers()["sprint:1"].delivery.through_event,
            batch_events[-1],
        )
        entry = {
            "selected_step": "read board",
            "selected_why": "coalesced batch",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "wait",
        }
        self.acknowledge_delivery(entry, request_id="burst-ack")

        acknowledged = self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        self.assertEqual(record.delivery.stage, DeliveryStage.IDLE)
        # The acknowledgement names the event it cut through -- the batch boundary, not a
        # later dispatcher bookkeeping comment.
        self.assertEqual(
            record.delivery.acknowledged_through,
            "evt_burst-two_semantic",
        )

    def test_persisted_nudge_intent_recovers_after_crash_without_a_duplicate_before_the_deadline(
        self,
    ) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="crash boundary",
            request_id="crash-boundary-event",
        )

        def crash_after_send(record, **_context: object) -> None:
            self.host.observer_nudges.append(str(record.sprint))
            raise KeyboardInterrupt("simulated dispatcher crash")

        with mock.patch.object(self.host, "nudge_observer", side_effect=crash_after_send):
            with self.assertRaisesRegex(KeyboardInterrupt, "simulated dispatcher crash"):
                self.runtime.production_tick()

        persisted = self.observers()["sprint:1"].delivery
        self.assertEqual(persisted.stage, DeliveryStage.DELIVERY_INTENT)
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

        recovered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(recovered)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers["sprint:1"].delivery.deadline = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

        redelivered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(redelivered)], ["observer-redelivered"])
        self.assertIn("acknowledgement deadline expired", self.actions(redelivered)[0]["reason"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])

    def test_failed_nudge_retries_with_the_same_delivery_after_restart(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="retry delivery",
            request_id="retry-delivery-event",
        )
        self.host.fail_observer_reason = "terminal transport failed"

        failed = self.runtime.production_tick()

        delivery = self.observers()["sprint:1"].delivery
        delivery_id = delivery.delivery_id
        self.assertEqual([row["action"] for row in self.actions(failed)], ["observer-wake-deferred"])
        self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
        self.assertEqual(delivery.attempts, 1)
        self.assertIn("retry in 30s", delivery.reason)

        self.host.fail_observer_reason = ""
        deferred = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(deferred)], ["observer-wake-deferred"])
        self.assertEqual(self.host.observer_nudges, [])

        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers["sprint:1"].delivery.next_at = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

        retried = self.runtime.production_tick()

        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual([row["action"] for row in self.actions(retried)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(delivery.delivery_id, delivery_id)
        self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_an_old_event_alone_does_not_redeliver_a_live_delivery(self) -> None:
        """The acknowledgement deadline is armed by the delivery, never by the age of the event."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.audit.append(
            "old-event",
            {
                "event_id": "evt_old_event",
                "request_id": "old-event",
                "ref": "ummanu-510",
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2000-01-01T00:00:00Z",
            },
        )

        first = self.runtime.production_tick()
        # The head is working the batch. The event it names is decades old, and that alone is
        # never a reason to send this delivery again.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        repeated = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(first)], ["observer-nudged"])
        self.assertEqual([row["action"] for row in self.actions(repeated)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_an_observer_that_becomes_idle_unacknowledged_is_redelivered_at_once(self) -> None:
        """No acknowledgement and a head back at its prompt: the tick that sees it redelivers."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="idle-redelivery-event",
        )
        self.runtime.production_tick()
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)
        # The turn ran and ended without a resume for this batch, seconds into a 30 minute
        # deadline. The pane is ready for input and its last output is current: nothing more is
        # required, and no quiet interval is waited out over either timestamp.
        self.age_delivery(5)
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}

        redelivered = self.runtime.production_tick()

        action = self.actions(redelivered)[0]
        self.assertEqual(action["action"], "observer-redelivered")
        self.assertIn("became idle without acknowledging", action["reason"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])
        after = self.observers()["sprint:1"].delivery
        self.assertEqual(after.stage, DeliveryStage.AWAITING_ACK)
        # The batch is the same one: a redelivery never advances the high-water mark.
        self.assertEqual(after.through_event, delivery.through_event)
        self.assertLess(time.time(), after.deadline)

    def test_a_pane_without_readable_activity_is_not_read_as_idle_by_the_redelivery(self) -> None:
        """Missing or unreadable activity is no evidence a turn ended, and a busy head is none either.

        A head mid-sentence is covered by the second of these: it is not ready for input, so the
        not-idle branch keeps waiting on it. No quiet interval guards this path.
        """
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="mid-sentence-event",
        )
        self.runtime.production_tick()

        # Long quiet, but the pane's activity timestamp is missing, then unreadable, then the pane
        # is busy. None is evidence of an ended turn, so the delivery waits for its deadline.
        self.age_delivery(600)
        for status in (
            {"idle": True},
            {"idle": True, "last_activity": "never"},
            {"idle": False, "last_activity": time.time()},
        ):
            self.host.observer_status_result = status
            result = self.runtime.production_tick()
            self.assertEqual([row["action"] for row in self.actions(result)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_a_head_that_never_returns_to_idle_ends_at_the_turn_ceiling(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed while the observer was working",
            request_id="turn-ceiling-event",
        )
        waiting = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-waiting"])

        with mock.patch.dict(os.environ, {"UMMANU_OBSERVER_TURN_CEILING_SECONDS": "1"}):
            self.age_delivery(60)
            ceiling = self.runtime.production_tick()

            action = self.actions(ceiling)[0]
            self.assertEqual(action["action"], "observer-wake-deferred")
            self.assertIn("turn ceiling", action["reason"])
            delivery = self.observers()["sprint:1"].delivery
            self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
            self.assertEqual(delivery.attempts, 1)

            # The retries are bounded, so a head that stays busy costs the sprint a replacement
            # rather than holding the batch forever.
            for _ in range(2):
                self.expire_wake_retry()
                replaced = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replaced)], ["observer-relaunched"])
        self.assertIn("turn ceiling", self.actions(replaced)[0]["reason"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])

    def test_exact_provider_progress_outranks_idle_past_the_legacy_turn_ceiling(self) -> None:
        """A live Codex rollout is authority even when the pane looks ready to wake."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source()
        cursors = iter(("cursor:before", "cursor:after"))
        self.host.observer_provider_progress = lambda record: self.observer_progress(  # type: ignore[method-assign]
            record, next(cursors)
        )
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="observer-progress-precedence-event",
        )

        baseline = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(baseline)], ["observer-wake-waiting"])
        self.age_delivery(4 * 60 * 60, deadline=True)
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        progressed = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(progressed)], ["observer-wake-progressing"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.wake_liveness.state.value, "progressed")
        self.assertEqual(record.wake_liveness.busy_attempts, 0)
        self.assertEqual(self.host.observer_nudges, [])
        self.assertEqual(self.host.stopped_observers, [])

    def test_precontract_unbound_observer_is_fenced_relaunched_and_acknowledges_same_batch(self) -> None:
        """No workspace scan may upgrade an old source into progress for a live observer."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source(precontract=True)
        old = self.observers()["sprint:1"]
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="rollout complete",
            request_id="observer-precontract-unbound-event",
        )
        event_id = next(
            event["event_id"]
            for event in self.audit.events()
            if event.get("request_id") == "observer-precontract-unbound-event:semantic"
        )

        replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-relaunched")
        self.assertIn("cannot be rebound from workspace journal discovery", action["reason"])
        self.assertLess(
            self.host.calls.index("stop_observer"),
            len(self.host.calls) - 1 - self.host.calls[::-1].index("prepare_observer"),
        )
        self.assertEqual(self.host.codex_provider_ingresses, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.through_event, event_id)
        self.assertEqual(record.wake_liveness.head_run_id, record.head_run["run_id"])
        self.assertNotEqual(record.wake_liveness.head_run_id, old.head_run["run_id"])
        self.assertEqual(record.wake_liveness.state.value, "baseline_pending")
        self.assertEqual(record.wake_liveness.terminal_outcome, "")
        retired = record.retired_wake_liveness
        self.assertEqual(retired["head_run_id"], old.head_run["run_id"])
        self.assertEqual(retired["terminal_outcome"], "replacement")

        # A normal delayed resume must survive another tick and reload. The current episode is
        # exact-new-run evidence; the old terminal outcome remains audit-only.
        restored = self.observers()["sprint:1"]
        self.assertEqual(restored.wake_liveness.head_run_id, restored.head_run["run_id"])
        self.assertEqual(restored.retired_wake_liveness["terminal_outcome"], "replacement")
        waiting = self.runtime.production_tick()
        # The replacement is a live head holding its batch, not an episode still reporting that
        # nothing about it can be proved.
        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-pending"])
        restored = self.observers()["sprint:1"]
        self.assertEqual(restored.wake_liveness.head_run_id, restored.head_run["run_id"])
        entry = {
            "selected_step": "read board",
            "selected_why": "rollout complete",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "resume",
        }
        self.acknowledge_delivery(entry, request_id="observer-precontract-replacement-ack")
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        acknowledged = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        self.assertEqual(self.observers()["sprint:1"].delivery.acknowledged_through, event_id)

    def test_stalled_residual_composer_reaches_bounded_replacement_without_terminal_input(self) -> None:
        """A stale composer is evidence on a bounded exact-cursor episode, never a 3h wait."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source()
        self.host.observer_provider_progress = lambda record: self.observer_progress(  # type: ignore[method-assign]
            record, "cursor:stalled"
        )
        self.host.observer_status_result = {
            "last_activity": time.time(),
            "idle": False,
            "delivery_evidence": {"reason": "payload-left-in-composer"},
        }
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="observer-stalled-composer-event",
        )

        self.assertEqual(
            [row["action"] for row in self.actions(self.runtime.production_tick())],
            ["observer-wake-waiting"],
        )
        first = self.runtime.production_tick()
        second = self.runtime.production_tick()
        terminal = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(first)], ["observer-wake-no-progress"])
        self.assertEqual([row["action"] for row in self.actions(second)], ["observer-wake-no-progress"])
        self.assertEqual([row["action"] for row in self.actions(terminal)], ["observer-relaunched"])
        self.assertNotIn("nudge_observer", self.host.calls)
        self.assertEqual(self.host.calls.count("stop_observer"), 1)
        record = self.observers()["sprint:1"]
        self.assertEqual(record.wake_liveness.head_run_id, record.head_run["run_id"])
        self.assertEqual(record.wake_liveness.terminal_outcome, "")
        self.assertEqual(record.retired_wake_liveness["terminal_outcome"], "replacement")
        self.assertEqual(
            record.retired_wake_liveness["no_progress_evidence"],
            "completed_turn_residual_composer",
        )

    def test_unadmitted_observer_progress_never_refreshes_a_current_episode(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source()
        self.host.observer_provider_progress = lambda _record: {  # type: ignore[method-assign]
            "state": "identity_mismatch",
            "reason": "foreign observer HeadRun",
        }
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="observer-foreign-provider-event",
        )

        outcome = self.runtime.production_tick()

        # A rejected source keeps waiting on its own clock rather than refusing the batch with
        # nothing left to end the wait, and it says which admission it is waiting on.
        rows = self.actions(outcome)
        self.assertEqual([row["action"] for row in rows], ["observer-wake-waiting"])
        self.assertEqual(rows[0]["admission"], "unknown")
        liveness = self.observers()["sprint:1"].wake_liveness
        self.assertEqual(liveness.state.value, "unknown")
        self.assertTrue(liveness.source_rejected)
        self.assertEqual(self.host.observer_nudges, [])
        self.assertEqual(self.host.stopped_observers, [])

    def test_unavailable_observer_liveness_survives_reload_without_rebaseline(self) -> None:
        """A lost exact source remains bound evidence, never a new workspace-wide baseline."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source()
        self.host.observer_provider_progress = lambda _record: {  # type: ignore[method-assign]
            "state": "unavailable",
            "reason": "bound provider journal cannot be verified",
        }
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="provider journal disappeared",
            request_id="observer-unavailable-reload-event",
        )

        unavailable = self.runtime.production_tick()

        self.assertEqual(
            [row["action"] for row in self.actions(unavailable)],
            ["observer-wake-waiting"],
        )
        before_reload = self.observers()["sprint:1"].wake_liveness
        self.assertEqual(before_reload.state.value, "unavailable")
        self.assertTrue(before_reload.bound)
        self.assertGreater(before_reload.last_provider_observed_at, 0.0)
        self.assertGreater(before_reload.first_observed_at, 0.0)
        self.assertFalse(before_reload.baseline_established)

        # `load_observers` reconstructs the durable state as a restarted dispatcher would.  The
        # first admitted-looking reply cannot turn the unavailable episode into a new baseline.
        reloaded = self.observers()["sprint:1"].wake_liveness
        self.assertEqual(reloaded.state.value, "unavailable")
        self.assertEqual(reloaded.head_run_id, before_reload.head_run_id)
        self.assertEqual(reloaded.head_run_fingerprint, before_reload.head_run_fingerprint)
        self.assertEqual(reloaded.last_provider_observed_at, before_reload.last_provider_observed_at)
        self.assertEqual(reloaded.first_observed_at, before_reload.first_observed_at)
        self.host.observer_provider_progress = lambda record: self.observer_progress(  # type: ignore[method-assign]
            record,
            "cursor:must-not-baseline-after-reload",
        )

        rejected = self.runtime.production_tick()

        self.assertEqual(
            [row["action"] for row in self.actions(rejected)],
            ["observer-wake-waiting"],
        )
        liveness = self.observers()["sprint:1"].wake_liveness
        self.assertEqual(liveness.state.value, "unknown")
        self.assertTrue(liveness.source_rejected)
        self.assertEqual(liveness.head_run_id, before_reload.head_run_id)
        self.assertEqual(self.host.observer_nudges, [])
        self.assertEqual(self.host.stopped_observers, [])

    def test_a_launch_unbound_codex_source_ends_at_the_unproven_ceiling(self) -> None:
        """sprint:1407: an unbound source and a falsely busy pane held a live sprint for hours."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source()
        # The real read of a complete preflight descriptor which never bound, against a pane which
        # keeps answering busy while the head stands at a visible prompt.
        self.host.observer_provider_progress = lambda record: provider_progress_for_run(  # type: ignore[method-assign]
            HeadRun.from_json(record.head_run)
        )
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed while the observer looked busy",
            request_id="unbound-source-ceiling-event",
        )

        waiting = self.actions(self.runtime.production_tick())

        self.assertEqual([row["action"] for row in waiting], ["observer-wake-waiting"])
        self.assertEqual(waiting[0]["admission"], "legacy_unbound_v1")
        self.assertEqual(self.host.observer_nudges, [])

        # Twenty minutes of that wait. The legacy three-hour ceiling would still be running, and
        # this is exactly where the incident sat: a free observer, a blocked sprint, no ladder.
        self.age_delivery(20 * 60, deadline=True)
        deferred = self.actions(self.runtime.production_tick())

        self.assertEqual([row["action"] for row in deferred], ["observer-wake-deferred"])
        self.assertIn("turn ceiling", deferred[0]["reason"])
        self.assertEqual(self.observers()["sprint:1"].delivery.attempts, 1)

        for _ in range(2):
            self.expire_wake_retry()
            replaced = self.actions(self.runtime.production_tick())

        self.assertEqual([row["action"] for row in replaced], ["observer-relaunched"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        # The batch the stuck head was holding is carried into the replacement's own launch
        # delivery rather than being dropped with the head that never answered for it.
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.DELIVERY_INTENT)

    def test_an_ambiguous_claude_observer_source_ends_at_the_unproven_ceiling(self) -> None:
        """A current Claude descriptor cannot fall back to a workspace-wide transcript scan."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(Path(tmp) / "claude-projects")}),
        ):
            self.open_sprint()
            self.board.save_metadata(12, sprint_ref="sprint:1")
            self.runtime.production_tick()
            root = self.install_observer_claude_provider_source()
            project = root / claude_project_dir_name(self.observers()["sprint:1"].workspace)
            project.mkdir(parents=True)
            (project / "first.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
            (project / "second.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
            real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="noop")
            self.host.observer_provider_progress = real_host.observer_provider_progress  # type: ignore[method-assign]
            self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
            self.writer.comment(
                role="dispatcher",
                actor="dispatcher",
                reference="ummanu-510",
                body="two Claude transcripts appeared after observer launch",
                request_id="ambiguous-claude-source-event",
            )

            waiting = self.actions(self.runtime.production_tick())

            self.assertEqual([row["action"] for row in waiting], ["observer-wake-waiting"])
            self.assertEqual(waiting[0]["admission"], "unavailable")
            source = self.observers()["sprint:1"].head_run["fanout_policy"]["provider_progress_source"]
            self.assertEqual(source["state"], "unavailable")
            self.assertIn("ambiguous", source["reason"])

            self.age_delivery(20 * 60, deadline=True)
            deferred = self.actions(self.runtime.production_tick())

            self.assertEqual([row["action"] for row in deferred], ["observer-wake-deferred"])
            self.assertIn("turn ceiling", deferred[0]["reason"])
            for _ in range(2):
                self.expire_wake_retry()
                replaced = self.actions(self.runtime.production_tick())

        self.assertEqual([row["action"] for row in replaced], ["observer-relaunched"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])

    def test_an_admitted_observer_cursor_is_never_judged_by_the_unproven_ceiling(self) -> None:
        """The negative case: a head with provable progress is judged on progress, not the clock."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.install_observer_provider_source()
        cursor = iter(f"cursor:{index}" for index in range(1, 9))
        self.host.observer_provider_progress = lambda record: self.observer_progress(  # type: ignore[method-assign]
            record,
            next(cursor),
        )
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed while the observer was really working",
            request_id="admitted-cursor-ceiling-event",
        )
        self.runtime.production_tick()

        # Well past the unproven ceiling, and past the legacy one too: an advancing exact cursor
        # is the authority, so neither clock may tear this head down.
        self.age_delivery(4 * 60 * 60, deadline=True)
        result = self.actions(self.runtime.production_tick())

        self.assertEqual([row["action"] for row in result], ["observer-wake-progressing"])
        self.assertEqual(self.host.stopped_observers, [])

    def test_a_long_card_mid_turn_is_not_torn_down_by_the_turn_ceiling(self) -> None:
        """The negative case: a busy head past its acknowledgement deadline is still working."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="a long card to work through",
            request_id="long-turn-event",
        )
        self.runtime.production_tick()
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)

        # An hour into the turn: the acknowledgement deadline is long gone, the head is busy the
        # whole time, and the default turn ceiling is nowhere near.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.age_delivery(60 * 60, deadline=True)

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-wake-waiting")
        self.assertIn("not ready for a prompt", action["reason"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, [])
        after = self.observers()["sprint:1"].delivery
        self.assertEqual(after.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(after.delivery_id, delivery.delivery_id)
        self.assertEqual(after.attempts, 0)

    def test_a_redelivered_batch_is_still_acknowledged_only_by_its_own_marker(self) -> None:
        """An idle-triggered redelivery does not loosen the causal acknowledgement."""
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card changed",
            request_id="redelivery-ack-event",
        )
        self.runtime.production_tick()
        first = self.observers()["sprint:1"].delivery
        self.age_delivery(5)
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        self.runtime.production_tick()
        second = self.observers()["sprint:1"].delivery
        self.assertNotEqual(second.delivery_id, first.delivery_id)
        self.assertEqual(second.through_event, first.through_event)

        entry = {
            "selected_step": "read board",
            "selected_why": "card changed",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-510",
            "dod_state": "open",
            "next_safe_step": "resume",
        }
        # The first turn finishes after the second intent was persisted. It names the delivery it
        # was given, which is no longer the active one, so it credits nothing.
        SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            reference="sprint:1",
            entry=entry,
            request_id="older-turn-resume",
            delivery_id=first.delivery_id,
            through_event=first.through_event,
        )
        # The head is working the redelivered batch, so this tick is only about the stale resume.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}

        stale = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(stale)], ["observer-wake-pending"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(self.observers()["sprint:1"].delivery.acknowledged_through, "")

        self.acknowledge_delivery(entry, request_id="active-marker-resume")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        record = self.observers()["sprint:1"].delivery
        self.assertEqual(record.acknowledged_delivery_id, second.delivery_id)
        self.assertEqual(record.acknowledged_through, first.through_event)

    def test_routing_event_and_closed_sprint_do_not_wake_an_observer(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        # A declared row is fenced until its head is adopted, so the card joins the sprint
        # once the observer is up, the way a card does in production.
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        # The dispatcher claim is routine machinery progress, and the later routing-only audit
        # line also starts no observer turn.
        self.assertEqual(
            [row["action"] for row in self.actions(self.runtime.production_tick())], ["observer-idle"]
        )
        self.audit.append(
            "routing-only",
            {
                "event_id": "evt_routing_only",
                "request_id": "routing-only",
                "ref": "ummanu-510",
                "kind": "routing",
                "outcome": "success",
                "occurred_at": "2026-07-29T12:00:00Z",
            },
        )

        routing = self.runtime.production_tick()
        self.close_sprint()
        closed = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(routing)], ["observer-idle"])
        self.assertEqual([row["action"] for row in self.actions(closed)], ["observer-stopped"])
        self.assertEqual(self.host.observer_nudges, [])

    def test_human_assessment_to_issues_wakes_for_the_next_cut_but_routine_move_does_not(self) -> None:
        """Only a human move that removes parked work makes the idle observer choose again."""
        self.open_sprint()
        self.runtime.production_tick()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.board.move(12, "assessment")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.assertEqual(
            [row["action"] for row in self.actions(self.runtime.production_tick())], ["observer-idle"]
        )

        moved = self.writer.move(
            role="po",
            actor="operator",
            reference="ummanu-510",
            target="issues",
            reason="return this cut to triage",
            sprint_override=True,
            sprint_override_reason="operator removes the parked cut",
            request_id="po-assessment-issues",
        )
        woke = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(woke)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.through_event, moved["event_id"])

        self.audit.append(
            "dispatcher-routine-routing",
            {
                "event_id": "evt_dispatcher_routine_routing",
                "request_id": "dispatcher-routine-routing",
                "ref": "ummanu-510",
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher", "id": "dispatcher"},
                "payload": {"from": "validate", "to": "in_progress"},
                "occurred_at": _now(),
            },
        )
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        repeated = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(repeated)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_denied_and_failed_card_events_do_not_wake_an_observer(self) -> None:
        self.open_sprint()
        self.board.move(12, "done")
        self.runtime.production_tick()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        for request_id, kind, outcome in (
            ("denied-card-event", "sprint_guard_denied", "denied"),
            ("guard-success-event", "sprint_guard_denied", "success"),
            ("failed-card-event", "commented", "failed"),
            ("missing-outcome-event", "commented", ""),
        ):
            self.audit.append(
                request_id,
                {
                    "event_id": "evt_" + request_id,
                    "request_id": request_id,
                    "ref": "ummanu-510",
                    "kind": kind,
                    "outcome": outcome,
                    "occurred_at": "2099-01-01T00:00:00Z",
                },
            )

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-idle"])
        self.assertEqual(self.host.observer_nudges, [])

    def test_legacy_noise_batch_preserves_semantic_events_after_prior_cursor(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        for request, kind, payload in (
            ("legacy-ack", "routing", {}),
            ("semantic-assessment", "moved", {"to": "assessment"}),
            ("legacy-active-noise", "routing", {}),
        ):
            self.audit.append(
                request,
                {
                    "event_id": "evt_" + request,
                    "request_id": request,
                    "ref": "ummanu-510",
                    "kind": kind,
                    "outcome": "success",
                    "actor": {"role": "dispatcher"},
                    "payload": payload,
                    "occurred_at": _now(),
                },
            )
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        delivery = observers["sprint:1"].delivery
        delivery.stage = DeliveryStage.AWAITING_ACK
        delivery.acknowledged_through = "evt_legacy-ack"
        delivery.delivery_id = "legacy-delivery"
        delivery.through_event = "evt_legacy-active-noise"
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-nudged")
        self.assertEqual(action["event_id"], "evt_semantic-assessment")
        self.assertEqual(self.observers()["sprint:1"].delivery.through_event, "evt_semantic-assessment")

    def test_unknown_active_legacy_cursor_fails_closed(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        delivery = observers["sprint:1"].delivery
        delivery.stage = DeliveryStage.AWAITING_ACK
        delivery.delivery_id = "legacy-delivery"
        delivery.through_event = "evt_missing"
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-cursor-unavailable")
        self.assertEqual(action["status"], "degraded")
        self.assertEqual(self.host.observer_nudges, [])

    def test_dead_pending_legacy_head_with_unknown_cursor_is_not_relaunched(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        record = observers["sprint:1"]
        record.state = "pending"
        record.delivery.stage = DeliveryStage.AWAITING_ACK
        record.delivery.delivery_id = "legacy-delivery"
        record.delivery.through_event = "evt_missing"
        launches = record.launches
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-cursor-unavailable")
        self.assertEqual(action["status"], "degraded")
        self.assertEqual(self.observers()["sprint:1"].launches, launches)

    def test_abandoned_dead_launch_intent_validates_unknown_cursor_before_relaunch(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        record = observers["sprint:1"]
        record.state = "launching"
        record.pending_launch = record.launches + 1
        record.delivery.stage = DeliveryStage.AWAITING_ACK
        record.delivery.delivery_id = "launching-legacy-delivery"
        record.delivery.through_event = "evt_missing_after_launch_intent"
        launches = record.launches
        prepared = list(self.host.observers)
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-cursor-unavailable")
        self.assertEqual(action["status"], "degraded")
        after = self.observers()["sprint:1"]
        self.assertEqual(after.launches, launches)
        self.assertEqual(after.pending_launch, 0)
        self.assertEqual(self.host.observers, prepared)

    def test_observer_event_reconciliation_reads_one_audit_snapshot(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        record = self.observers()["sprint:1"]
        real_events = SqlTaskAudit.events
        calls: list[SqlTaskAudit] = []

        def counted(audit: SqlTaskAudit, *args: object, **kwargs: object) -> list[dict]:
            calls.append(audit)
            return real_events(audit, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(SqlTaskAudit, "events", new=counted):
            state = _observer_event_state(self.runtime, "sprint:1", record)

        self.assertTrue(state["known"])
        self.assertEqual(len(calls), 1)

    def test_event_boundary_precedes_the_live_context_snapshot(self) -> None:
        """A committed event in this batch has already made its board comment visible."""
        self.open_sprint()
        self.runtime.production_tick()
        record = self.observers()["sprint:1"]
        sprint_writer = SprintWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        real_events = SqlTaskAudit.events
        inserted = [False]
        cutoff = "Close after ummanu-1591 and create no more cards."

        def events_after_owner_comment(audit: SqlTaskAudit, *args: object, **kwargs: object) -> list[dict]:
            if not inserted[0]:
                inserted[0] = True
                sprint_writer.comment(
                    role="po",
                    actor="owner",
                    reference="sprint:1",
                    body=cutoff,
                    request_id="owner-comment-at-event-boundary",
                )
            return real_events(audit, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(SqlTaskAudit, "events", new=events_after_owner_comment):
            state = _observer_event_state(self.runtime, "sprint:1", record)

        self.assertTrue(state["pending"])
        self.assertIn("[po]\n" + cutoff, [row["body"] for row in state["sprint"]["comments"]])

    def test_wake_carries_the_event_states_single_live_sprint_snapshot(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="assessment changed",
            request_id="single-sprint-snapshot-event",
        )
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        record = observers["sprint:1"]
        event = _observer_event_state(self.runtime, "sprint:1", record)
        self.assertTrue(event["pending"])

        with mock.patch.object(
            self.runtime.sprints,
            "show",
            side_effect=AssertionError("wake must carry the event-state snapshot"),
        ):
            result = _wake_for_event(self.runtime, payload, observers, "sprint:1", record, event)

        self.assertEqual(result["action"], "observer-nudged")
        self.assertIs(self.host.observer_wake_contexts[-1][0], event["sprint"])

    def test_unloaded_live_comments_fail_closed_before_delivery_intent(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="assessment changed",
            request_id="missing-live-comments-event",
        )
        unloaded = self.runtime.sprints.show("sprint:1", include_resume_freshness=False)
        unloaded.pop("comments")

        with mock.patch.object(self.runtime.sprints, "show", return_value=unloaded):
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-cursor-unavailable")
        self.assertIn("live sprint comments are unavailable", action["reason"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.IDLE)
        self.assertEqual(self.host.observer_nudges, [])

    def test_ready_terminal_nudge_does_not_poll_audit_for_confirmation(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        for key in (12, 13):
            self.board.move(key, "done")
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="card entered assessment",
            request_id="single-audit-snapshot-event",
        )
        real_events = SqlTaskAudit.events
        calls: list[SqlTaskAudit] = []

        def counted(audit: SqlTaskAudit, *args: object, **kwargs: object) -> list[dict]:
            calls.append(audit)
            return real_events(audit, *args, **kwargs)  # type: ignore[arg-type]

        def accept_while_ready(record: ObserverRecord, **_context: object) -> str:
            self.host.observer_nudges.append(str(record.sprint))
            return "accepted"

        with (
            mock.patch.object(SqlTaskAudit, "events", new=counted),
            mock.patch.object(
                self.host,
                "nudge_observer",
                side_effect=accept_while_ready,
            ),
            mock.patch(
                "ummanu.dispatch.production._reconcile_sprint_budget",
                return_value=[],
            ),
        ):
            result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.observers()["sprint:1"].delivery.stage,
            DeliveryStage.AWAITING_ACK,
        )

    def test_active_card_does_not_relaunch_a_finished_observer_without_a_new_event(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        # A declared row is fenced until its head is adopted, so the card joins the sprint
        # once the observer is up, the way a card does in production.
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.board.save_sprint_metadata("sprint:1", sprint_current_task="ummanu-510")
        self.host.observer_status_result = {
            "last_activity": time.time() - 2,
            "idle": True,
        }
        # The initial dispatcher claim is machinery progress, not observer work. The completed
        # observer remains quiet despite the active card remaining on the board.
        self.assertEqual(
            [row["action"] for row in self.actions(self.runtime.production_tick())], ["observer-idle"]
        )
        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-idle"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.observer_nudges, [])

    def test_closed_sprint_stops_the_head_and_drops_the_record(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_CLEARED, EVENT_STOPPED],
        )

    def test_vanished_sprint_stops_the_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.clear_sprints()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.observers(), {})

    def test_a_reappeared_sprint_reference_is_a_second_audited_lifecycle(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.clear_sprints()
        self.runtime.production_tick()

        self.open_sprint()
        result = self.runtime.production_tick()
        self.board.clear_sprints()
        self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        events = self.audit.events("sprint:1")
        # The reappeared sprint is fenced, launched, cleared and stopped again: a full second
        # lifecycle, including its own fence episode. That episode used to be missing from the log
        # whenever both lifecycles fell in the same wall-clock second, because the fence request id
        # was built from a second-granularity timestamp and the audit deduped the collision away —
        # which also made this test pass or fail on where a second boundary landed (secretary-1164).
        self.assertEqual(
            [event["kind"] for event in events],
            [
                EVENT_FENCED,
                EVENT_LAUNCHED,
                EVENT_CLEARED,
                EVENT_STOPPED,
                EVENT_FENCED,
                EVENT_LAUNCHED,
                EVENT_CLEARED,
                EVENT_STOPPED,
            ],
        )
        # The second lifecycle is its own request, not a retry of the first one.
        self.assertEqual(len({event["request_id"] for event in events}), 8)

    def test_no_open_sprint_changes_nothing(self) -> None:
        before = len(self.host.calls)

        result = self.runtime.production_tick()

        self.assertEqual(self.actions(result), [])
        self.assertNotIn("observers", self.runtime.production_state.load())
        self.assertNotIn("prepare_observer", self.host.calls[before:])
        self.assertNotIn("stop_observer", self.host.calls[before:])

    def test_budget_is_charged_from_card_events_once_and_hard_limit_stops_observer(self) -> None:
        self.catalog.instance = {"sprint_budget": {"signal": 1, "hard": 2}}
        self.runtime.sprints = SprintReader(
            self.board, data_dir=self.data_dir, thresholds={"signal": 1, "hard": 2}
        )  # type: ignore[arg-type]
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="red",
            body="fix it",
            request_id="red-review",
        )
        first = self.runtime.production_tick()
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 1)
        self.assertTrue(self.runtime.sprints.show("sprint:1")["budget"]["signal_reached"])
        self.assertEqual(len([row for row in first["actions"] if row.get("step") == "sprint-budget"]), 1)
        repeated = self.runtime.production_tick()
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 1)
        self.assertEqual([row for row in repeated["actions"] if row.get("step") == "sprint-budget"], [])
        self.writer.move(
            role="po",
            actor="operator",
            reference="ummanu-510",
            target="blocked",
            reason="operator stop",
            sprint_override=True,
            sprint_override_reason="operator stop",
            request_id="blocked-card",
        )
        result = self.runtime.production_tick()
        sprint = self.runtime.sprints.show("sprint:1")
        self.assertEqual(sprint["status"], "stopped")
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 1)
        hard_stop_events = [
            event for event in self.audit.events("sprint:1") if event.get("kind") == "budget_hard_stopped"
        ]
        self.assertEqual(len(hard_stop_events), 1)
        self.assertEqual(hard_stop_events[0]["payload"]["reason"], "budget_hard_limit")
        self.assertIn("observer-stopped", [row.get("action") for row in self.actions(result)])

    def test_an_infrastructure_block_is_recorded_on_the_sprint_without_charging_it(self) -> None:
        """End to end: the tick reads the shared terminal taxonomy and counts it apart."""
        self.catalog.instance = {"sprint_budget": {"signal": 1, "hard": 2}}
        self.runtime.sprints = SprintReader(
            self.board, data_dir=self.data_dir, thresholds={"signal": 1, "hard": 2}
        )  # type: ignore[arg-type]
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.writer.move(
            role="po",
            actor="operator",
            reference="ummanu-510",
            target="blocked",
            reason="the worker head never came up",
            sprint_override=True,
            sprint_override_reason="the worker head never came up",
            request_id=infrastructure_action("dispatcher-attempt-1-bringup-blocked"),
            terminal_taxonomy={
                "version": 1,
                "disposition": "blocked",
                "blocked_reason": "infrastructure",
                "source_evidence": "infrastructure",
                "provenance": "forward",
            },
        )

        self.runtime.production_tick()

        sprint = self.runtime.sprints.show("sprint:1")
        self.assertEqual(sprint["budget"]["total"], 0)
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 0)
        self.assertEqual(
            sprint["budget"]["uncharged"],
            {BUDGET_UNCHARGED_INFRASTRUCTURE: 1},
        )
        self.assertFalse(sprint["budget"]["signal_reached"])
        self.assertEqual(sprint["status"], "open")
        # The card itself is untouched by the budget decision: it stays Blocked for the observer.
        self.assertEqual(self.reader.show("ummanu-510")["state"], "blocked")

    def test_budget_event_classification_excludes_green_card_cycle(self) -> None:
        cases = {
            "red_review": {"kind": "verdict", "payload": {"marker": "review:red"}},
            "blocked": {"kind": "moved", "payload": {"to": "blocked"}},
            "red_ci": {"kind": "moved", "payload": {"to": "in_progress"}, "request_id": "gate-red"},
            "preempt": {"kind": "moved", "payload": {"from": "validate", "to": "ready"}},
            "recreated_task": {"kind": "created", "payload": {"budget_event": "recreated_task"}},
            "hotfix": {"kind": "created", "payload": {"budget_event": "hotfix"}},
        }
        self.assertEqual(
            {name: _budget_event_type(event) for name, event in cases.items()}, {name: name for name in cases}
        )
        self.assertIsNone(_budget_event_type({"kind": "verdict", "payload": {"marker": "review:green"}}))
        self.assertIsNone(_budget_event_type({"kind": "moved", "payload": {"to": "done"}}))

    def test_infrastructure_block_is_counted_apart_from_a_task_block(self) -> None:
        """The class is read from the transition taxonomy, never request-id spelling."""
        blocked = {
            "record_type": "board.protocol_event",
            "kind": "card.blocked",
            "transition": {"target": "blocked"},
            "data": {
                "terminal_taxonomy": {
                    "version": 1,
                    "disposition": "blocked",
                    "blocked_reason": "infrastructure",
                    "source_evidence": "infrastructure",
                    "provenance": "forward",
                }
            },
        }
        charged = (
            "dispatcher-attempt-1-bringup-blocked",
            "dispatcher-attempt-1-worker-respawn-blocked",
            "dispatcher-attempt-1-rework-blocked",
            "dispatcher-attempt-1-review-blocked",
            # A block that is not a bring-up at all: a worker's own report, the gate, a merge.
            "dispatcher-attempt-1-worker-report-blocked",
            "",
        )
        self.assertEqual(
            [_budget_event_type({**blocked, "request_id": request_id}) for request_id in charged],
            [BUDGET_UNCHARGED_INFRASTRUCTURE] * len(charged),
        )
        legacy = {"kind": "moved", "payload": {"to": "blocked"}}
        self.assertEqual(
            _budget_event_type({**legacy, "request_id": infrastructure_action(charged[0])}),
            BUDGET_UNCHARGED_INFRASTRUCTURE,
        )
        self.assertEqual(_budget_event_type({**legacy, "request_id": "worker-report-blocked"}), "blocked")
        # The other budget-shaped events keep their type whatever the request id spells.
        self.assertEqual(
            _budget_event_type(
                {
                    "kind": "moved",
                    "payload": {"from": "validate", "to": "ready"},
                    "request_id": charged[0],
                }
            ),
            "preempt",
        )

    def test_forward_reslice_charges_and_malformed_history_does_not_stop_later_budget_events(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")
        self.writer.move(
            role="po",
            actor="operator",
            reference="ummanu-510",
            target="blocked",
            reason="reslice",
            sprint_override=True,
            sprint_override_reason="reslice",
            request_id="forward-reslice",
            terminal_taxonomy={
                "version": 2,
                "disposition": "reslice",
                "blocked_reason": None,
                "source_evidence": None,
                "budget_class": "blocked",
                "provenance": "forward",
            },
        )
        self.audit.append(
            "malformed-taxonomy",
            {
                "event_id": "evt_malformed_taxonomy",
                "request_id": "malformed-taxonomy",
                "ref": "ummanu-510",
                "record_type": "board.protocol_event",
                "kind": "card.blocked",
                "transition": {"target": "blocked"},
                "data": {"terminal_taxonomy": {"version": 1}},
            },
        )
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="red",
            body="later budget event",
            request_id="later-red-review",
        )

        actions = _reconcile_sprint_budget(self.runtime)

        self.assertEqual(
            [action.get("event_type") or action.get("action") for action in actions],
            ["blocked", "terminal-taxonomy-invalid", "red_review"],
        )
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 2)

    def test_full_green_card_cycle_does_not_charge_the_sprint_budget(self) -> None:
        self.open_sprint()
        self.board.save_metadata(12, sprint_ref="sprint:1")

        self.writer.claim(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            worker="worker",
            request_id="green-claim",
        )
        self.writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            target="validate",
            reason="worker completed",
            request_id="green-validate",
        )
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="green",
            body="looks good",
            request_id="green-verdict",
        )
        self.writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            target="done",
            reason="review passed",
            request_id="green-done",
        )

        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 0)
        self.assertEqual([row for row in result["actions"] if row.get("step") == "sprint-budget"], [])

    def test_unlinked_historical_budget_events_are_not_reread_on_every_tick(self) -> None:
        for index in range(20):
            task_id = 1000 + index
            reference = f"ummanu-historical-{index}"
            self.board.add_card(
                task_id,
                reference,
                state="ready",
                title=reference,
                description="",
                position=task_id,
                created=1720000000,
                project=None,
                metadata={"project": "ummanu", "task_type": "code"},
            )
            self.audit.append(
                f"historical-red-{index}",
                {
                    "event_id": f"evt_historical_red_{index}",
                    "request_id": f"historical-red-{index}",
                    "ref": reference,
                    "kind": "verdict",
                    "occurred_at": "2026-07-27T00:00:00Z",
                    "payload": {"marker": "review:red"},
                },
            )

        self.board.calls.clear()
        self.assertEqual(_reconcile_sprint_budget(self.runtime), [])
        first_reads = [
            params
            for method, params in self.board.calls
            if method == "getTaskByReference" and params.get("project_id") == BOARD_ID
        ]
        self.assertEqual(len(first_reads), 20)
        self.assertEqual(
            len([event for event in self.audit.events() if event.get("kind") == "budget_unlinked"]),
            20,
        )

        self.board.calls.clear()
        self.assertEqual(_reconcile_sprint_budget(self.runtime), [])
        repeated_reads = [
            params
            for method, params in self.board.calls
            if method == "getTaskByReference" and params.get("project_id") == BOARD_ID
        ]
        self.assertEqual(repeated_reads, [])

    def test_unreadable_linked_sprint_skips_only_its_ready_cards_and_is_cached(self) -> None:
        ready = [
            {"ref": "broken-1", "sprint": "sprint:broken", "type": "code", "project": "one"},
            {"ref": "broken-2", "sprint": "sprint:broken", "type": "code", "project": "two"},
            {"ref": "claimable", "sprint": None, "type": "code", "project": "three"},
        ]
        with (
            mock.patch("ummanu.dispatch.production._production_tasks", side_effect=[[], ready]),
            mock.patch.object(
                self.runtime.sprints,
                "show",
                side_effect=TaskError("backend_error", "sprint board is down", 1),
            ) as show,
            mock.patch(
                "ummanu.dispatch.production.claim_ready_task",
                return_value={"action": "claimed"},
            ) as claim,
        ):
            result = _production_claim_ready(self.runtime, {}, {})

        self.assertEqual(show.call_count, 1)
        self.assertIs(claim.call_args.args[0], self.runtime)
        self.assertEqual(claim.call_args.args[1]["ref"], "claimable")
        self.assertEqual([item["ref"] for item in result["skipped_ready"]], ["broken-1", "broken-2"])

    def test_unreadable_sprint_board_never_stops_a_live_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        def explode(**_kwargs):
            raise TaskError("backend_error", "board store is down", 1)

        with mock.patch.object(self.runtime.sprints, "list", explode):
            result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["sprint-board-unavailable"])
        self.assertEqual(self.host.stopped_observers, [])
        self.assertIn("sprint:1", self.observers())

    def test_a_refused_stop_keeps_the_record_and_the_next_tick_retries(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stop-failed"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")
        # The head is still alive, so nothing may claim it was stopped.
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_CLEARED],
        )

        self.host.fail_stop_observer_reason = ""
        retry = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(retry)], ["observer-stopped"])
        self.assertEqual(self.observers(), {})
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_CLEARED, EVENT_STOPPED],
        )

    def test_a_refused_stop_never_yields_a_second_head_when_the_sprint_reopens(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"
        self.runtime.production_tick()

        self.board.save_sprint_metadata("sprint:1", sprint_status="open")
        self.host.fail_stop_observer_reason = ""
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "running")

    def test_a_dead_observer_whose_pane_will_not_close_parks_its_bring_up(self) -> None:
        """secretary-1478 rewrote this scenario's verdict, not its shape.

        It used to assert that a dead head with no pending work needed no teardown at all, because
        no replacement was brought up for it. The replacement is now brought up whatever the queue
        holds, so this is the ordinary refused-teardown path: the pane is left on the record for
        the next tick to close, and no second head is opened beside it.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launch-deferred"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.handle, "observer:sprint:1")

    def test_a_bring_up_that_left_its_terminal_up_keeps_the_handle_on_the_record(self) -> None:
        self.open_sprint()
        pid_file = self.data_dir / "observers" / "aborted.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        # The abandoned head is a live process: only the record's own mark tells it apart from a
        # working observer.
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(pid_file),
        )

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launch-deferred"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertTrue(record.abandoned_handle)
        self.assertEqual(record.launches, 0)
        self.assertIn("terminal close failed", record.deferred_reason)
        # Nothing was launched, so no launch event may be in the log.
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_DEFERRED],
        )

    def test_an_abandoned_terminal_that_will_not_close_never_yields_a_second_head(self) -> None:
        self.open_sprint()
        pid_file = self.data_dir / "observers" / "aborted.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(pid_file),
        )
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        # Not "observer-live": the pid behind that handle belongs to a head that never got its
        # sprint. The tick retries the close and brings nothing new up until it succeeds.
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launch-deferred"])
        self.assertEqual(self.host.calls.count("prepare_observer"), 1)
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertTrue(record.abandoned_handle)
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertIn("could not be stopped", record.deferred_reason)

    def test_a_closed_abandoned_terminal_frees_the_sprint_for_a_launch(self) -> None:
        self.open_sprint()
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(self.data_dir / "observers" / "aborted.pid"),
        )
        self.runtime.production_tick()
        self.host.fail_observer_error = None

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertFalse(record.abandoned_handle)
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")][-1], EVENT_LAUNCHED)

    def test_a_freeze_takes_an_abandoned_terminal_down_with_the_rest(self) -> None:
        self.open_sprint()
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(self.data_dir / "observers" / "aborted.pid"),
        )
        self.runtime.production_tick()

        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        record = self.observers()["sprint:1"]
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(record.handle, "")
        self.assertFalse(record.abandoned_handle)

    # readiness ---------------------------------------------------------------

    def test_unready_resource_defers_the_launch_and_keeps_the_sprint(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unauthenticated", "resource authentication failed", 1.0
        )

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertIn("unauthenticated", record.deferred_reason)
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_DEFERRED],
        )

    # role skill delivery ------------------------------------------------------

    def test_an_undelivered_skill_defers_the_launch_instead_of_a_blind_head(self) -> None:
        self.observer_skill.unlink()
        self.open_sprint()

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertIn("observe-sprint", record.deferred_reason)
        self.assertIn(str(self.observer_skill), record.deferred_reason)
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_DEFERRED],
        )
        # The same reason has to be readable from outside, or the sprint just looks headless.
        self.assertIn(
            "observe-sprint", status_observers(self.runtime.production_state.load())[0]["deferred_reason"]
        )

    def test_a_delivered_skill_launches_after_the_deferred_retry_deadline(self) -> None:
        self.observer_skill.unlink()
        self.open_sprint()
        self.runtime.production_tick()

        install_skill_registry(self.data_dir)
        self.expire_launch_retry()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.observers()["sprint:1"].deferred_reason, "")

    def test_a_shell_without_an_observer_target_defers_the_launch(self) -> None:
        """The skill exists and is delivered somewhere, just not to the shell of this head."""
        manifest = self.data_dir / "registry" / "manifest.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('shell = "codex"', 'shell = "claude"'),
            encoding="utf-8",
        )
        self.open_sprint()

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        self.assertIn("no codex target", self.observers()["sprint:1"].deferred_reason)

    def test_the_launched_prompt_points_at_the_delivered_skill(self) -> None:
        self.open_sprint()

        self.runtime.production_tick()

        prompt = (Path(self.observers()["sprint:1"].workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn(str(self.observer_skill), prompt)

    def test_deferred_launch_waits_for_a_persisted_retry_deadline(self) -> None:
        self.open_sprint()
        unready = HeadReadiness("openai-sub", "unavailable", "resource provider is unavailable", 1.0)
        self.runtime.head_readiness = lambda _head: unready
        self.runtime.production_tick()

        del self.runtime.head_readiness
        waiting = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(waiting)], ["observer-launch-deferred"])
        self.assertEqual(self.host.calls.count("prepare_observer"), 0)
        self.expire_launch_retry()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.deferred_reason, "")
        self.assertEqual(record.launches, 1)

    def test_a_deferral_streak_records_one_event(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )

        self.runtime.production_tick()
        self.runtime.production_tick()

        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_DEFERRED, EVENT_FENCED],
        )

    def test_failed_bring_up_keeps_the_record_intact(self) -> None:
        self.open_sprint()
        self.host.fail_observer_reason = "orca refused the terminal"

        result = self.runtime.production_tick()

        self.assertEqual(self.actions(result)[0]["action"], "observer-launch-deferred")
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 0)
        self.assertIn("orca refused the terminal", record.deferred_reason)

    def test_failed_bring_up_is_not_retried_on_every_tick(self) -> None:
        self.open_sprint()
        self.host.fail_observer_reason = "orca refused the terminal"

        self.runtime.production_tick()
        repeated = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(repeated)], ["observer-launch-deferred"]
        )
        self.assertEqual(self.host.calls.count("prepare_observer"), 1)
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launch_attempts, 1)
        self.assertGreater(record.launch_next_at, time.time())

    # audit durability --------------------------------------------------------

    def broken_append(self):
        """Audit storage that takes a staged event but refuses to commit it."""

        def explode(_request_id, _event):
            raise OSError("audit log is not writable")

        return mock.patch.object(self.audit, "append", explode)

    def broken_stage(self):
        """Audit storage that cannot even take the staged event."""

        def explode(_request_id, _event):
            raise OSError("pending audit directory is not writable")

        return mock.patch.object(self.audit, "stage", explode)

    def test_a_refused_audit_append_still_leaves_the_launched_head_on_the_books(self) -> None:
        self.open_sprint()

        with self.broken_append():
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launched")
        self.assertEqual(action["audit"], "pending")
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertTrue(record.handle)
        # The event is not lost: it waits on disk for the repair pass.
        pending = self.audit.pending_events()
        self.assertEqual(sorted(event["kind"] for event in pending), sorted([EVENT_FENCED, EVENT_LAUNCHED]))
        launched = next(event for event in pending if event["kind"] == EVENT_LAUNCHED)
        self.assertEqual(launched["payload"]["workspace"], record.workspace)
        # Settling it is the checkpoint's stale-staged pass on this store (`SqlTaskAudit.
        # settle_stale_staged`, tests/test_stale_staged_settlement.py); `reconcile` owes nothing.
        self.assertEqual(self.audit.events("sprint:1"), [])

    def test_a_refused_audit_append_does_not_yield_a_second_head(self) -> None:
        self.open_sprint()
        with self.broken_append():
            self.runtime.production_tick()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_an_unwritable_audit_defers_the_launch_instead_of_opening_a_head(self) -> None:
        self.open_sprint()
        # The fence raises on the first tick of a declared row and writes that once. This test is
        # about the launch's own audit write, so the fence has already said its piece by here.
        with self.failing_state_save(after=0), self.assertRaises(OSError):
            self.runtime.production_tick()

        with self.broken_stage():
            result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launch-deferred"])
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 0)
        self.assertIn("could not be staged", record.deferred_reason)

        self.expire_launch_retry()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_fence_episode_keeps_its_request_id_across_a_second_boundary(self) -> None:
        """The retry of a fence raise whose tick died is the same episode, whatever the clock says.

        The test above is this one without the clock pinned: its first tick cannot save state, so
        the fence state that would short-circuit the second raise never lands, and the retry is
        deduped only by minting the request id the first pass already committed. While the id
        carried a second-granularity timestamp, the two passes minted different ids whenever they
        straddled a second — the retry then reached `audit.stage`, which that test has broken on
        purpose, and the whole tick ended at the fence with no observer outcome at all. That is a
        real lottery of about 2% per run, and it is what made the suite red on main (secretary-1167).
        """
        self.open_sprint()
        seconds = iter(["2026-08-06T10:00:00Z", "2026-08-06T10:00:01Z"])
        real_clock = dispatcher_observer_fence.now_rfc3339

        def clock() -> str:
            # The two fence passes, pinned either side of a second boundary. Anything the fence
            # stamps after them reads the real clock again.
            return next(seconds, "") or real_clock()

        with mock.patch.object(dispatcher_observer_fence, "now_rfc3339", clock):
            with self.failing_state_save(after=0), self.assertRaises(OSError):
                self.runtime.production_tick()

            with self.broken_stage():
                result = self.runtime.production_tick()

        # The second pass reached the observer instead of dying at the fence, and it wrote no
        # second fence event: one episode, one durable line, one request id.
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launch-deferred"])
        fenced = [event for event in self.audit.events("sprint:1") if event["kind"] == EVENT_FENCED]
        self.assertEqual(len(fenced), 1)

    # crash-safe launch intent ------------------------------------------------

    def failing_state_save(self, *, after: int = 0):
        """Production state that stops accepting writes after `after` of them landed.

        `after=0` is a data plane that is down before the tick touches the host; `after=1` leaves
        only the pre-launch intent; `after=2` also persists the create-time pane identity and then
        kills the ordinary outcome commit.
        """
        real = self.runtime.production_state.save
        landed = {"count": 0}

        def save(payload):
            if landed["count"] >= after:
                raise OSError("production state is not writable")
            landed["count"] += 1
            real(payload)

        return mock.patch.object(self.runtime.production_state, "save", save)

    def test_the_launch_intent_is_on_disk_before_the_host_is_called(self) -> None:
        self.open_sprint()
        seen: list = []
        real = self.host.prepare_observer

        def spy(sprint, head, *, prompt, identity=None, **kwargs):
            seen.append(load_observers(self.runtime.production_state.load()).get("sprint:1"))
            return real(sprint, head, prompt=prompt, identity=identity, **kwargs)

        with mock.patch.object(self.host, "prepare_observer", spy):
            self.runtime.production_tick()

        intent = seen[0]
        self.assertIsNotNone(intent)
        self.assertEqual(intent.state, "launching")
        self.assertEqual(intent.pending_launch, 1)
        self.assertEqual(intent.launches, 0)
        # Workspace and pid file are the head's own, known before the head exists.
        self.assertEqual(intent.workspace, self.host.observer_workspace("sprint:1"))
        self.assertEqual(intent.pid_file, self.host.observer_pid_file("sprint:1"))

    def test_state_that_cannot_be_written_launches_no_head_at_all(self) -> None:
        self.open_sprint()

        with self.failing_state_save(), self.assertRaises(OSError):
            self.runtime.production_tick()

        self.assertEqual(self.host.observers, [])
        self.assertEqual(self.observers(), {})

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].launches, 1)

    def test_a_launch_intent_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()

        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch, intent.handle), ("launching", 1, ""))
        self.assertTrue(intent.head_run["run_id"], "the pre-launch record binds the future heartbeat")
        self.assertEqual(self.host.observers, ["sprint:1"])

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-adopted"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.pending_launch, 0)
        self.assertEqual(record.head_run["run_id"], intent.head_run["run_id"])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_FENCED],
        )

        live = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(live)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_live_foreign_observer_heartbeat_is_fenced_without_a_stop_or_relaunch(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()
        record = self.observers()["sprint:1"]
        path = Path(record.pid_file)
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat["run_id"] = "foreign-run"
        path.write_text(json.dumps(heartbeat), encoding="utf-8")
        self.host.calls.clear()

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)],
            ["observer-heartbeat-identity-mismatch"],
        )
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertNotIn("stop_observer", self.host.calls)
        self.assertNotIn("prepare_observer", self.host.calls)

    def test_an_observer_launch_persists_its_returned_leaf_before_outcome_commit(self) -> None:
        """A crash after `terminal create` retains the leaf that identifies its alias pane."""
        self.open_sprint()
        with self.failing_state_save(after=2), self.assertRaises(OSError):
            self.runtime.production_tick()

        intent = self.observers()["sprint:1"]
        self.assertEqual(
            (intent.state, intent.pending_launch, intent.handle, intent.leaf),
            ("launching", 1, "observer:sprint:1", "leaf:observer:sprint:1"),
        )

        adopted = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(adopted)], ["observer-adopted"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_no_tick_after_a_refused_confirmation_ever_opens_a_second_head(self) -> None:
        """The invariant the worker and reviewer contours were ported from (secretary-820).

        The head is up and the write that would have confirmed it refused. Every tick after that,
        not only the first, has to resolve the intent to the head that is already running.
        """
        self.open_sprint()
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()

        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "launching")

        actions = [
            action["action"] for _ in range(3) for action in self.actions(self.runtime.production_tick())
        ]

        self.assertEqual(actions, ["observer-adopted", "observer-live", "observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_an_adopted_head_is_stopped_through_its_workspace(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()
        self.runtime.production_tick()
        self.close_sprint()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})

    def test_a_freeze_takes_an_adopted_head_down_too(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()
        self.runtime.production_tick()

        status = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "stopped-by-pause")

    def test_an_intent_whose_head_died_before_the_next_tick_is_a_relaunch(self) -> None:
        self.open_sprint()
        # The heartbeat writes a pid nobody is running under, so the head of the lost tick is dead.
        self.host.observer_pid = DEAD_PID
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()

        self.host.observer_pid = os.getpid()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-relaunched"])
        # Whatever the lost tick opened is closed before the replacement head opens.
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.state, "running")
        # The attempt the log already carries is spent, so the new head gets its own line.
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_FENCED, EVENT_RELAUNCHED],
        )

    def test_a_tick_killed_before_the_host_answered_retries_the_same_attempt(self) -> None:
        self.open_sprint()

        with mock.patch.object(self.host, "prepare_observer", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.production_tick()

        self.assertEqual(self.host.observers, [])
        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch), ("launching", 1))
        # The event of that attempt never made it out of pending, so nothing was observed to launch.
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_LAUNCHED])

        self.expire_launch_grace()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].launches, 1)
        self.assertEqual(self.audit.pending_events(), [])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_FENCED, EVENT_LAUNCHED],
        )

    def test_an_intent_without_a_pid_yet_waits_out_its_grace_window(self) -> None:
        """A head that has been launched but has not written its pid is not a dead head.

        Its own grace window says so, so the tick leaves the intent alone: no stop, no second
        prepare_observer. Cutting a head that is still coming up would break the one continuous
        session the sprint is supposed to have.
        """
        self.open_sprint()
        with mock.patch.object(self.host, "prepare_observer", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.production_tick()
        self.host.calls.clear()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launch-pending"])
        self.assertNotIn("prepare_observer", self.host.calls)
        self.assertNotIn("stop_observer", self.host.calls)
        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch, intent.launches), ("launching", 1, 0))
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_LAUNCHED])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")], [EVENT_FENCED, EVENT_FENCED]
        )

    def test_a_waiting_intent_whose_head_appears_is_adopted(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1), self.assertRaises(OSError):
            self.runtime.production_tick()
        pid_file = Path(self.observers()["sprint:1"].pid_file)
        written = pid_file.read_text(encoding="utf-8")
        pid_file.unlink()

        waiting = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(waiting)], ["observer-launch-pending"])

        pid_file.write_text(written, encoding="utf-8")
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-adopted"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_refused_audit_append_still_drops_the_stopped_record(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        with self.broken_append():
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-stopped")
        self.assertEqual(action["audit"], "pending")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        # The terminal is gone, so no record may keep pointing at it.
        self.assertEqual(self.observers(), {})
        self.assertEqual(
            sorted(event["kind"] for event in self.audit.pending_events()),
            sorted([EVENT_CLEARED, EVENT_STOPPED]),
        )
        # Settled by the checkpoint's stale-staged pass on this store, not by `reconcile`.
        self.assertEqual(
            sorted(event["kind"] for event in self.audit.events("sprint:1")),
            sorted([EVENT_FENCED, EVENT_LAUNCHED]),
        )

    def test_an_unwritable_audit_parks_the_stop_instead_of_performing_it(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        # The declared row's fence raises on the first tick and clears on the second; the stop
        # below is then the only thing left with an audit write to make.
        self.runtime.production_tick()
        self.close_sprint()

        with self.broken_stage():
            result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stop-failed"])
        self.assertEqual(self.host.stopped_observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")

        retry = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(retry)], ["observer-stopped"])
        self.assertEqual(self.observers(), {})

    def test_the_repair_pass_commits_a_staged_event_of_a_sprint_that_is_gone(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        # The declared row's fence raises on the first tick and clears on the second, so the stop
        # below is the one event this repair pass has to commit.
        self.runtime.production_tick()
        self.board.clear_sprints()
        with self.broken_append():
            self.runtime.production_tick()

        repaired, unresolved = self.writer.reconcile()

        self.assertEqual((repaired, unresolved), (1, 0))
        self.assertEqual(self.audit.pending_events(), [])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_CLEARED, EVENT_STOPPED],
        )

    def test_a_refused_audit_append_still_records_the_freeze_stop(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        with self.broken_append():
            status = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_STOPPED])

    def test_an_unwritable_audit_keeps_the_freeze_stop_pending(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        with self.broken_stage():
            status = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        self.assertEqual(status["stopped_observer"], [])
        self.assertEqual(self.host.stopped_observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "pause-stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in result["observer_stops"]], ["observer-stopped-by-pause"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])

    # pause -------------------------------------------------------------------

    def test_freeze_stops_the_observer_and_records_the_reason(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        status = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertIn("host maintenance", record.stopped_reason)
        self.assertGreater(record.paused_at, 0)

    def test_a_refused_freeze_stop_is_reported_and_retried_by_the_frozen_tick(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        status = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        self.assertEqual(status["stopped_observer"], [])
        self.assertTrue(any("sprint:1" in warning for warning in status["warnings"]))
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "pause-stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED],
        )

        self.host.fail_stop_observer_reason = ""
        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in result["observer_stops"]], ["observer-stopped-by-pause"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_FENCED, EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_a_head_that_survived_a_refused_freeze_stop_is_not_relaunched_on_resume(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        resumed = self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        self.assertEqual(resumed["observers_resumed"], ["sprint:1"])
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.paused_at, 0.0)

    def test_resume_brings_the_observer_back_on_the_next_tick(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        resumed = self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        self.assertEqual(resumed["observers_resumed"], ["sprint:1"])
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-relaunched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.paused_at, 0.0)

    def test_drain_leaves_a_live_observer_alone_and_launches_none(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()

        actions = {action["sprint"]: action["action"] for action in self.actions(result)}
        self.assertEqual(actions["sprint:1"], "observer-live")
        self.assertEqual(actions["sprint:2"], "observer-launch-skipped")
        self.assertEqual(self.host.stopped_observers, [])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_sprint_opened_during_drain_is_still_visible_from_outside(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        self.runtime.production_tick()

        record = self.observers()["sprint:2"]
        self.assertEqual(record.head, "codex-observer")
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertEqual(record.deferred_reason, "pipeline is draining")
        self.assertTrue(record.last_action_at)
        observed = {row["sprint"]: row for row in self.runtime.production_observe()["observers"]}
        status = {row["sprint"]: row for row in status_observers(self.runtime.production_state.load())}
        for row in (observed["sprint:2"], status["sprint:2"]):
            self.assertEqual(row["head"], "codex-observer")
            self.assertFalse(row["alive"])
            self.assertIn("draining", row["deferred_reason"])
            self.assertTrue(row["last_action_at"])

    def test_the_drained_sprint_is_launched_from_its_record_after_resume(self) -> None:
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")
        self.runtime.production_tick()
        generation = self.observers()["sprint:2"].generation

        self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        actions = {action["sprint"]: action["action"] for action in self.actions(result)}
        self.assertEqual(actions["sprint:2"], "observer-launched")
        record = self.observers()["sprint:2"]
        self.assertEqual(record.generation, generation)
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.deferred_reason, "")
        self.assertEqual(self.host.observers, ["sprint:2"])
        kinds = [event["kind"] for event in self.audit.events("sprint:2")]
        self.assertEqual(kinds, [EVENT_FENCED, EVENT_DEFERRED, EVENT_FENCED, EVENT_LAUNCHED])

    def test_repeated_drain_ticks_write_one_deferral_event(self) -> None:
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        self.runtime.production_tick()
        before = self.observers()["sprint:2"]
        self.runtime.production_tick()

        self.assertEqual(self.observers()["sprint:2"].generation, before.generation)
        self.assertEqual(self.observers()["sprint:2"].launches, 0)
        self.assertEqual(self.host.observers, [])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:2")],
            [EVENT_FENCED, EVENT_DEFERRED, EVENT_FENCED],
        )

    def test_frozen_tick_launches_nothing(self) -> None:
        self.open_sprint()
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.host.observers, [])

    # isolation ---------------------------------------------------------------

    def test_a_live_observer_does_not_block_a_card_claim(self) -> None:
        self.open_sprint()

        result = self.runtime.production_tick()

        claim = [action for action in result["actions"] if action.get("step") == "claim"]
        self.assertEqual(claim[0]["pilot_ref"], "ummanu-510")
        self.assertEqual(self.reader.show("ummanu-510")["state"], "in_progress")
        # The observer holds no card record and does not occupy the per-project claim gate.
        records = self.runtime.production_state.records(self.runtime.production_state.load())
        self.assertEqual(sorted(records), ["ummanu-510"])
        self.assertNotIn("sprint:1", records)

    def test_the_observer_workspace_is_not_a_card_workspace(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        card = self.runtime.production_state.records(self.runtime.production_state.load())
        self.assertNotEqual(record.workspace, card["ummanu-510"].workspace)
        self.assertNotEqual(record.handle, card["ummanu-510"].handle)

    # observability -----------------------------------------------------------

    def test_observer_state_is_visible_without_a_transcript(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        observed = self.runtime.production_observe()["observers"]
        status = status_observers(self.runtime.production_state.load())

        self.assertEqual(observed[0]["sprint"], "sprint:1")
        self.assertEqual(observed[0]["head"], "codex-observer")
        self.assertTrue(observed[0]["alive"])
        self.assertEqual(status[0]["state"], "running")
        self.assertEqual(status[0]["launches"], 1)
        self.assertIsNotNone(status[0]["last_action_at"])
        self.assertIsNone(status[0]["deferred_reason"])

    def test_status_reports_the_deferred_reason(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )
        self.runtime.production_tick()

        status = status_observers(self.runtime.production_state.load())

        self.assertEqual(status[0]["state"], "deferred")
        self.assertFalse(status[0]["alive"])
        self.assertIn("unavailable", status[0]["deferred_reason"])

    # two open sprints --------------------------------------------------------

    def open_disjoint_pair(self, *, second_observer=None) -> None:
        """The admitted pair, with one card in each of the four reserved projects."""
        self.sprint_writer = self.admit_two_open_sprints(
            observer=head_choice("codex-observer"),
            second_observer=second_observer,
        )
        self.link_pair_cards()

    def observed_pair(self) -> dict:
        """Both sprints running their own head, both adopted, both idle for a wake.

        The first tick fences each sprint on its unlaunched head and launches both; the second
        finds both alive, so from here every sprint's cards move under its own observer.
        """
        # Both on the same profile, so what separates the two heads in the assertions below is
        # the sprint each is bound to and nothing about the adapter it runs.
        self.open_disjoint_pair(second_observer=head_choice("codex-observer"))
        self.runtime.production_tick()
        result = self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        return result

    def observed_pair_in_flight(self) -> None:
        """Both heads up and one card of each sprint claimed, one claim per tick."""
        self.observed_pair()
        self.assertEqual(
            self.claimed(self.runtime.production_tick())[0]["pilot_ref"],
            "ummanu-511",
        )

    def budget_of(self, reference: str) -> dict:
        return self.runtime.sprints.show(reference, include_cards=False)["budget"]

    def charge(self, reference: str, request_id: str) -> None:
        """Put one budget-shaped card event on the board card of `reference`'s sprint."""
        self.writer.move(
            role="po",
            actor="operator",
            reference=reference,
            target="blocked",
            reason="operator stop",
            sprint_override=True,
            sprint_override_reason="operator stop",
            request_id=request_id,
        )

    def claimed(self, result: dict) -> list[dict]:
        return [action for action in result["actions"] if action.get("step") == "claim"]

    def skipped(self, result: dict) -> list[dict]:
        for action in result["actions"]:
            if action.get("step") in {"claim", "production-claim"}:
                return list(action.get("skipped_ready") or [])
        return []

    def advanced(self, result: dict) -> list[str]:
        return [action["pilot_ref"] for action in result["actions"] if action["step"] == "advance"]

    def with_thresholds(self, signal: int, hard: int) -> None:
        self.catalog.instance = {"sprint_budget": {"signal": signal, "hard": hard}}
        self.runtime.sprints = SprintReader(  # type: ignore[arg-type]
            self.board,
            data_dir=self.data_dir,
            thresholds={"signal": signal, "hard": hard},
        )

    def settled_pair(self) -> None:
        """Two ticks: the declared head is up, and one card of each sprint is in flight.

        The first tick fences `sprint:1` because its head has not been launched yet, so the
        card claimed there is the other sprint's; the second claims `sprint:1`'s own.
        """
        self.open_disjoint_pair()
        self.assertEqual(
            self.claimed(self.runtime.production_tick())[0]["pilot_ref"],
            "ummanu-511",
        )
        self.assertEqual(
            self.claimed(self.runtime.production_tick())[0]["pilot_ref"],
            "ummanu-510",
        )

    def test_a_card_event_charges_the_sprint_it_is_linked_to_and_no_other(self) -> None:
        self.with_thresholds(1, 3)
        self.settled_pair()
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="red",
            body="fix it",
            request_id="red-review-first-sprint",
        )

        result = self.runtime.production_tick()

        charged = [action for action in result["actions"] if action.get("step") == "sprint-budget"]
        self.assertEqual([action["sprint"] for action in charged], ["sprint:1"])
        self.assertEqual(self.budget_of("sprint:1")["total"], 1)
        self.assertTrue(self.budget_of("sprint:1")["signal_reached"])
        self.assertEqual(self.budget_of("sprint:2")["total"], 0)
        self.assertFalse(self.budget_of("sprint:2")["signal_reached"])
        self.assertEqual(self.runtime.sprints.show("sprint:2")["status"], "open")

    def test_a_red_review_that_restarts_a_worker_charges_only_its_own_sprint(self) -> None:
        """The operational restart, not a synthesised event: review red, worker restarted.

        The card driven here belongs to the sprint that declares no observer, so the verdict
        acts at once rather than parking for a decision.
        """
        self.with_thresholds(1, 6)
        self.open_disjoint_pair()
        self.assertEqual(
            self.claimed(self.runtime.production_tick())[0]["pilot_ref"],
            "ummanu-511",
        )
        # Through the command in the checkout: that id is what attributes the report to the round
        # the dispatcher is waiting for (secretary-1063).
        workspace = self.runtime.production_state.load()["records"]["ummanu-511"]["workspace"]
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
        done_command = next(line for line in document.splitlines() if "--kind done" in line)
        self.writer.report(
            role="worker",
            actor="worker",
            reference="ummanu-511",
            kind="done",
            body="ready for validation",
            request_id=done_command.split("--request-id ", 1)[1].split()[0],
        )
        self.assertEqual(self.runtime.production_tick()["status"], "ok")  # moved to validate
        self.assertIn(
            "review-started",
            [
                action["action"]
                for action in self.runtime.production_tick()["actions"]
                if action["step"] == "review"
            ],
        )
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-511",
            kind="red",
            body="needs work",
            request_id="rework-red-verdict",
        )

        result = self.runtime.production_tick()

        self.assertIn(
            "rework-started",
            [action["action"] for action in result["actions"] if action["step"] == "review"],
        )
        self.assertIn("restart_worker", self.host.calls)
        charged = [action for action in result["actions"] if action.get("step") == "sprint-budget"]
        self.assertEqual(
            [(action["sprint"], action["event_type"]) for action in charged],
            [("sprint:2", "red_review")],
        )
        self.assertEqual(self.budget_of("sprint:2")["total"], 1)
        self.assertTrue(self.budget_of("sprint:2")["signal_reached"])
        self.assertEqual(self.budget_of("sprint:1")["total"], 0)
        self.assertFalse(self.budget_of("sprint:1")["signal_reached"])
        self.assertEqual(self.runtime.sprints.show("sprint:1")["status"], "open")

    def test_a_hard_stop_stops_one_sprint_while_the_other_keeps_claiming(self) -> None:
        self.with_thresholds(1, 2)
        self.settled_pair()
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="ummanu-510",
            kind="red",
            body="fix it",
            request_id="red-first-sprint",
        )
        self.charge("ummanu-510", "blocked-first-sprint")

        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show("sprint:1")["status"], "stopped")
        self.assertEqual(self.runtime.sprints.show("sprint:2")["status"], "open")
        self.assertEqual(self.budget_of("sprint:1")["total"], 2)
        self.assertEqual(self.budget_of("sprint:2")["total"], 0)
        self.assertEqual(
            [event["ref"] for event in self.audit.events() if event["kind"] == "budget_hard_stopped"],
            ["sprint:1"],
        )
        # The stopped sprint's head is stopped and its remaining Ready card is left alone; the
        # other sprint claims its own in the same tick.
        self.assertIn("observer-stopped", [action["action"] for action in self.actions(result)])
        self.assertEqual(self.claimed(result)[0]["pilot_ref"], "third-1")
        self.assertEqual(
            self.skipped(result),
            [{"ref": "fourth-1", "reason": "linked sprint is stopped or closed"}],
        )
        self.assertEqual(self.reader.show("third-1")["state"], "in_progress")
        self.assertEqual(self.reader.show("fourth-1")["state"], "ready")

    def test_closing_the_observed_sprint_leaves_the_other_live_and_claiming(self) -> None:
        self.settled_pair()
        self.assertEqual(self.host.observers, ["sprint:1"])

        settle_dispatcher_work(
            self.data_dir,
            [card["ref"] for card in self.runtime.sprints.show(self.FIRST)["cards"]],
        )
        self.sprint_writer.close(
            role="po",
            actor="operator",
            reference=self.FIRST,
            decisions=close_decisions(self.sprint_writer, self.FIRST),
        )
        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show(self.FIRST)["status"], "closed")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})
        self.assertEqual(self.runtime.sprints.show(self.SECOND)["status"], "open")
        self.assertEqual(self.claimed(result)[0]["pilot_ref"], "third-1")
        # Nothing is skipped for the closed sprint any more: its cards left the board with it,
        # taken off the contract by the dispositions the close carried.
        self.assertEqual(self.skipped(result), [])
        self.assertEqual([card["ref"] for card in self.runtime.sprints.show(self.FIRST)["cards"]], [])
        # The open sprint's card in flight keeps riding its cycle.
        self.assertIn("ummanu-511", self.advanced(result))

    def test_a_po_comment_after_the_close_wakes_nothing_and_the_tick_still_ends_the_head(self) -> None:
        """Criteria 4 and 5 of secretary-1578, against the production tick itself.

        A PO may add the outcome to a sprint after it closed (issue:9eee1d8ee505bc4ecdc2). That
        comment is a semantic event on an entity whose observer is over, and what must not happen is
        a wake or a launch for it. The probe is the tick: the close stops no head itself, and the
        reconciliation of the observer records against the sprint board is what ends this one --
        `dispatcher_observer`, "closed or gone sprint -> stop the head and drop the record".
        """
        self.settled_pair()
        self.assertEqual(self.host.observers, ["sprint:1"])

        settle_dispatcher_work(
            self.data_dir,
            [card["ref"] for card in self.runtime.sprints.show(self.FIRST)["cards"]],
        )
        self.sprint_writer.close(
            role="po",
            actor="operator",
            reference=self.FIRST,
            decisions=close_decisions(self.sprint_writer, self.FIRST),
        )
        # The close itself took no head down: that is the tick's, and it has not run yet.
        self.assertEqual(self.host.stopped_observers, [])
        self.sprint_writer.comment(
            role="po",
            actor="operator",
            reference=self.FIRST,
            body="PO: the deferred findings ride on the next sprint",
            request_id="po-after-close",
        )

        result = self.runtime.production_tick()

        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})
        # Nothing was woken or launched for the comment: the sprint is not open, so the tick never
        # reaches the event state it would have woken a head from. `host.observers` is every head
        # this host was ever asked to raise, so what is asserted is that it gained none.
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(
            [action["action"] for action in self.actions(result) if action["sprint"] == self.FIRST],
            ["observer-stopped"],
        )
        self.assertEqual(self.runtime.sprints.show(self.FIRST)["status"], "closed")

    def test_closing_the_second_sprint_leaves_the_first_live_and_claiming(self) -> None:
        """The other way round: the sprint closed here is not the first one opened."""
        self.settled_pair()

        settle_dispatcher_work(
            self.data_dir,
            [card["ref"] for card in self.runtime.sprints.show(self.SECOND)["cards"]],
        )
        self.sprint_writer.close(
            role="po",
            actor="operator",
            reference=self.SECOND,
            decisions=close_decisions(self.sprint_writer, self.SECOND),
        )
        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show(self.SECOND)["status"], "closed")
        self.assertEqual(self.host.stopped_observers, [])
        # The head of the sprint that stayed open is untouched and still alive.
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertTrue(observer_alive(self.observers()[self.FIRST])["alive"])
        self.assertEqual(self.runtime.sprints.show(self.FIRST)["status"], "open")
        self.assertEqual(self.claimed(result)[0]["pilot_ref"], "fourth-1")
        self.assertIn("ummanu-510", self.advanced(result))
        # The closed sprint's own Ready card is not left alone on the board any more: its
        # disposition archived it with the close, so no later pass reaches it at all.
        self.assertEqual(self.skipped(self.runtime.production_tick()), [])
        self.assertEqual([card["ref"] for card in self.runtime.sprints.show(self.SECOND)["cards"]], [])

    # two open sprints, one head each ----------------------------------------

    def test_both_open_sprints_run_their_own_head_bound_to_themselves(self) -> None:
        """The pair the admission ceiling used to refuse: two heads observing at once.

        What makes it safe is the binding each head is launched with, so the assertion is not
        only that both came up but that neither carries the other's sprint.
        """
        result = self.observed_pair()

        self.assertEqual(sorted(self.host.observers), [self.FIRST, self.SECOND])
        self.assertEqual(
            sorted(identity[OBSERVER_SPRINT_ENV] for identity in self.host.observer_identities),
            [self.FIRST, self.SECOND],
        )
        # One generation per head, and the record of each sprint carries its own.
        generations = {
            identity[OBSERVER_SPRINT_ENV]: identity[OBSERVER_GENERATION_ENV]
            for identity in self.host.observer_identities
        }
        self.assertEqual(len(set(generations.values())), 2)
        records = self.observers()
        self.assertEqual(sorted(records), [self.FIRST, self.SECOND])
        for reference in (self.FIRST, self.SECOND):
            self.assertEqual(records[reference].generation, generations[reference])
            self.assertTrue(observer_alive(records[reference])["alive"])
        self.assertNotEqual(records[self.FIRST].workspace, records[self.SECOND].workspace)
        self.assertNotEqual(records[self.FIRST].handle, records[self.SECOND].handle)
        self.assertEqual(
            {action["sprint"] for action in self.actions(result)},
            {self.FIRST, self.SECOND},
        )

    def test_each_head_is_woken_only_by_the_card_events_of_its_own_sprint(self) -> None:
        """One tick, one event per sprint, two deliveries that never touch each other."""
        self.observed_pair()
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-510",
            body="the first sprint's card changed",
            request_id="event-of-first-sprint",
        )

        first = self.runtime.production_tick()

        self.assertEqual(self.host.observer_nudges, [self.FIRST])
        self.assertEqual(
            [(action["sprint"], action["action"]) for action in self.actions(first)],
            [(self.FIRST, "observer-nudged"), (self.SECOND, "observer-idle")],
        )
        records = self.observers()
        self.assertEqual(records[self.FIRST].delivery.stage, DeliveryStage.AWAITING_ACK)
        # The other head is not merely unnudged: nothing of the batch reached its cursor.
        self.assertEqual(records[self.SECOND].delivery.stage, DeliveryStage.IDLE)
        self.assertEqual(records[self.SECOND].delivery.delivery_id, "")
        self.assertEqual(records[self.SECOND].delivery.through_event, "")

        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference="ummanu-511",
            body="the second sprint's card changed",
            request_id="event-of-second-sprint",
        )

        second = self.runtime.production_tick()

        # The first sprint's head is redelivered its own batch, because it was seen ready for
        # input without having acknowledged it; the second sprint's batch is a first delivery.
        self.assertEqual(self.host.observer_nudges, [self.FIRST, self.FIRST, self.SECOND])
        self.assertEqual(
            [(action["sprint"], action["action"]) for action in self.actions(second)],
            [(self.FIRST, "observer-redelivered"), (self.SECOND, "observer-nudged")],
        )
        records = self.observers()
        self.assertNotEqual(
            records[self.FIRST].delivery.delivery_id,
            records[self.SECOND].delivery.delivery_id,
        )
        self.assertNotEqual(
            records[self.FIRST].delivery.through_event,
            records[self.SECOND].delivery.through_event,
        )

    def test_one_heads_acknowledgement_does_not_clear_the_others_delivery(self) -> None:
        """Each cursor is closed by its own sprint's resume, and by no other."""
        self.observed_pair()
        for reference, body, request in (
            ("ummanu-510", "first changed", "cursor-event-first"),
            ("ummanu-511", "second changed", "cursor-event-second"),
        ):
            self.writer.comment(
                role="dispatcher",
                actor="dispatcher",
                reference=reference,
                body=body,
                request_id=request,
            )
        self.runtime.production_tick()
        self.assertEqual(sorted(self.host.observer_nudges), [self.FIRST, self.SECOND])
        held = self.observers()[self.SECOND].delivery

        self.acknowledge_delivery(
            {
                "selected_step": "read the board",
                "selected_why": "a card changed",
                "rejected_alternatives": "wait",
                "current_task": "ummanu-510",
                "dod_state": "open",
                "next_safe_step": "resume",
            },
            request_id="ack-of-first-sprint",
            reference=self.FIRST,
        )
        result = self.runtime.production_tick()

        self.assertEqual(
            [(action["sprint"], action["action"]) for action in self.actions(result)],
            [(self.FIRST, "observer-idle"), (self.SECOND, "observer-redelivered")],
        )
        records = self.observers()
        self.assertEqual(records[self.FIRST].delivery.stage, DeliveryStage.IDLE)
        self.assertTrue(records[self.FIRST].delivery.acknowledged_through)
        # The second sprint's head still owes an answer for its own batch: the resume of the
        # first sprint moved neither its cursor nor its stage, and it is redelivered instead.
        self.assertEqual(records[self.SECOND].delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(records[self.SECOND].delivery.through_event, held.through_event)
        self.assertEqual(records[self.SECOND].delivery.acknowledged_through, "")
        self.assertNotEqual(
            records[self.SECOND].delivery.through_event,
            records[self.FIRST].delivery.acknowledged_through,
        )

    def test_a_dead_head_holds_its_own_sprint_while_the_other_keeps_running(self) -> None:
        """A head that failed is one sprint's outage, with two heads open as with one."""
        self.observed_pair_in_flight()
        self.kill_observer(self.FIRST)

        result = self.runtime.production_tick()

        fenced = [action for action in result["actions"] if action["step"] == "observer-fence"]
        self.assertEqual([action["sprint"] for action in fenced], [self.FIRST])
        # The second sprint reconciles and claims inside the same tick the first is held in.
        self.assertEqual(
            [action["pilot_ref"] for action in result["actions"] if action["step"] == "advance"],
            ["ummanu-511"],
        )
        self.assertEqual(self.claimed(result)[0]["pilot_ref"], "third-1")
        self.assertEqual(
            self.skipped(result),
            [
                {
                    "ref": "fourth-1",
                    "reason": "the sprint holding this project has no working declared observer",
                }
            ],
        )
        self.assertEqual(self.reader.show("fourth-1")["state"], "ready")
        self.assertEqual(self.reader.show("third-1")["state"], "in_progress")
        # The surviving head is untouched: the outage and its bring-up belong to the first sprint
        # alone. The dead head's own pane is the only one taken down (secretary-1478 — before it,
        # a dead head with no semantic work pending was left where it was, and its sprint stayed
        # fenced).
        self.assertTrue(observer_alive(self.observers()[self.SECOND])["alive"])
        self.assertEqual(self.host.stopped_observers, ["observer:" + self.FIRST])
        self.assertEqual(self.observers()[self.SECOND].launches, 1)
        self.assertEqual(self.observers()[self.FIRST].launches, 2)

    def test_a_head_that_will_not_take_its_wake_does_not_hold_the_other_sprint(self) -> None:
        """The hang, not the crash: the pane is alive and never takes the prompt.

        A wake that fails is a bounded retry on that sprint's own delivery, so the other
        sprint's head is nudged in the same tick and both sprints keep claiming.
        """
        self.observed_pair_in_flight()
        for reference, request in (
            ("ummanu-510", "hung-event-first"),
            ("ummanu-511", "hung-event-second"),
        ):
            self.writer.comment(
                role="dispatcher",
                actor="dispatcher",
                reference=reference,
                body="card changed",
                request_id=request,
            )
        nudge = self.host.nudge_observer
        self.host.observer_nudges.clear()

        def refuse_the_first_sprints_wake(record, **context: object):
            if str(record.sprint) == self.FIRST:
                raise HostError("the pane never took the prompt")
            return nudge(record, **context)

        with mock.patch.object(self.host, "nudge_observer", side_effect=refuse_the_first_sprints_wake):
            result = self.runtime.production_tick()

        self.assertEqual(
            [(action["sprint"], action["action"]) for action in self.actions(result)],
            [(self.FIRST, "observer-wake-deferred"), (self.SECOND, "observer-nudged")],
        )
        self.assertEqual(self.host.observer_nudges, [self.SECOND])
        records = self.observers()
        self.assertEqual(records[self.FIRST].delivery.stage, DeliveryStage.RETRY_DEFERRED)
        self.assertEqual(records[self.SECOND].delivery.stage, DeliveryStage.AWAITING_ACK)
        # A deferred wake is not a fence: both sprints keep advancing and claiming.
        self.assertEqual(
            [action for action in result["actions"] if action["step"] == "observer-fence"],
            [],
        )
        self.assertEqual(
            sorted(action["pilot_ref"] for action in result["actions"] if action["step"] == "advance"),
            ["ummanu-510", "ummanu-511"],
        )


class ClaudeObserverProviderContractTests(unittest.TestCase):
    def test_current_launch_retains_its_pre_pane_baseline_and_records_progress(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(Path(tmp) / "claude-projects")}),
        ):
            root = Path(tmp)
            catalog = FakeCatalog()
            catalog.profiles["claude-observer"] = {
                "adapter": "claude",
                "model": "opus",
                "resource": "claude-sub",
                "runtime": "orca-legacy",
            }
            host = CommandHostRuntime(catalog, root / "data", mode="noop")
            workspace = host.observer_workspace("sprint:1")
            run_id = "claude-observer-run"
            prepared = host.preflight_codex_run(
                "claude-observer",
                role="observer",
                workspace=workspace,
                task_ref=TaskRef.sprint("sprint:1"),
                pid_file=host.observer_pid_file("sprint:1"),
                run_id=run_id,
            )
            before = prepared.fanout_policy["provider_progress_source"]
            launched = host.prepare_observer(
                {"ref": "sprint:1"},
                "claude-observer",
                prompt="# Sprint sprint:1\n",
                heartbeat_run_id=run_id,
            )
            run = HeadRun.from_json(launched["head_run"])
            self.assertEqual(run.spec.adapter, "claude")
            self.assertEqual(run.fanout_policy["provider_progress_source"], before)

            transcript = Path(before["root"]) / claude_project_dir_name(workspace) / "observer-session.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                '{"type":"assistant","sessionId":"claude-observer-session"}\n', encoding="utf-8"
            )
            record = ObserverRecord(
                sprint="sprint:1",
                workspace=workspace,
                head_run=run.to_json(),
            )

            baseline = host.observer_provider_progress(record)

            self.assertEqual(baseline["state"], "observed")
            bound = record.head_run["fanout_policy"]["provider_progress_source"]
            self.assertEqual(bound["state"], "bound")
            self.assertEqual(bound["path"], str(transcript.resolve()))
            transcript.write_text(
                '{"type":"assistant","sessionId":"claude-observer-session"}\n{"type":"assistant"}\n',
                encoding="utf-8",
            )

            progressed = host.observer_provider_progress(record)

        self.assertEqual(progressed["state"], "observed")
        self.assertNotEqual(progressed["cursor"], baseline["cursor"])
        self.assertEqual(progressed["head_run_id"], run_id)


class ObserverConfigurationTests(unittest.TestCase):
    def test_the_observer_head_comes_from_its_own_role_default(self) -> None:
        canonical = canonical_heads(Path(__file__).resolve().parents[1])
        role_defaults = canonical["role_defaults"]

        self.assertIn("observer", role_defaults)
        self.assertNotEqual(role_defaults["observer"], role_defaults["new_card"])
        self.assertIn(role_defaults["observer"], canonical["profiles"])

    def test_a_registry_without_the_key_is_refused_by_that_key(self) -> None:
        catalog = FakeCatalog()
        catalog.role_defaults.pop("observer")

        with self.assertRaisesRegex(HostError, r"role_defaults\.observer"):
            catalog.observer_head()

    def test_the_observer_runs_with_the_role_scoped_environment(self) -> None:
        self.assertIn("observer", ROLE_ALLOWLIST)
        # The shared non-secret environment plus the two names that say which sprint this head observes.
        self.assertEqual(
            ROLE_ALLOWLIST["observer"],
            (
                "UMMANU_INSTANCE",
                "UMMANU_DATA_DIR",
                "UMMANU_REPO",
                OBSERVER_SPRINT_ENV,
                OBSERVER_GENERATION_ENV,
                "UMMANU_MEMORY_ACCESS_TOKEN",
            ),
        )

        env_file = Path(tempfile.mkdtemp()) / "runtime.env"
        env_file.write_text(
            legacy_runtime_lines() + "UNRELATED_SECRET_TOKEN=leak\n",
            encoding="utf-8",
        )

        env = runtime_env("observer", base_env={"PATH": "/usr/bin"}, env_file=env_file)

        self.assertEqual(env["BOARD_ROLE"], "observer")
        self.assertNotIn(LEGACY_ENV[0], env)
        self.assertNotIn("UNRELATED_SECRET_TOKEN", env)

    def test_runtime_env_cannot_rename_the_sprint_a_head_observes(self) -> None:
        """`runtime.env` is a file inside an installation, and the binding is not its to give.

        A line there naming another sprint is how a head would come up able to write on work it
        was never launched for, so the launcher's value wins the way the installation binding
        does. A head the launcher bound to nothing takes nothing from the file either.
        """
        env_file = Path(tempfile.mkdtemp()) / "runtime.env"
        env_file.write_text(
            legacy_runtime_lines() + f"{OBSERVER_SPRINT_ENV}=sprint:somebody-else\n"
            f"{OBSERVER_GENERATION_ENV}=forged\n",
            encoding="utf-8",
        )

        bound = runtime_env(
            "observer",
            base_env={"PATH": "/usr/bin", **observer_binding("sprint:1126", "abc123")},
            env_file=env_file,
        )
        self.assertEqual(bound[OBSERVER_SPRINT_ENV], "sprint:1126")
        self.assertEqual(bound[OBSERVER_GENERATION_ENV], "abc123")

        unbound = runtime_env("observer", base_env={"PATH": "/usr/bin"}, env_file=env_file)
        self.assertNotIn(OBSERVER_SPRINT_ENV, unbound)
        self.assertNotIn(OBSERVER_GENERATION_ENV, unbound)

    def test_half_a_binding_is_no_identity(self) -> None:
        """The launcher renders both names or neither, so a lone sprint came from somewhere else."""
        self.assertEqual(observer_binding("sprint:1126", ""), {})
        self.assertEqual(observer_binding("", "abc123"), {})
        self.assertEqual(
            declared_observer_sprint({OBSERVER_SPRINT_ENV: "sprint:1126"}),
            "",
        )
        self.assertEqual(
            declared_observer_sprint(observer_binding("sprint:1126", "abc123")),
            "sprint:1126",
        )

    def test_the_prompt_is_rendered_from_the_live_sprint(self) -> None:
        prompt = render_observer_prompt(
            {
                "ref": "sprint:9",
                "goal": "make the pipeline autonomous",
                "definition_of_done": "an operator sleeps through a sprint",
                "repositories": ["ummanu", "codegen"],
                "status": "open",
                "current_task": "ummanu-800",
                "budget": {"total": 3},
                "comments": [
                    {"created_at": "same", "body": "[dispatcher]\nfirst"},
                    {"created_at": "same", "body": "[po]\nowner cutoff"},
                ],
                "resume": {
                    "selected_step": "old plan",
                    "next_safe_step": "cut another card",
                },
            },
            skill_path="/shell/skills/observe-sprint/SKILL.md",
        )

        self.assertIn("sprint:9", prompt)
        self.assertIn("make the pipeline autonomous", prompt)
        self.assertIn("an operator sleeps through a sprint", prompt)
        self.assertIn("- ummanu", prompt)
        self.assertIn("- codegen", prompt)
        self.assertIn("ummanu-800", prompt)
        self.assertIn("/shell/skills/observe-sprint/SKILL.md", prompt)
        self.assertIn("python3 -P -m ummanu sprint show --ref sprint:9", prompt)
        self.assertIn("### Comment 2 (applicable PO/owner decision)", prompt)
        self.assertLess(prompt.index("[dispatcher]\nfirst"), prompt.index("[po]\nowner cutoff"))
        self.assertLess(prompt.index("[po]\nowner cutoff"), prompt.index('"next_safe_step"'))
        self.assertIn("no timestamp cutoff", prompt)

    def test_observer_context_distinguishes_loaded_empty_from_unavailable_comments(self) -> None:
        empty = render_observer_prompt({"ref": "sprint:9", "comments": []})
        self.assertIn("## Live sprint comments\n\n", empty)
        self.assertIn("(none)", empty)

        for unavailable in ({"ref": "sprint:9"}, {"ref": "sprint:9", "comments": None}):
            with (
                self.subTest(unavailable=unavailable),
                self.assertRaisesRegex(TaskError, "live sprint comments are unavailable"),
            ):
                render_observer_prompt(unavailable)

    def test_observer_prompt_sources_forbid_subagents(self) -> None:
        document = render_observer_prompt({"ref": "sprint:9", "comments": []})
        launch = observer_launch_prompt()

        for prompt in (document, launch):
            self.assertIn("Do not spawn", prompt)
            self.assertIn("subagents", prompt)

    def test_the_prompt_points_at_the_skill_instead_of_restating_it(self) -> None:
        """The launch document carries data and one pointer; the instructions live in the skill."""
        prompt = render_observer_prompt(
            {"ref": "sprint:9", "goal": "goal", "definition_of_done": "dod", "comments": []},
            skill_path="/shell/skills/observe-sprint/SKILL.md",
        )
        canonical = (
            Path(__file__).resolve().parents[1]
            / "skills"
            / "roles"
            / "observer"
            / "observe-sprint"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

        # Every heading the skill owns is a section the prompt must not carry a second copy of.
        headings = [line for line in canonical.splitlines() if line.startswith("## ")]
        self.assertTrue(headings)
        self.assertEqual([heading for heading in headings if heading in prompt], [])
        self.assertNotIn("sprint resume", prompt)
        self.assertNotIn("task create", prompt)

    def test_request_ids_are_stable_per_launch_generation(self) -> None:
        self.assertEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-a", 1),
        )
        self.assertNotEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-a", 2),
        )
        # Two lifecycles of the same sprint reference are two different requests.
        self.assertNotEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-b", 1),
        )

    def test_each_record_gets_its_own_generation(self) -> None:
        self.assertNotEqual(
            ObserverRecord(sprint="sprint:1").generation,
            ObserverRecord(sprint="sprint:1").generation,
        )

    def test_a_record_that_names_no_head_refuses_instead_of_reading_busy(self) -> None:
        """A head that cannot be addressed is not a working one, whatever its pid says."""
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            adopted = ObserverRecord(sprint="sprint:1", workspace="/workspace")

            # A record whose handle died with the tick that launched it: nothing to read at all.
            with self.assertRaises(HostError):
                host.observer_status(adopted)

    def test_real_host_nudge_carries_the_active_delivery_marker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            host, backend, workspace = _supervised_observer_host(Path(root))
            record = ObserverRecord(
                sprint="sprint:1",
                head="codex-observer",
                workspace=str(workspace),
                handle="run:observer-1",
                head_run=_supervised_observer_run(workspace),
                delivery=ObserverDelivery(
                    delivery_id="delivery-1",
                    through_event="evt-card-1",
                ),
            )
            owner_decision = "[po]\nclose after ummanu-1591; create no new cards\n" + "x" * 70_000
            seen_at_delivery: list[str] = []
            backend.on_deliver = lambda: seen_at_delivery.append(
                (workspace / "SPRINT.md").read_text(encoding="utf-8")
            )

            host.nudge_observer(
                record,
                sprint={
                    "ref": "sprint:1",
                    "comments": [
                        {
                            "created_at": "2026-09-07T23:00:35Z",
                            "body": owner_decision,
                        }
                    ],
                    "resume": {"next_safe_step": "create ummanu-1592"},
                },
                change="sprint-entity",
            )
            document_text = (workspace / "SPRINT.md").read_text(encoding="utf-8")

        [(_run, pointer, subject)] = backend.deliveries
        message = pointer.text
        self.assertEqual(subject, "observer-wake")
        # The whole document is on disk before the pointer at it is handed over.
        self.assertIn(owner_decision, seen_at_delivery[0])
        self.assertNotIn("\n", message)
        self.assertLessEqual(len(message.encode("utf-8")), 256)
        self.assertEqual(message, f"Read {workspace / 'SPRINT.md'} and do its task.")
        self.assertGreater(len(document_text.encode("utf-8")), 64 * 1024)
        self.assertIn("delivery_id: delivery-1", document_text)
        self.assertIn("through_event: evt-card-1", document_text)
        self.assertIn("only when that receipt exists", document_text)
        self.assertIn("none/noop/missing evidence proves no broad suite", document_text)
        self.assertTrue(document_text.startswith("The sprint entity changed."))
        self.assertIn("### Comment 1 (applicable PO/owner decision)", document_text)
        self.assertIn(owner_decision, document_text)
        self.assertLess(
            document_text.index("close after ummanu-1591"), document_text.index('"next_safe_step"')
        )
        broad = "worker-local broad receipt"
        gate = "dispatcher-owned exact-SHA gate receipt"

        def assert_receipt_name_at_site(site: str, expected: str, other: str) -> None:
            _, found, rest = document_text.partition(site)
            self.assertTrue(found, f"observer message must retain receipt site: {site!r}")
            sentence = site + rest.split(".", 1)[0]
            self.assertIn(expected, sentence)
            self.assertNotIn(other, sentence)

        for site, expected in (
            ("Read its worker report, reviewer verdict and any valid executed ", gate),
            ("Keep a ", broad),
        ):
            other = gate if expected == broad else broad
            assert_receipt_name_at_site(site, expected, other)

        corrupted = document_text.replace(
            "Keep a worker-local broad receipt with the worker",
            "Keep a dispatcher-owned exact-SHA gate receipt with the worker",
            1,
        )
        with self.assertRaises(AssertionError):
            _, found, rest = corrupted.partition("Keep a ")
            self.assertTrue(found)
            self.assertIn(broad, "Keep a " + rest.split(".", 1)[0])
    def test_real_host_nudge_api_rejects_a_synchronous_confirm_callback(self) -> None:
        """Observer acknowledgement is out of band and cannot re-enter prompt delivery."""
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            workspace = Path(root) / "observer-workspace"
            workspace.mkdir()
            record = ObserverRecord(
                sprint="sprint:1",
                workspace=str(workspace),
                handle="observer:sprint:1",
                delivery=ObserverDelivery(delivery_id="delivery-2", through_event="evt-card-2"),
            )
            reached = [False]

            def forbidden(_sent_at: float) -> bool:
                reached[0] = True
                return True

            with self.assertRaises(TypeError):
                host.nudge_observer(  # type: ignore[call-arg]
                    record,
                    sprint={"ref": "sprint:1", "comments": []},
                    confirm=forbidden,
                )

        self.assertFalse(reached[0])

    def test_real_host_nudge_refuses_a_wake_the_head_never_took(self) -> None:
        """A wake the backend could not deliver is an explicit failure, never a quiet success."""
        with tempfile.TemporaryDirectory() as root:
            host, backend, workspace = _supervised_observer_host(Path(root))
            backend.deliver_refusal = DeliverReceipt(status=HEAD_GONE, reason="the prompt did not start a turn")
            record = ObserverRecord(
                sprint="sprint:1",
                head="codex-observer",
                workspace=str(workspace),
                handle="run:observer-1",
                head_run=_supervised_observer_run(workspace),
                delivery=ObserverDelivery(delivery_id="delivery-3", through_event="evt-card-3"),
            )

            with self.assertRaises(HostError) as raised:
                host.nudge_observer(record, sprint={"ref": "sprint:1", "comments": []})

        self.assertIn("observer wake was not delivered", str(raised.exception))
        self.assertIn("did not start a turn", str(raised.exception))
        self.assertEqual(backend.deliveries, [])


def _supervised_observer_host(root: Path, catalog: FakeCatalog | None = None):
    """A real host whose heads are on a recording backend, and sprint:1's observer workspace path.

    The path is the one the host places (`<data_dir>/workspaces/observers/<token>`), made as a
    plain directory: what a test here is about is the host around it, not the git worktree, which
    `tests.test_observer_git_workspace` cuts for real.
    """
    host = CommandHostRuntime(catalog or FakeCatalog(), root / "data", mode="real")  # type: ignore[arg-type]
    backend = SupervisedBackend().install(host)
    workspace = Path(host.observer_workspace("sprint:1"))
    workspace.mkdir(parents=True, exist_ok=True)
    return host, backend, workspace


def _supervised_observer_run(workspace: Path, **fields: object) -> dict:
    return supervised_run(
        "observer-run-1",
        profile="codex-observer",
        role="observer",
        workspace=str(workspace),
        task_ref=TaskRef.sprint("sprint:1"),
        handle="run:observer-1",
        **fields,
    )


class RealHostStopObserverTests(unittest.TestCase):
    """The real host, not the fake: a refused stop has to reach the lifecycle."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(
            FakeCatalog(), self.root / "data", mode="real",
            production_runtime=registered_production_runtime(self.root),
        )  # type: ignore[arg-type]
        self.backend = SupervisedBackend().install(self.host)
        self.workspace = Path(self.host.observer_workspace("sprint:1"))
        self.host._create_git_observer_workspace(self.workspace)
        runtime = SimpleNamespace(
            data_dir=self.host.data_dir, host=self.host,
            sprints=SimpleNamespace(show=lambda *a, **k: {
                "id": "sprint-1", "ref": "sprint:1", "status": "open"}),
        )
        self.host.cleanup_owner = CleanupOwner(runtime)
        self.record = ObserverRecord(
            sprint="sprint:1",
            head="observer",
            handle="run:observer-1",
            workspace=str(self.workspace),
            head_possible=True,
            head_run=_supervised_observer_run(self.workspace),
        )

    def assert_workspace_retained(self) -> None:
        self.assertTrue(self.workspace.is_dir())
        self.assertTrue(self.host._git_observer_worktree_listed(str(self.workspace)))

    def test_a_live_foreign_observer_heartbeat_fences_the_head_stop_and_worktree_removal(self) -> None:
        pid_file = self.root / "foreign-observer.pid"
        foreign = subprocess.Popen(["sleep", "5"])

        def reap_foreign() -> None:
            if foreign.poll() is None:
                foreign.terminate()
            foreign.wait()

        self.addCleanup(reap_foreign)
        stat = Path(f"/proc/{foreign.pid}/stat").read_text(encoding="utf-8")
        heartbeat = heartbeat_identity(
            run_id="foreign-observer-run",
            role="observer",
            task="sprint:sprint:1",
            leaf="leaf-observer",
        )
        heartbeat.update(
            {
                "version": 1,
                "pid": foreign.pid,
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
                "proc_starttime_ticks": stat[stat.rfind(")") + 2 :].split()[19],
            }
        )
        pid_file.write_text(json.dumps(heartbeat), encoding="utf-8")
        record = ObserverRecord(
            sprint="sprint:1",
            head="observer",
            handle="run:observer-1",
            leaf="leaf-observer",
            workspace=str(self.workspace),
            head_possible=True,
            pid_file=str(pid_file),
            head_run=_supervised_observer_run(
                self.workspace, leaf="leaf-observer", pid_file=str(pid_file)),
        )

        with mock.patch.object(self.host, "_signal_head") as signal_head:
            with self.assertRaisesRegex(HostError, "mismatching launch identity"):
                self.host.stop_observer(record)

        self.assertEqual(self.backend.stops, [], "no head stop is allowed")
        self.assert_workspace_retained()
        intent = next(iter(self.host.cleanup_owner.journal.read()["intents"].values()))
        self.assertEqual(intent["status"], "pending")
        self.assertFalse(intent["progress"]["heads_stopped"])
        signal_head.assert_not_called()
        self.assertIsNone(foreign.poll())

    def test_a_record_without_a_workspace_is_a_legacy_record_and_is_left_alone(self) -> None:
        """Records written before the launch intent named a workspace: the handle was an Orca pane."""
        legacy = ObserverRecord(sprint="sprint:1", head="observer", handle="term-1")

        with self.assertRaisesRegex(LegacyDispatcherRecord, "names an Orca pane and no workspace"):
            self.host.stop_observer(legacy)

        self.assertEqual(self.backend.stops, [])

    def test_a_refused_stop_raises_instead_of_reporting_success(self) -> None:
        self.backend.stop_refusal = "the head's process outlived the stop it was sent"

        with self.assertRaisesRegex(HostError, "outlived the stop"):
            self.host.stop_observer(self.record)

        self.assert_workspace_retained()
        self.assertEqual(self.host.cleanup_owner.journal.summary()[0]["status"], "pending")

    def test_a_refused_stop_keeps_the_record_and_marks_stop_pending(self) -> None:
        runtime = mock.Mock()
        runtime.host = self.host
        self.backend.stop_refusal = "the head's process outlived the stop it was sent"

        self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertTrue(self.record.head_possible)
        self.assertEqual(self.record.workspace, str(self.workspace))
        self.assert_workspace_retained()

    def test_a_head_that_is_stopped_but_a_worktree_that_will_not_go_is_a_failed_stop(self) -> None:
        """Otherwise the record is dropped while the worktree it named is still registered, and
        nothing is left pointing at it to clean it up."""
        runtime = mock.Mock()
        runtime.host = self.host
        with mock.patch("ummanu.dispatch.cleanup.git_worktree.remove", return_value=False):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertEqual(self.backend.stops, [("observer-run-1", "ummanu-dispatcher")])
        self.assertEqual(self.record.workspace, str(self.workspace))
        self.assert_workspace_retained()
        intent = next(iter(self.host.cleanup_owner.journal.read()["intents"].values()))
        self.assertEqual(intent["status"], "pending")
        self.assertTrue(intent["progress"]["heads_stopped"])

        self.assertTrue(stop_observer_head(runtime, self.record))
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.host._git_observer_worktree_listed(str(self.workspace)))
        self.assertEqual(self.host.cleanup_owner.journal.summary()[0]["status"], "completed")
        self.assertEqual(len(self.backend.stops), 1, "the durable unscoped stop receipt survives retry")

    def test_an_unreadable_answer_is_not_an_absent_workspace(self) -> None:
        """Unreadable registration preserves the worktree and a retryable record after exact stop."""
        runtime = mock.Mock()
        runtime.host = self.host
        # Capture exact registration before the Git answer becomes unreadable.
        self.backend.stop_refusal = "stop not yet confirmed"
        self.assertFalse(stop_observer_head(runtime, self.record))
        self.backend.stops.clear()
        self.backend.stop_refusal = ""
        with mock.patch("ummanu.dispatch.cleanup._registered",
                        side_effect=HostError("cleanup worktree registrations are unreadable")):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertTrue(self.record.head_possible)
        self.assertEqual(self.record.workspace, str(self.workspace))
        self.assertEqual(self.backend.stops, [("observer-run-1", "ummanu-dispatcher")])
        self.assert_workspace_retained()
        intent = next(iter(self.host.cleanup_owner.journal.read()["intents"].values()))
        self.assertEqual(intent["status"], "pending")
        self.assertIn("unreadable", intent["reason"])
        self.assertTrue(intent["progress"]["heads_stopped"])
        self.assertFalse(intent["progress"].get("workspace_removed", False))

    def test_the_pid_file_is_named_before_the_head_exists(self) -> None:
        self.assertEqual(self.host.observer_pid_file("sprint:1"), observer_pid_file("sprint:1"))


class RealHostObserverTeardownTests(unittest.TestCase):
    """The lifecycle over the real host seam: what a closed sprint gives back.

    The bring-up cuts the observer workspace before it starts a head, so a failure after that point
    leaves a worktree behind with no head at all. The record has to keep pointing at it, or the
    sprint closes and the worktree stays with nothing left to remove it.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        install_skill_registry(self.data_dir)
        env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
                "UMMANU_ROLE_SKILLS_MANIFEST": str(self.data_dir / "registry" / "manifest.toml"),
                "UMMANU_INSTANCE": str(self.data_dir / "registry" / "instance"),
                "UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.data_dir / "orca-workspaces"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (self.data_dir / "bodies").mkdir(parents=True, exist_ok=True)
        self.board = card_store(self, dispatcher_seed(), instance_dir=self.data_dir)
        self.catalog = _ObserverCatalog(instance_dir=self.data_dir)
        self.host = CommandHostRuntime(
            self.catalog, self.data_dir, mode="real",
            production_runtime=registered_production_runtime(self.data_dir),
        )  # type: ignore[arg-type]
        self.host.preflight_codex_run = accepted_transport_run  # type: ignore[method-assign]
        self.backend = SupervisedBackend().install(self.host)
        self.audit = task_audit_for(self.board)
        self.runtime = DispatcherRuntime(
            TaskReader(self.board),  # type: ignore[arg-type]
            TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir),  # type: ignore[arg-type]
            self.audit,
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
        )
        self.worktree_create_fails = False
        self.removal_refused = False
        self.create_workspace = self.host._create_git_observer_workspace
        self.remove_worktree = git_worktree.remove
        create = mock.patch.object(self.host, "_create_git_observer_workspace", side_effect=self._create)
        create.start()
        self.addCleanup(create.stop)
        remove = mock.patch("ummanu.dispatch.cleanup.git_worktree.remove", side_effect=self._remove)
        remove.start()
        self.addCleanup(remove.stop)

    @property
    def registered(self) -> bool:
        return self.host._git_observer_worktree_listed(self.host.observer_workspace("sprint:1"))

    def _create(self, workspace: Path) -> Path:
        if self.worktree_create_fails:
            raise HostError("git worktree add failed for the observer workspace: fatal: bad object")
        return self.create_workspace(workspace)

    def _remove(self, run, repo: Path, workspace: Path) -> bool:
        if self.removal_refused:
            return False
        return self.remove_worktree(run, repo, workspace)

    def observers(self) -> dict:
        return load_observers(self.runtime.production_state.load())

    def actions(self, result: dict) -> list[str]:
        return [
            action["action"] for action in result["actions"] if action.get("step") == "observer-reconcile"
        ]

    def open_sprint(self) -> None:
        self.board.add_sprint(
            "sprint:1",
            status="open",
            sprint_observer=encode_observer(head_choice(self.catalog.observer_head())),
        )

    def close_sprint(self) -> None:
        self.board.save_sprint_metadata("sprint:1", sprint_status="closed")

    def test_a_bring_up_that_dies_after_the_worktree_still_gives_it_back_on_closure(self) -> None:
        self.open_sprint()
        self.backend.start_failure = head_ops.HeadSpawnFailed("the observer head never started")

        deferred = self.runtime.production_tick()

        self.assertEqual(self.actions(deferred), ["observer-launch-deferred"])
        record = self.observers()["sprint:1"]
        self.assertFalse(record.head_possible)
        self.assertTrue(record.workspace_live)
        self.assertTrue(self.registered)

        self.close_sprint()
        stopped = self.runtime.production_tick()

        self.assertEqual(self.actions(stopped), ["observer-stopped"])
        self.assertFalse(self.registered)
        self.assertEqual(self.observers(), {})
        self.assertFalse(Path(record.workspace).exists())
        self.assertEqual(self.runtime.cleanup.journal.summary()[0]["status"], "completed")

    def test_a_bring_up_that_never_cut_a_workspace_leaves_nothing_to_remove(self) -> None:
        """An unmaterialized launch intent is settled without deleting an unproven path."""
        self.open_sprint()
        self.worktree_create_fails = True

        self.runtime.production_tick()
        self.close_sprint()
        with mock.patch("ummanu.dispatch.cleanup.git_worktree.remove") as remove:
            stopped = self.runtime.production_tick()

        self.assertEqual(self.actions(stopped), ["observer-stopped"])
        remove.assert_not_called()
        self.assertEqual(self.backend.starts, [])
        self.assertEqual(self.observers(), {})
        self.assertFalse(self.registered)
        intent = next(iter(self.runtime.cleanup.journal.read()["intents"].values()))
        self.assertEqual(intent["status"], "preserved")
        self.assertIn("missing exact", intent["reason"])
        self.assertTrue(intent["progress"]["heads_stopped"])

    def test_a_worktree_that_will_not_go_keeps_the_closed_sprint_on_the_books(self) -> None:
        """A refused teardown of a workspace with no head behind it is still a failed stop: the
        record survives as `stop-pending` and the next tick comes back to it."""
        self.open_sprint()
        self.backend.start_failure = head_ops.HeadSpawnFailed("the observer head never started")
        self.runtime.production_tick()
        self.close_sprint()
        self.removal_refused = True

        failed = self.runtime.production_tick()

        self.assertEqual(self.actions(failed), ["observer-stop-failed"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertTrue(record.workspace_live)
        self.assertTrue(self.registered)
        intent = next(iter(self.runtime.cleanup.journal.read()["intents"].values()))
        self.assertEqual(intent["status"], "pending")
        self.assertTrue(intent["progress"]["heads_stopped"])
        self.assertFalse(intent["progress"].get("workspace_removed", False))

        self.removal_refused = False
        retried = self.runtime.production_tick()

        self.assertEqual(self.actions(retried), ["observer-stopped"])
        self.assertFalse(self.registered)
        self.assertEqual(self.observers(), {})
        self.assertFalse(Path(record.workspace).exists())
        self.assertEqual(self.runtime.cleanup.journal.summary()[0]["status"], "completed")


class RealHostTuiObserverLaunchTests(unittest.TestCase):
    """The real host on the TUI bring-up path: what a failed prompt delivery hands back."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.root / "orca-workspaces"),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.host, self.backend, _workspace = _supervised_observer_host(self.root, _TuiCatalog())
        self.host.preflight_codex_run = accepted_transport_run  # type: ignore[method-assign]
        self.host._create_git_observer_workspace = lambda workspace: workspace  # type: ignore[method-assign]
        self.backend.deliver_refusal = DeliverReceipt(
            status=HEAD_GONE,
            reason="TUI prompt was not delivered",
            failure=head_ops.HeadNudgeFailed("TUI prompt was not delivered"),
        )

    def _prepare(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

    def test_a_stopped_head_reports_a_bring_up_with_nothing_left_running(self) -> None:
        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        [(run_id, _actor)] = self.backend.stops
        self.assertEqual(run_id, self.backend.starts[0]["run"].run_id)
        self.assertEqual(caught.exception.handle, "")

    def test_a_failed_launch_delivery_hands_back_its_evidence_under_its_own_subject(self) -> None:
        """The bring-up prompt is delivered by the same boundary as a wake, and evidenced like one.

        The subject is what keeps the two apart in the sprint's telemetry: a launch delivery that
        failed is not a wake that failed, and neither is a reviewer that would not come up.
        """
        evidence = DeliveryEvidence(
            handle="run:observer",
            stage="payload_written",
            payload_bytes=420,
            payload_sha256="feedfacefeedface",
            reason="payload-left-in-composer",
        )
        self.backend.deliver_refusal = DeliverReceipt(
            status=HEAD_GONE,
            reason="TUI prompt was not delivered",
            failure=head_ops.HeadNudgeFailed("TUI prompt was not delivered", evidence=evidence),
        )

        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertEqual(caught.exception.evidence["subject"], "observer-launch")
        self.assertEqual(caught.exception.evidence["reason"], "payload-left-in-composer")
        self.assertEqual(caught.exception.evidence["payload_bytes"], 420)

    def test_a_head_that_will_not_stop_hands_its_handle_back(self) -> None:
        self.backend.stop_refusal = "the head's process outlived the stop it was sent"

        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertTrue(caught.exception.handle)
        self.assertTrue(caught.exception.workspace)
        self.assertTrue(caught.exception.pid_file)
        self.assertIn("stop failed", str(caught.exception))


class ObserverCodexTrustTests(unittest.TestCase):
    """The bring-up answers codex's trust question for the observer workspace.

    A real `CommandHostRuntime` over a real `InstanceCatalog`: the launcher renders the command and
    prepares the workspace, only the head's backend is a recording stand-in. The git worktree is a
    real one, cut from the real observer repo the runtime creates, because what codex asks about is
    the repository root that worktree hangs off and no fake reproduces that shape.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.codex_home = self.root / "codex-home"
        env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces"),
                "TA_CODEX_HOME": str(self.codex_home),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        catalog = object.__new__(InstanceCatalog)
        catalog._heads = canonical_heads(Path(__file__).resolve().parents[1])  # type: ignore[attr-defined]
        self.host = CommandHostRuntime(catalog, self.root / "data", mode="real")  # type: ignore[arg-type]
        # This suite's subject is the shared trust write after an independently captured allow;
        # it supplies that boundary explicitly rather than letting profile data imply it.
        self.host.preflight_codex_run = self._trust_attested_run  # type: ignore[method-assign]
        self.backend = SupervisedBackend().install(self.host)

    @property
    def commands(self) -> list[str]:
        return [str(start["command"]) for start in self.backend.starts]

    def _trust_attested_run(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: TaskRef,
        pid_file: str,
        run_id: str,
    ) -> HeadRun:
        ensure_codex_workspace_trusted(self.host.catalog.head_profile(head), workspace)
        return accepted_transport_run(
            head,
            role=role,
            workspace=workspace,
            task_ref=task_ref,
            pid_file=pid_file,
            run_id=run_id,
        )

    def test_the_bring_up_trusts_the_repository_root_of_the_observer_workspace(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-sol-high", prompt="# Sprint\n")

        trusted = tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))
        workspace = Path(self.host.observer_workspace("sprint:1")).resolve()
        repo_root = observer_root_repo(self.root / "data").resolve()
        self.assertEqual(trusted["projects"][str(repo_root)]["trust_level"], "trusted")
        self.assertEqual(trusted["projects"][str(workspace)]["trust_level"], "trusted")
        command = self.commands[0]
        self.assertIn(f"CODEX_HOME={self.codex_home} codex", command)
        self.assertNotIn("codex exec", command)
        self.assertIn("--role observer", command)

    def test_sprint_project_declarations_do_not_gate_its_observer_repository(self) -> None:
        sprint = {
            "ref": "sprint:1425",
            "repositories": [str((self.root / "projects" / "ummanu").resolve())],
            "reservations": ["unavailable-project"],
        }

        launched = self.host.prepare_observer(sprint, "codex-sol-high", prompt="# Sprint 1425\n")

        self.assertTrue(launched["handle"])
        self.assertEqual(len(self.commands), 1)

    def test_a_second_bring_up_leaves_the_recorded_trust_alone(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-sol-high", prompt="# Sprint\n")
        first = (self.codex_home / "config.toml").read_text(encoding="utf-8")

        self.host.prepare_observer({"ref": "sprint:1"}, "codex-sol-high", prompt="# Sprint\n")

        self.assertEqual((self.codex_home / "config.toml").read_text(encoding="utf-8"), first)

    def test_worker_and_reviewer_launches_trust_their_workspace_too(self) -> None:
        """Trust is written for every codex head, not for the observer alone.

        This test asserted the opposite until secretary-1173: worker and reviewer bring-up was
        expected to leave the codex config untouched, on the reasoning that their workspaces are
        worktrees of repositories the runtime already trusts. That reasoning describes a host that
        has been running codex heads for a while, not the product's own contract — on a clean host
        no such entry exists, and every codex head is now a TUI that will not take a prompt until
        the dialog is answered. So the role no longer decides: what decides is that the head is an
        interactive codex one.
        """
        workspace = self.root / "worker-workspace"
        workspace.mkdir()

        for head, role in (("codex-terra-high", "worker"), ("codex-sol-medium", "reviewer")):
            with self.subTest(role=role):
                launch = self.host.catalog.head_launch(head, "TASK.md", workspace=str(workspace), role=role)
                self.assertIn(
                    f"CODEX_HOME={self.codex_home} codex --dangerously-bypass-approvals-and-sandbox",
                    launch.command,
                )
                self.assertNotIn("codex exec", launch.command)
                self.assertIn(f"--role {role}", launch.command)
                trusted = tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))
                self.assertEqual(trusted["projects"][str(workspace.resolve())]["trust_level"], "trusted")


class _ObserverCatalog(FakeCatalog):
    """An observer profile whose head takes its prompt on the command line."""

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ):
        return HeadCommand(f"run-{role}")


class _TuiCatalog(FakeCatalog):
    """An observer profile whose head takes its prompt after the terminal is already up."""

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ):
        return HeadCommand(f"run-{role}", prompt_after_start=True)


if __name__ == "__main__":
    unittest.main()
