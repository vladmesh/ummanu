"""A card head's confirmed stop and its successor become one `head_changed` record, read back by the
card's committed history and drawn on the card page's Timeline."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from ummanu.board.head_changes import (
    CAUSE_OPERATOR_STOP,
    CAUSE_PROVIDER_FAILOVER,
    CAUSE_STALL,
    head_change_payload,
    head_changes,
)
from ummanu.dispatch.head_changes import record_head_change
from ummanu.dispatch.state import DispatcherRecord
from ummanu.runtime.head import HeadRun, HeadSpec, StopInitiator, TaskRef
from ummanu.web.page_card import _timeline
from ummanu.webproto.journal import CommittedAudit

REF = "proj-7"


def run(
    run_id: str, profile: str = "codex-high", *, task: TaskRef | None = None, role: str = "worker"
) -> HeadRun:
    return HeadRun(
        run_id=run_id,
        spec=HeadSpec(profile_id=profile, adapter="codex", model="gpt-x", effort="high"),
        workspace="/w/proj-7",
        task_ref=task or TaskRef.card(REF),
        role=role,
    )


class Journal:
    """The committed records of one card, shaped the way the board writer commits a generic record."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return next((record for record in self.records if record["request_id"] == request_id), None)

    def events(self, reference: str = "", *, kind: str = "") -> list[dict[str, Any]]:
        return [
            record
            for record in self.records
            if (not reference or record["ref"] == reference) and (not kind or record["kind"] == kind)
        ]

    def head_changed(self, *, role: str, actor: str, reference: str, payload: dict, request_id: str) -> None:
        self.writes.append(payload)
        self.records.append(
            {
                "event_id": f"evt_{len(self.records)}",
                "occurred_at": f"2026-01-01T00:0{len(self.records)}:00Z",
                "actor": {"role": role, "id": actor},
                "kind": "head_changed",
                "outcome": "success",
                "ref": reference,
                "request_id": request_id,
                "payload": payload,
            }
        )


class RecordHeadChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = Journal()
        self.runtime = SimpleNamespace(audit=self.journal, writer=self.journal, owner="dispatcher-owner")
        self.record = DispatcherRecord(
            worker="proj-7-slug",
            workspace="/w/proj-7",
            handle="pane",
            head="codex-high",
            review_head="claude-review",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

    def change(
        self, before: HeadRun, *, successor: HeadRun | None, cause: str = CAUSE_STALL, **extra: Any
    ) -> None:
        if successor is not None:
            self.record.worker_head_run = successor.to_json()  # type: ignore[assignment]
        record_head_change(
            self.runtime,
            REF,
            self.record,
            kind="worker",
            cause=cause,
            reason="no terminal output for 1900s",
            before=before.to_json(),
            successor=successor is not None,
            **extra,
        )

    def test_a_replacement_on_the_same_profile_names_both_runs(self) -> None:
        self.change(run("run-a"), successor=run("run-b"))

        [change] = head_changes(self.journal.events(REF))
        self.assertEqual((change["role"], change["change"], change["cause"]), ("worker", "replaced", "stall"))
        self.assertEqual((change["before"]["run_id"], change["after"]["run_id"]), ("run-a", "run-b"))
        self.assertEqual(
            (change["before"]["profile"], change["after"]["profile"]), ("codex-high", "codex-high")
        )
        self.assertEqual(change["at"], "2026-01-01T00:00:00Z")

    def test_a_stop_without_successor_has_no_after(self) -> None:
        self.change(run("run-a"), successor=None, cause=CAUSE_OPERATOR_STOP)

        [change] = head_changes(self.journal.events(REF))
        self.assertEqual((change["change"], change["after"]), ("stopped", None))

    def test_a_failover_keeps_the_requested_profile_apart_from_the_run_that_worked(self) -> None:
        self.change(
            run("run-a", "claude-fallback"),
            successor=run("run-b", "codex-next"),
            cause=CAUSE_PROVIDER_FAILOVER,
            requested="codex-high",
        )

        [change] = head_changes(self.journal.events(REF))
        self.assertEqual(
            (change["requested"], change["before"]["profile"], change["after"]["profile"]),
            ("codex-high", "claude-fallback", "codex-next"),
        )

    def test_heads_that_are_not_this_cards_role_write_nothing(self) -> None:
        for before in (
            run("run-o", task=TaskRef.sprint("sprint:9"), role="observer"),
            run("run-s", task=TaskRef.standing("po"), role="po"),
            run("run-x", task=TaskRef.card("proj-8")),
            run("run-r", role="reviewer"),
        ):
            with self.subTest(before=before.run_id):
                self.change(before, successor=None)
        self.change(run("run-own"), successor=None)
        self.assertEqual([write["before"]["run_id"] for write in self.journal.writes], ["run-own"])

    def test_a_run_already_ended_before_the_operation_was_not_stopped_by_it(self) -> None:
        settled = run("run-a").finishing(StopInitiator("review-freeze")).exited()

        self.change(settled, successor=None, cause=CAUSE_OPERATOR_STOP)

        self.assertEqual(self.journal.writes, [])

    def test_a_relaunch_that_left_the_stopped_run_on_the_record_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "without a run of its own"):
            self.change(run("run-a"), successor=run("run-a"))
        self.assertEqual(self.journal.writes, [])

    def test_a_replay_keeps_the_first_facts_and_the_next_run_gets_its_own_record(self) -> None:
        self.change(run("run-a"), successor=run("run-b"))
        self.change(run("run-a"), successor=run("run-c", "codex-other"))
        self.change(run("run-b"), successor=run("run-c"))

        changes = head_changes(self.journal.events(REF))
        self.assertEqual(
            [(change["before"]["run_id"], change["after"]["run_id"]) for change in changes],
            [("run-a", "run-b"), ("run-b", "run-c")],
        )
        self.assertEqual(len({change["request_id"] for change in changes}), 2)


