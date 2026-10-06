"""secretary-1799, ummanu-108: a head whose turn ended on a provider error falls back, it does not stall.

These drive one card through the real dispatcher tick with the fake host. The provider error each
test feeds in is parsed by the real reader (`runtime.provider_errors`) from the record shapes the
providers write; which run it belongs to is the fake host's only scripted fact.
"""

from __future__ import annotations

import json
import time
import unittest
from typing import Any
from unittest import mock

from tests.dispatcher_fixtures import DispatcherRuntimeFixture
from ummanu.dispatch.production import _budget_event_type
from ummanu.dispatch.provider_failure import PROVIDER_UNAVAILABLE_READY_ACTION
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.dispatch.types import STOPPED_BY_PROVIDER_FAILURE
from ummanu.head_health import resource_health_path, until_text
from ummanu.runtime.provider_errors import (
    KIND_QUOTA,
    ProviderError,
    claude_first_turn_failure,
    claude_turn_failure,
    codex_first_turn_failure,
    codex_turn_failure,
)

REF = "ummanu-510"

# The 2026-09-25 reviewer's rollout tail, as Codex wrote it (codegen-orchestrator-1373): the turn
# opened at 22:58:11 and closed at 22:58:41 on the backend's 401. The key Codex echoed back is
# masked the way Codex itself masks it.
CODEX_401_ERROR = (
    "unexpected status 401 Unauthorized: Incorrect API key provided: sk-svcac"
    + "*" * 40
    + "fvMA. You can find your API key at https://platform.openai.com/account/api-keys., "
    "url: https://chatgpt.com/backend-api/codex/responses, cf-ray: a40da28f7896ec3e-VNO, "
    "request id: 831b9421-ddf3-4abf-bf14-f013e47cd11e"
)


def codex_401_turn(started_at: float) -> list[dict[str, Any]]:
    return [
        {"type": "session_meta", "payload": {"session_id": "s-1", "cwd": "/w"}},
        {
            "timestamp": _iso(started_at),
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t-1"},
        },
        {"type": "response_item", "payload": {"type": "message", "role": "user"}},
        {
            "timestamp": _iso(started_at + 35),
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "t-1",
                "last_agent_message": None,
                "error": {"message": CODEX_401_ERROR, "codex_error_info": "other"},
            },
        },
    ]


# The 2026-10-06 reviewer (issue:ee68af70cc409f458615), as Codex wrote it: a turn that had been
# working for a while ended on the spent subscription.
CODEX_USAGE_LIMIT = (
    "You\u2019ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
    "credits or try again at Oct 9th, 2026 9:11 PM."
)


def codex_usage_limit_after_work(started_at: float) -> list[dict[str, Any]]:
    """A clean first turn, then a second turn that ran into the usage limit mid-way."""
    return [
        {"type": "session_meta", "payload": {"session_id": "s-1", "cwd": "/w"}},
        {"timestamp": _iso(started_at), "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": _iso(started_at + 600),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "read the diff, checking tests"},
        },
        {"timestamp": _iso(started_at + 601), "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": _iso(started_at + 634),
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": None,
                "error": {"message": CODEX_USAGE_LIMIT, "codex_error_info": "usage_limit_exceeded"},
            },
        },
    ]


def claude_weekly_limit_turn(started_at: float) -> list[dict[str, Any]]:
    """A Claude head that answered once, then ran into its weekly limit."""
    return [
        {"type": "user", "timestamp": _iso(started_at), "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "timestamp": _iso(started_at + 30),
            "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]},
        },
        {"type": "user", "timestamp": _iso(started_at + 40), "message": {"role": "user", "content": "next"}},
        {
            "type": "assistant",
            "timestamp": _iso(started_at + 41),
            "isApiErrorMessage": True,
            "apiErrorStatus": 429,
            "error": "rate_limit",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "You've hit your weekly limit \u00b7 resets Oct 7, 1am (UTC)"}],
            },
        },
    ]


