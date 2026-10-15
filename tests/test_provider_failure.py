"""secretary-1799: reading a head's first-turn provider error, without secrets, bound to its run."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.codex_provider_events import _range_digest, _read_source
from ummanu.dispatch.provider_failure import (
    is_provider_unavailable_return,
    provider_failure_for_persisted_run,
    provider_failure_for_run,
)
from ummanu.dispatch.tui import (
    bind_claude_provider_progress_source,
    prepare_claude_provider_progress_source,
)
from ummanu.dispatch.worker_lifecycle import head_run_binding
from ummanu.runtime.claude_sessions import claude_project_dir_name
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef
from ummanu.runtime.provider_errors import (
    KIND_AUTH,
    KIND_PLAN,
    KIND_RATE_LIMIT,
    KIND_RECONNECT,
    KIND_SERVER,
    classify_provider_error,
    claude_first_turn_failure,
    codex_first_turn_failure,
    codex_turn_failure,
    screen_turn_failure,
    summarize_provider_error,
)

KEY = "sk-svcac" + "*" * 40 + "fvMA"
CODEX_401 = (
    f"unexpected status 401 Unauthorized: Incorrect API key provided: {KEY}. You can find your API "
    "key at https://platform.openai.com/account/api-keys., url: https://chatgpt.com/backend-api/codex/"
    "responses, cf-ray: a40da28f7896ec3e-VNO, request id: 831b9421-ddf3-4abf-bf14-f013e47cd11e"
)


def started(at: str = "2026-09-25T22:58:11.391Z") -> dict:
    return {"timestamp": at, "type": "event_msg", "payload": {"type": "task_started"}}


def completed(
    error: str | None = None, *, at: str = "2026-09-25T22:58:41.771Z", message: str | None = None
) -> dict:
    payload: dict = {"type": "task_complete", "last_agent_message": message}
    if error is not None:
        payload["error"] = {"message": error, "codex_error_info": "other"}
    return {"timestamp": at, "type": "event_msg", "payload": payload}


class ClassifierTests(unittest.TestCase):
    def test_the_2026_09_25_error_is_an_auth_failure_named_without_its_key(self) -> None:
        found = classify_provider_error(CODEX_401)

        assert found is not None
        self.assertEqual((found.kind, found.status), (KIND_AUTH, 401))
        self.assertIn("401 Unauthorized: Incorrect API key provided", found.summary)
        for secret in ("sk-svcac", "fvMA", "cf-ray", "831b9421", "a40da28f"):
            self.assertNotIn(secret, found.summary)

    def test_each_provider_class_is_named(self) -> None:
        for text, kind, status in (
            ("unexpected status 403 Forbidden", KIND_AUTH, 403),
            ("API Error: 429 rate limit reached", KIND_RATE_LIMIT, 429),
            ("unexpected status 502 Bad Gateway", KIND_SERVER, 502),
            ("API Error: 529 Overloaded. This is a server-side issue", KIND_SERVER, 529),
            ("exceeded retry limit, last status: 503 Service Unavailable", KIND_SERVER, 503),
            ("Reconnecting... 5/5 (stream disconnected before completion)", KIND_RECONNECT, None),
            ("stream disconnected before completion: error sending request", KIND_RECONNECT, None),
        ):
            with self.subTest(text=text):
                found = classify_provider_error(text)
                assert found is not None
                self.assertEqual((found.kind, found.status), (kind, status))

    def test_what_is_not_a_provider_error_is_not_classified(self) -> None:
        for text in (
            "Reconnecting... 2/5",
            "context window exceeded",
            "the model declined",
            "wrote 503 lines to the log",
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(classify_provider_error(text))

    def test_a_summary_is_bounded_and_scrubbed(self) -> None:
        secret = "sk-ant-oat01-" + "a" * 40
        summary = summarize_provider_error(f"API Error: 401 bearer {secret} invalid " + "x" * 500)
        self.assertNotIn(secret, summary)
        self.assertLessEqual(len(summary), 200)


class CodexFirstTurnTests(unittest.TestCase):
    def test_a_first_turn_ending_on_the_401_is_the_failure(self) -> None:
        found = codex_first_turn_failure([{"type": "session_meta"}, started(), completed(CODEX_401)])

        assert found is not None
        self.assertEqual(found.status, 401)
        self.assertEqual(found.source, "codex-rollout")
        self.assertAlmostEqual(found.at, 1790377121.771, places=2)

    def test_a_turn_still_open_answers_nothing(self) -> None:
        self.assertIsNone(codex_first_turn_failure([started()]))

    def test_a_head_that_completed_a_clean_turn_is_out_of_scope(self) -> None:
        events = [started(), completed(message="done"), started(), completed(CODEX_401)]
        self.assertIsNone(codex_first_turn_failure(events))

    def test_a_failed_first_turn_nudged_into_a_second_failure_is_still_the_case(self) -> None:
        events = [started(), completed(CODEX_401), started(), completed(CODEX_401)]
        self.assertIsNotNone(codex_first_turn_failure(events))

    def test_a_later_clean_turn_ends_it(self) -> None:
        events = [started(), completed(CODEX_401), started(), completed(message="working")]
        self.assertIsNone(codex_first_turn_failure(events))

    def test_a_non_provider_turn_error_keeps_the_old_path(self) -> None:
        self.assertIsNone(codex_first_turn_failure([started(), completed("context window exceeded")]))

    def test_the_older_separate_error_event_shape_is_read(self) -> None:
        events = [
            started(),
            {
                "type": "event_msg",
                "payload": {"type": "error", "message": "exceeded retry limit, last status: 500"},
            },
            completed(),
        ]
        found = codex_first_turn_failure(events)
        assert found is not None
        self.assertEqual((found.kind, found.status), (KIND_SERVER, 500))


def claude_error(status: int = 401, text: str = "API Error: 401 invalid token · Please run /login") -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-09-25T22:58:30.000Z",
        "isApiErrorMessage": True,
        "apiErrorStatus": status,
        "error": "authentication_failed",
        "message": {
            "role": "assistant",
            "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": text}],
        },
    }


def claude_turn_end(text: str = "done") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
        },
    }


def claude_user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


class ClaudeFirstTurnTests(unittest.TestCase):
    def test_a_first_turn_ending_on_an_api_error_is_the_failure(self) -> None:
        found = claude_first_turn_failure([claude_user(), claude_error()])
        assert found is not None
        self.assertEqual((found.kind, found.status, found.source), (KIND_AUTH, 401, "claude-session"))

    def test_the_typed_error_field_alone_is_enough(self) -> None:
        record = claude_error(text="")
        record.pop("apiErrorStatus")
        found = claude_first_turn_failure([claude_user(), record])
        assert found is not None
        self.assertEqual(found.kind, KIND_AUTH)

    def test_an_earlier_clean_turn_puts_it_out_of_scope(self) -> None:
        self.assertIsNone(
            claude_first_turn_failure([claude_user(), claude_turn_end(), claude_user(), claude_error()])
        )

    def test_a_new_prompt_after_the_error_is_a_turn_in_progress(self) -> None:
        self.assertIsNone(claude_first_turn_failure([claude_user(), claude_error(), claude_user("retry")]))

    def test_meta_records_do_not_hide_the_error(self) -> None:
        records = [claude_user(), claude_error(), {"type": "system", "subtype": "turn_duration"}]
        self.assertIsNotNone(claude_first_turn_failure(records))


CODEX_PLAN_400 = (
    "unexpected status 400 Bad Request: The 'gpt-6.1-sol' model is not supported when using Codex "
    "with a ChatGPT account."
)


class PlanRefusalTests(unittest.TestCase):
    def test_a_turn_refused_for_the_accounts_plan_is_a_plan_failure(self) -> None:
        found = codex_turn_failure([started(), completed(CODEX_PLAN_400)])
        assert found is not None
        self.assertEqual((found.kind, found.status), (KIND_PLAN, 400))
        self.assertIn("model is not supported when using Codex", found.summary)

    def test_the_refusal_at_the_bottom_of_a_codex_screen_is_read(self) -> None:
        found = screen_turn_failure(["› review the card", "", f"■ {CODEX_PLAN_400}", "", "› "])
        assert found is not None
        self.assertEqual(found.kind, KIND_PLAN)

    def test_prose_that_quotes_the_refusal_is_not_read_from_the_screen(self) -> None:
        line = "• Codex said the model is not supported when using Codex with a ChatGPT account"
        self.assertIsNone(screen_turn_failure([line, "› "]))


class ScreenTests(unittest.TestCase):
    def test_the_error_just_above_the_prompt_is_read(self) -> None:
        lines = [
            "> read TASK.md",
            "",
            "  ⎿  API Error: 401 invalid token · Please run /login",
            "",
            "╭──╮",
            "│ > │",
            "╰──╯",
        ]
        found = screen_turn_failure(lines)
        assert found is not None
        self.assertEqual((found.status, found.source), (401, "pty-screen"))

    def test_an_error_scrolled_far_above_the_prompt_is_not_the_turn_end(self) -> None:
        lines = ["API Error: 401 invalid token", *[f"line {n}" for n in range(30)]]
        self.assertIsNone(screen_turn_failure(lines))


def _codex_run(workspace: Path, root: Path, path: Path) -> HeadRun:
    run = HeadRun(
        run_id="codex-bound",
        spec=HeadSpec(profile_id="codex-terra-high", adapter="codex", resource="openai-sub"),
        workspace=str(workspace),
        task_ref=TaskRef.card("ummanu-1799"),
        role="reviewer",
    )
    parsed = _read_source(path)
    assert parsed is not None
    _meta, lines = parsed
    _, fingerprint = head_run_binding(run.to_json())
    source = {
        "version": 1,
        "kind": "codex_session_event_jsonl",
        "state": "bound",
        "run_id": run.run_id,
        "head_run_fingerprint": fingerprint,
        "workspace": str(workspace.resolve()),
        "role": "reviewer",
        "task_ref": run.task_ref.to_json(),
        "root": str(root.resolve()),
        "path": str(path.resolve()),
        "session_id": "own",
        "parent_thread_id": "own",
        "initial_range": {
            "first": {"line": 1, "digest": lines[0].digest},
            "root": {"line": 1, "digest": lines[0].digest},
            "last": {"line": 1, "digest": lines[0].digest},
            "digest": _range_digest(lines[:1]),
        },
        "cursor": {"line": 1, "digest": lines[0].digest},
        "bound_at": "2026-09-25T22:58:05Z",
    }
    return run.with_fanout_policy({**run.fanout_policy, "provider_source": source})


class RunBoundReaderTests(unittest.TestCase):
    def test_a_bound_codex_rollout_that_ended_on_the_401_is_read_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = Path(tmp) / "sessions"
            root.mkdir()
            rollout = root / "rollout.jsonl"
            meta = {"type": "session_meta", "payload": {"session_id": "own", "cwd": str(workspace)}}
            rollout.write_text(json.dumps(meta) + "\n", encoding="utf-8")
            run = _codex_run(workspace, root, rollout)

            self.assertEqual(provider_failure_for_run(run)["state"], "none")

            with rollout.open("a", encoding="utf-8") as handle:
                for event in (started(), completed(CODEX_401)):
                    handle.write(json.dumps(event) + "\n")
            reading = provider_failure_for_persisted_run(run.to_json())

        self.assertEqual(reading["state"], "failed")
        self.assertEqual(reading["run_id"], "codex-bound")
        self.assertEqual(reading["resource"], "openai-sub")
        self.assertEqual(reading["error"]["status"], 401)
        self.assertNotIn("sk-svcac", json.dumps(reading))

    def test_an_unbound_codex_source_is_no_answer(self) -> None:
        run = HeadRun(
            run_id="codex-unbound",
            spec=HeadSpec(profile_id="codex", adapter="codex"),
            workspace="/nonexistent",
            task_ref=TaskRef.card("ummanu-1799"),
            role="worker",
        )
        self.assertEqual(provider_failure_for_run(run)["state"], "unavailable")

    def test_a_bound_claude_transcript_that_ended_on_an_api_error_is_read_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = Path(tmp) / "claude-projects"
            with mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(root)}):
                run = prepare_claude_provider_progress_source(
                    HeadRun(
                        run_id="claude-bound",
                        spec=HeadSpec(profile_id="claude-opus-high", adapter="claude", resource="claude-sub"),
                        workspace=str(workspace),
                        task_ref=TaskRef.card("ummanu-1799"),
                        role="worker",
                    )
                )
                transcript = root / claude_project_dir_name(str(workspace)) / "session.jsonl"
                transcript.parent.mkdir(parents=True)
                records = [{**claude_user(), "sessionId": "claude-session"}, claude_error()]
                transcript.write_text(
                    "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
                    encoding="utf-8",
                )
                run = bind_claude_provider_progress_source(run)
                reading = provider_failure_for_run(run)

        self.assertEqual(reading["state"], "failed")
        self.assertEqual(reading["resource"], "claude-sub")
        self.assertEqual(reading["error"]["kind"], KIND_AUTH)

    def test_a_claude_head_with_no_transcript_and_no_pty_is_no_answer(self) -> None:
        run = HeadRun(
            run_id="claude-unbound",
            spec=HeadSpec(profile_id="claude", adapter="claude"),
            workspace="/nonexistent",
            task_ref=TaskRef.card("ummanu-1799"),
            role="worker",
        )
        self.assertEqual(provider_failure_for_run(run)["state"], "unavailable")

    def test_the_ready_return_token_is_read_back_off_its_request_id(self) -> None:
        self.assertTrue(
            is_provider_unavailable_return("dispatcher-a-provider-unavailable-ready-ummanu-1-run")
        )
        self.assertFalse(is_provider_unavailable_return("dispatcher-a-worker-respawn-ummanu-1"))


if __name__ == "__main__":
    unittest.main()