class HeadChangePayloadTests(unittest.TestCase):
    def test_refuses_a_shape_the_contract_lacks(self) -> None:
        before = {"run_id": "run-a", "profile": "codex-high"}
        cases = (
            {"role": "observer"},
            {"cause": "slow"},
            {"reason": " "},
            {"before": {"run_id": "run-a"}},
            {"before": None},
            {"after": {"run_id": "run-a", "profile": "codex-high"}},
        )
        for override in cases:
            arguments: dict[str, Any] = {
                "role": "worker",
                "cause": CAUSE_STALL,
                "reason": "stalled",
                "before": before,
                "after": None,
                **override,
            }
            with self.subTest(override=override), self.assertRaises(ValueError):
                head_change_payload(**arguments)


class TimelineTests(unittest.TestCase):
    def test_a_head_change_is_drawn_in_time_order_beside_the_transitions(self) -> None:
        journal = Journal()
        journal.records.append(
            {
                "record_type": "board.protocol_event",
                "kind": "moved",
                "ref": REF,
                "request_id": "claim",
                "occurred_at": "2026-01-01T00:00:00Z",
                "transition": {"source": "ready", "target": "in_progress"},
                "reason": "claimed",
                "data": {},
            }
        )
        journal.head_changed(
            role="dispatcher",
            actor="dispatcher-owner",
            reference=REF,
            payload=head_change_payload(
                role="reviewer",
                cause=CAUSE_PROVIDER_FAILOVER,
                reason="provider error from openai-sub: 401 Unauthorized",
                before={"run_id": "run-r1", "profile": "codex-reviewer"},
                after={"run_id": "run-r2", "profile": "claude-opus"},
                requested="codex-reviewer",
            ),
            request_id="head-change-r1",
        )
        journal.records.append(
            {
                "record_type": "board.protocol_event",
                "kind": "moved",
                "ref": REF,
                "request_id": "blocked",
                "occurred_at": "2026-01-01T00:05:00Z",
                "transition": {"source": "in_progress", "target": "blocked"},
                "reason": "operator",
                "data": {},
            }
        )
        page = CommittedAudit(journal).page(REF, cursor=None, limit=50, now=0.0)

        html = _timeline(list(reversed(page.items)))

        claimed, change, blocked = (html.index(text) for text in ("claimed", "reviewer replaced", "operator"))
        self.assertLess(claimed, change)
        self.assertLess(change, blocked)
        for text in (
            "provider failover: codex-reviewer → claude-opus",
            "401 Unauthorized",
            "run-r1",
            "run-r2",
        ):
            self.assertIn(text, html)

    def test_a_stop_without_successor_reads_as_such(self) -> None:
        item = {
            "kind": "head_changed",
            "occurred_at": "2026-01-01T00:01:00Z",
            "transition": None,
            "data": head_change_payload(
                role="worker",
                cause=CAUSE_OPERATOR_STOP,
                reason="pipeline freeze by operator: host maintenance",
                before={"run_id": "run-a", "profile": "codex-high"},
                after=None,
            ),
        }

        html = _timeline([item])

        self.assertIn("worker stopped", html)
        self.assertIn("manual stop: codex-high → no successor", html)
        self.assertIn("pipeline freeze by operator: host maintenance", html)


if __name__ == "__main__":
    unittest.main()