def claude_auth_turn(started_at: float) -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "timestamp": _iso(started_at),
            "message": {"role": "user", "content": "read TASK.md"},
        },
        {
            "type": "assistant",
            "timestamp": _iso(started_at + 4),
            "isApiErrorMessage": True,
            "apiErrorStatus": 401,
            "error": "authentication_failed",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "API Error: 401 invalid token · Please run /login"}],
            },
        },
    ]


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int(epoch % 1 * 1000):03d}Z"


class ProviderFailureFallbackTests(DispatcherRuntimeFixture, unittest.TestCase):
    def _record(self) -> DispatcherRecord:
        return self.runtime.production_state.records(self.runtime.production_state.load())[REF]

    def _fail(self, kind: str, error: ProviderError) -> str:
        """Mark the role's current run as the one whose first turn ended on `error`."""
        record = self._record()
        run_id = str((record.review_head_run if kind == "review" else record.worker_head_run)["run_id"])
        self.host.__dict__.setdefault("failed_runs", {})[run_id] = error
        return run_id

    def _resource_health(self) -> dict[str, Any]:
        path = resource_health_path(self.data_dir)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _comments(self) -> list[str]:
        return [comment["body"] for comment in self.reader.show(REF).get("comments") or []]

    def _budget_types(self) -> list[str]:
        events = self.writer.audit.events(REF)
        return [kind for kind in (_budget_event_type(event) for event in events) if kind]

    def _assert_no_stall_path(self, outcomes: list[dict[str, Any]]) -> None:
        actions = [str(outcome.get("action") or "") for outcome in outcomes]
        for forbidden in ("stall-suspected", "respawned", "escalate", "guard-refused"):
            self.assertFalse([a for a in actions if forbidden in a], (forbidden, actions))
        for body in self._comments():
            self.assertNotIn("respawned the", body)
            self.assertNotIn("Another stall escalates", body)

    # ---- AC1: the 2026-09-25 reviewer -------------------------------------------------------

    def test_codex_reviewer_401_falls_back_to_the_chain_head_on_the_next_tick(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        started = self.tick()
        self.assertEqual(started["action"], "review-started")
        launched_at = self._record().review_started_at
        error = codex_first_turn_failure(codex_401_turn(launched_at))
        assert error is not None
        failed_run = self._fail("review", error)

        # The fake clock: the tick that observes the turn's end runs a minute after the error,
        # long before any stall threshold, and the relaunch happens on it.
        tick_at = error.at + 60
        with mock.patch("time.time", return_value=tick_at):
            fell_back = self.tick()

        self.assertEqual(fell_back["action"], "review-provider-fallback")
        self.assertEqual(fell_back["status"], "ok")
        self.assertEqual(fell_back["head"], "codex-reviewer")
        self.assertEqual(fell_back["resource"], "openai-sub")
        self.assertEqual(fell_back["switched_to"], "claude-opus")
        self.assertIn("401 Unauthorized: Incorrect API key provided", fell_back["error"])
        self.assertNotIn("sk-svcac", json.dumps(fell_back))
        self.assertNotIn("cf-ray", json.dumps(fell_back))
        record = self._record()
        self.assertEqual(record.review_head, "claude-opus")
        self.assertEqual(record.preferred_review_head, "codex-reviewer")
        self.assertNotEqual(record.review_head_run["run_id"], failed_run)
        self.assertLessEqual(record.review_started_at - error.at, 300)
        self.assertEqual(self.host.reviews, [REF, REF])
        self.assertEqual(self.host.review_stop_initiators, [STOPPED_BY_PROVIDER_FAILURE])
        # Nothing was charged: no respawn, no round, no budget.
        self.assertEqual(record.review_respawns, 0)
        self.assertEqual(record.report_generation, 1)
        self.assertEqual(self._budget_types(), [])
        health = self._resource_health()["openai-sub"]
        self.assertEqual(health["status"], "unavailable")
        self.assertIn("codex-reviewer", health["reason"])
        self.assertNotIn("sk-svcac", health["reason"])
        card = self.reader.show(REF)
        self.assertEqual(card["state"], "validate")
        comment = self._comments()[-1]
        for part in ("codex-reviewer", "openai-sub", "401 Unauthorized", "claude-opus"):
            self.assertIn(part, comment)
        self.assertNotIn("sk-svcac", comment)

        # The replacement is left to work: the next ticks wait on its verdict.
        waited = self.tick()
        self.assertEqual(waited["action"], "waiting-review-verdict")
        self._assert_no_stall_path([started, fell_back, waited])

    # ---- AC2: a worker's first turn ---------------------------------------------------------

    def test_codex_worker_401_is_relaunched_on_its_chain_head(self) -> None:
        self.catalog.profiles["codex"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        claimed = self.tick()
        self.assertEqual(claimed["step"], "claim")
        record = self._record()
        self.assertEqual(record.head, "codex")
        error = codex_first_turn_failure(codex_401_turn(record.worker_started_at))
        assert error is not None
        self._fail("worker", error)

        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "worker-provider-fallback")
        self.assertEqual(fell_back["switched_to"], "claude-opus")
        self.assertEqual(fell_back["resource"], "openai-sub")
        record = self._record()
        self.assertEqual(record.head, "claude-opus")
        self.assertEqual(record.preferred_head, "codex")
        self.assertEqual(record.worker_respawns, 0)
        self.assertEqual(record.report_generation, 1)
        self.assertIn("restart_worker", self.host.calls)
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self._resource_health()["openai-sub"]["status"], "unavailable")
        self.assertEqual(self._budget_types(), [])
        self.assertIn("relaunched on claude-opus", self._comments()[-1])
        self.assertEqual(self.tick()["action"], "waiting-worker-report")

    def test_codex_worker_401_with_an_empty_chain_is_one_blocked_for_the_operator(self) -> None:
        # ummanu-108 replaces the Ready return of secretary-1799: a chain with nothing launchable
        # means every provider is down, and that is one Blocked naming them, not a silent wait.
        self.start_dispatcher()
        self.tick()
        record = self._record()
        error = codex_first_turn_failure(codex_401_turn(record.worker_started_at))
        assert error is not None
        self._fail("worker", error)

        blocked = self.tick()

        self.assertEqual(blocked["action"], "worker-provider-blocked")
        self.assertEqual(blocked["to"], "blocked")
        self.assertIn("openai-sub unavailable until", blocked["reason"])
        self.assertEqual(self.reader.show(REF)["state"], "blocked")
        self._assert_no_stall_path([blocked])
        moves = [event for event in self.writer.audit.events(REF) if "provider-blocked" in str(event.get("request_id"))]
        self.assertEqual(len(moves), 1)
        self.assertIn("refused by its provider, not stalled", json.dumps(moves[0]))

    def test_the_ready_return_is_no_budget_event_and_a_plain_preempt_still_is(self) -> None:
        def moved(request_id: str) -> dict[str, Any]:
            return {
                "kind": "moved",
                "request_id": request_id,
                "payload": {"from": "in_progress", "to": "ready"},
            }

        ours = attempt_request_id("attempt-1", PROVIDER_UNAVAILABLE_READY_ACTION, REF, "run-1")
        self.assertIsNone(_budget_event_type(moved(ours)))
        self.assertEqual(_budget_event_type(moved("po-preempt-1")), "preempt")

    # ---- AC3: a Claude head ----------------------------------------------------------------

    def test_claude_worker_auth_error_is_handled_the_same_way(self) -> None:
        self.catalog.profiles["claude-opus"]["fallback"] = ["codex"]
        self.start_dispatcher()
        self.board.save_metadata(12, head="claude-opus")
        self.tick()
        record = self._record()
        self.assertEqual(record.head, "claude-opus")
        error = claude_first_turn_failure(claude_auth_turn(record.worker_started_at))
        assert error is not None
        self.assertEqual((error.kind, error.status), ("auth", 401))
        self._fail("worker", error)

        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "worker-provider-fallback")
        self.assertEqual(fell_back["resource"], "claude-sub")
        self.assertEqual(fell_back["switched_to"], "codex")
        self.assertEqual(self._record().head, "codex")
        self.assertEqual(self._resource_health()["claude-sub"]["status"], "unavailable")
        self.assertNotEqual(self.reader.show(REF)["state"], "blocked")

    def test_claude_reviewer_auth_error_falls_back_to_the_codex_chain_head(self) -> None:
        self.catalog.role_defaults["reviewer"] = "claude-default"
        self.catalog.profiles["claude-default"]["fallback"] = ["codex-reviewer"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        error = claude_first_turn_failure(claude_auth_turn(self._record().review_started_at))
        assert error is not None
        self._fail("review", error)

        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "review-provider-fallback")
        self.assertEqual(fell_back["switched_to"], "codex-reviewer")
        self.assertEqual(self._record().review_head, "codex-reviewer")
        self.assertEqual(self._resource_health()["claude-sub"]["status"], "unavailable")

    # ---- AC5: the reviewer's empty chain ------------------------------------------------------

    def test_both_families_down_is_one_blocked_with_both_resources_and_their_resets(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        claude_until = time.time() + 9 * 3600
        self.runtime.head_health.record("claude-sub", "exhausted", "weekly limit", until=claude_until)
        error = codex_turn_failure(codex_usage_limit_after_work(self._record().review_started_at))
        assert error is not None
        self._fail("review", error)

        blocked = self.tick()

        self.assertEqual(blocked["action"], "review-provider-blocked")
        self.assertEqual(blocked["status"], "blocked")
        reason = blocked["reason"]
        self.assertIn(f"openai-sub exhausted until {until_text(blocked['resource_until'])}", reason)
        self.assertIn(f"claude-sub exhausted until {until_text(claude_until)}", reason)
        self.assertEqual(self.reader.show(REF)["state"], "blocked")
        # One Blocked, no respawn into either provider and no stall verdict.
        self.assertEqual(self.host.reviews, [REF])
        self._assert_no_stall_path([blocked])
        blocks = [
            event for event in self.writer.audit.events(REF) if "provider-blocked" in str(event.get("request_id"))
        ]
        self.assertEqual(len(blocks), 1)

    # ---- ummanu-108: a spent subscription, mid-turn ---------------------------------------------

    def test_codex_reviewer_usage_limit_mid_turn_falls_over_to_claude_with_no_wait(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        candidate = self._record().gate_attestation.to_json()
        error = codex_turn_failure(codex_usage_limit_after_work(self._record().review_started_at))
        assert error is not None
        self.assertEqual(error.kind, KIND_QUOTA)
        # The first-turn reading of secretary-1799 would have missed it: the head had a clean turn.
        self.assertIsNone(codex_first_turn_failure(codex_usage_limit_after_work(0.0)))
        failed_run = self._fail("review", error)

        before = time.time()
        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "review-provider-fallback")
        self.assertEqual(fell_back["switched_to"], "claude-opus")
        self.assertEqual(fell_back["resource_status"], "exhausted")
        # Held to the reset Codex named ("Oct 9th, 2026 9:11 PM"), or, once that has passed, to the
        # bounded backoff; never to the probe TTL.
        until = fell_back["resource_until"]
        self.assertEqual(until, error.reset_at if error.reset_at > before else until)
        self.assertGreater(until, before + 3000)
        record = self._record()
        self.assertEqual(record.review_head, "claude-opus")
        self.assertNotEqual(record.review_head_run["run_id"], failed_run)
        # The same candidate is reviewed again; nothing was charged.
        self.assertEqual(record.gate_attestation.to_json(), candidate)
        self.assertEqual(record.review_respawns, 0)
        self.assertEqual(self._budget_types(), [])
        health = self._resource_health()["openai-sub"]
        self.assertEqual(health["status"], "exhausted")
        self.assertEqual(health["until"], until)
        comment = self._comments()[-1]
        for part in ("codex-reviewer", "openai-sub", "usage limit", "claude-opus", until_text(until)):
            self.assertIn(part, comment)
        self.assertNotEqual(self.reader.show(REF)["state"], "blocked")

        # Past the probe TTL the verdict still holds: a cheap probe is no proof of quota.
        with mock.patch("time.time", return_value=before + 600):
            self.assertEqual(self.runtime.head_health.check("codex-reviewer").status, "exhausted")
        waited = self.tick()
        self.assertEqual(waited["action"], "waiting-review-verdict")
        self._assert_no_stall_path([fell_back, waited])

    def test_the_resource_expires_and_the_next_launch_is_on_the_primary_again(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self.board.save_metadata(12, head="claude-opus")
        self._run_worker_to_validate()
        until = time.time() + 3600
        self.runtime.head_health.record("openai-sub", "exhausted", "spent", until=until)

        with mock.patch("time.time", return_value=until + 1):
            started = self.tick()

        self.assertEqual(started["action"], "review-started")
        self.assertEqual(self._record().review_head, "codex-reviewer")

    def test_the_reviewer_held_in_red_runs_on_the_fallback_until_then(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self.board.save_metadata(12, head="claude-opus")
        self._run_worker_to_validate()
        self.runtime.head_health.record("openai-sub", "exhausted", "spent", until=time.time() + 3600)

        started = self.tick()

        self.assertEqual(started["action"], "review-started")
        self.assertEqual(self._record().review_head, "claude-opus")

    def test_a_fallback_review_in_the_workers_own_family_is_recorded_on_the_card(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-default"]
        self.start_dispatcher()
        self.board.save_metadata(12, head="claude-opus")
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self.assertEqual(self._record().head, "claude-opus")
        error = codex_turn_failure(codex_usage_limit_after_work(self._record().review_started_at))
        assert error is not None
        self._fail("review", error)

        fell_back = self.tick()

        self.assertEqual(fell_back["switched_to"], "claude-default")
        self.assertIn("same-family", self._comments()[-1])
        self.assertIn("not a defect", self._comments()[-1])

    def test_claude_worker_weekly_limit_mid_turn_continues_on_codex_in_the_same_workspace(self) -> None:
        self.catalog.profiles["claude-opus"]["fallback"] = ["codex"]
        self.start_dispatcher()
        self.board.save_metadata(12, head="claude-opus")
        self.tick()
        record = self._record()
        workspace = record.workspace
        error = claude_turn_failure(claude_weekly_limit_turn(record.worker_started_at))
        assert error is not None
        self.assertEqual(error.kind, KIND_QUOTA)
        self.assertIsNone(claude_first_turn_failure(claude_weekly_limit_turn(0.0)))
        self._fail("worker", error)
        before = time.time()

        fell_back = self.tick()

        self.assertEqual(fell_back["action"], "worker-provider-fallback")
        self.assertEqual(fell_back["switched_to"], "codex")
        record = self._record()
        self.assertEqual((record.head, record.workspace), ("codex", workspace))
        self.assertEqual(record.worker_respawns, 0)
        self.assertEqual(record.report_generation, 1)
        self.assertEqual(self._resource_health()["claude-sub"]["status"], "exhausted")
        self.assertGreater(self._resource_health()["claude-sub"]["until"], before)
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")

    def test_the_hold_survives_a_record_round_trip(self) -> None:
        record = DispatcherRecord(
            worker="w",
            workspace="/w",
            handle="",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="a",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=1.0,
            review_provider_hold="provider unavailable: openai-sub",
        )
        self.assertEqual(
            DispatcherRecord.from_json(record.to_json()).review_provider_hold, record.review_provider_hold
        )
        record.review_provider_hold = ""
        self.assertNotIn("review_provider_hold", record.to_json())

    # ---- B.4: only the first turn ----------------------------------------------------------

    def test_a_head_with_no_provider_failure_keeps_the_ordinary_wait(self) -> None:
        self.catalog.profiles["codex-reviewer"]["fallback"] = ["claude-opus"]
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        # A failure of some other run is not this head's.
        self.host.__dict__.setdefault("failed_runs", {})["another-run"] = ProviderError("auth", 401, "401")

        waited = self.tick()

        self.assertEqual(waited["action"], "waiting-review-verdict")
        self.assertEqual(self._record().review_head, "codex-reviewer")
        self.assertEqual(self._resource_health(), {})


if __name__ == "__main__":
    unittest.main()
