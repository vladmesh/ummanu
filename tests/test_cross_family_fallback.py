"""ummanu-108: a role's default profile is only the first one to try.

Unit-level pieces of the provider fallback: the provider's own words for a spent subscription and
its reset time, a turn refused mid-run, a resource held red to that reset whatever a cheap probe
says, the walk going back to the primary once it expires, and the configuration rule that every
profile can fall over to the other family. The dispatcher's handling of the same events is in
`tests/test_dispatcher_provider_failure.py`; the PO service's in `tests/test_po_service.py`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from unittest import mock

from ummanu.config import fallback_errors
from ummanu.dispatch.observer import ObserverRecord
from ummanu.head_health import (
    QUOTA_BACKOFF_SECONDS,
    HeadHealth,
    failure_status,
    failure_until,
    resolve_head_chain,
)
from ummanu.runtime.heads import cross_family_gaps
from ummanu.runtime.provider_errors import (
    KIND_AUTH,
    KIND_QUOTA,
    KIND_RATE_LIMIT,
    classify_provider_error,
    claude_turn_failure,
    codex_first_turn_failure,
    codex_turn_failure,
    reset_time,
    screen_turn_failure,
)
from ummanu.webproto.reads import health_summary

# The 2026-10-06 reviewer's rollout tail, as Codex wrote it (issue:ee68af70cc409f458615).
CODEX_USAGE_LIMIT = (
    "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
    "credits or try again at Oct 9th, 2026 9:11 PM."
)
NOW = datetime(2026, 10, 6, 16, 0).timestamp()


def started(at: str) -> dict:
    return {"timestamp": at, "type": "event_msg", "payload": {"type": "task_started"}}


def completed(at: str, *, message: str | None = None, error: dict | None = None) -> dict:
    payload: dict = {"type": "task_complete", "last_agent_message": message}
    if error is not None:
        payload["error"] = error
    return {"timestamp": at, "type": "event_msg", "payload": payload}


INCIDENT_ROLLOUT = [
    started("2026-10-06T14:53:48.000Z"),
    completed("2026-10-06T14:54:00.000Z", message="reading the diff"),
    started("2026-10-06T14:54:01.000Z"),
    completed(
        "2026-10-06T14:54:22.818Z",
        error={"message": CODEX_USAGE_LIMIT, "codex_error_info": "usage_limit_exceeded"},
    ),
]


class ClassifierTests(unittest.TestCase):
    def test_codex_usage_limit_is_a_spent_quota_with_its_named_reset(self) -> None:
        found = classify_provider_error(CODEX_USAGE_LIMIT)

        assert found is not None
        self.assertEqual(found.kind, KIND_QUOTA)
        self.assertEqual(found.reset_at, datetime(2026, 10, 9, 21, 11).timestamp())
        self.assertIn("hit your usage limit", found.summary)

    def test_claude_weekly_limit_is_a_spent_quota_not_a_rate_limit(self) -> None:
        found = classify_provider_error("You've hit your weekly limit · resets 1am (UTC)", status=429)

        assert found is not None
        self.assertEqual((found.kind, found.status), (KIND_QUOTA, 429))
        self.assertGreater(found.reset_at, 0)

    def test_a_bare_429_stays_a_rate_limit_and_a_401_stays_auth(self) -> None:
        self.assertEqual(classify_provider_error("API Error: 429 rate limit reached").kind, KIND_RATE_LIMIT)
        self.assertEqual(classify_provider_error("unexpected status 401 Unauthorized").kind, KIND_AUTH)

    def test_reset_times_in_each_providers_words(self) -> None:
        cases = {
            "try again at Oct 9th, 2026 9:11 PM.": datetime(2026, 10, 9, 21, 11).timestamp(),
            "Try again in 3 days.": NOW + 3 * 86400,
            "Try again in 2 hours 30 minutes.": NOW + 2.5 * 3600,
            "Claude AI usage limit reached|1791700000": 1791700000.0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reset_time(text, now=NOW), expected)
        self.assertEqual(reset_time("no time named here", now=NOW), 0.0)
        # A zone Claude names is honoured, and a time already past today is tomorrow's.
        utc_now = datetime(2026, 10, 6, 16, 0, tzinfo=UTC).timestamp()
        self.assertEqual(
            reset_time("resets 3:30pm (UTC)", now=utc_now), datetime(2026, 10, 7, 15, 30, tzinfo=UTC).timestamp()
        )
        self.assertEqual(
            reset_time("resets Oct 7, 1am (UTC)", now=utc_now), datetime(2026, 10, 7, 1, 0, tzinfo=UTC).timestamp()
        )

    def test_the_typed_codex_error_info_alone_names_the_quota(self) -> None:
        rollout = [
            started("2026-10-06T15:00:00Z"),
            completed("2026-10-06T15:00:02Z", error={"message": "", "codex_error_info": "usage_limit_exceeded"}),
        ]
        found = codex_turn_failure(rollout)
        assert found is not None
        self.assertEqual(found.kind, KIND_QUOTA)


class MidTurnTests(unittest.TestCase):
    def test_the_incident_rollout_is_a_failure_though_the_first_turn_was_clean(self) -> None:
        self.assertIsNone(codex_first_turn_failure(INCIDENT_ROLLOUT))

        found = codex_turn_failure(INCIDENT_ROLLOUT)

        assert found is not None
        self.assertEqual(found.kind, KIND_QUOTA)
        self.assertEqual(found.source, "codex-rollout")
        self.assertEqual(found.reset_at, datetime(2026, 10, 9, 21, 11).timestamp())

    def test_an_open_turn_after_the_failure_answers_nothing(self) -> None:
        self.assertIsNone(codex_turn_failure([*INCIDENT_ROLLOUT, started("2026-10-06T15:00:00Z")]))

    def test_a_later_clean_turn_ends_it(self) -> None:
        rollout = [*INCIDENT_ROLLOUT, started("2026-10-06T15:00:00Z"), completed("2026-10-06T15:01:00Z", message="ok")]
        self.assertIsNone(codex_turn_failure(rollout))

    def test_a_claude_session_refused_after_a_clean_turn(self) -> None:
        records = [
            {"type": "user", "message": {"content": "go"}},
            {"type": "assistant", "message": {"stop_reason": "end_turn", "content": "done"}},
            {"type": "user", "message": {"content": "next"}},
            {
                "type": "assistant",
                "timestamp": "2026-10-06T15:00:00Z",
                "isApiErrorMessage": True,
                "apiErrorStatus": 429,
                "error": "rate_limit",
                "message": {"content": [{"type": "text", "text": "You've hit your weekly limit · resets 1am (UTC)"}]},
            },
        ]
        found = claude_turn_failure(records)
        assert found is not None
        self.assertEqual(found.kind, KIND_QUOTA)
        self.assertIsNone(claude_turn_failure([*records, {"type": "user", "message": {"content": "again"}}]))

    def test_a_codex_screen_shows_its_usage_limit(self) -> None:
        lines = ["", "■ " + CODEX_USAGE_LIMIT, "", "› Implement {feature}", ""]
        found = screen_turn_failure(lines)
        assert found is not None
        self.assertEqual(found.kind, KIND_QUOTA)


class Catalog:
    profiles: ClassVar[dict[str, dict]] = {
        "codex-terra-high": {"resource": "openai-sub", "fallback": ["claude-opus-high"]},
        "claude-opus-high": {"resource": "claude-sub", "fallback": ["codex-terra-high"]},
    }

    def head_profile(self, head: str) -> dict:
        return self.profiles[head]

    def resource(self, resource: str) -> dict:
        return {"probe": f"probe {resource}"}


class ExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.health = HeadHealth(Catalog(), self.data)
        self.pong = subprocess.CompletedProcess("probe", 0, "pong", "")

    def walk(self, now: float):
        with mock.patch("time.time", return_value=now):
            return resolve_head_chain(
                "codex-terra-high", self.health.check, lambda head: Catalog.profiles[head]["fallback"]
            )

    def test_a_red_resource_is_held_to_its_reset_and_a_pong_does_not_clear_it(self) -> None:
        until = NOW + 3 * 86400
        self.health.record("openai-sub", "exhausted", "spent", now=NOW, until=until)
        with mock.patch("ummanu.head_health._proc.run_isolated", return_value=self.pong) as probe:
            # Well past the probe TTL, a day before the reset.
            choice = self.walk(until - 86400)

        self.assertEqual(choice.head, "claude-opus-high")
        self.assertTrue(choice.substituted)
        rejected = dict(choice.rejected)["codex-terra-high"]
        self.assertEqual((rejected.status, rejected.until), ("exhausted", until))
        # Only the fallback's resource was probed; the red one was not asked.
        self.assertEqual(len(probe.call_args_list), 1)
        self.assertIn("claude-sub", probe.call_args.args[0][-1])

    def test_once_it_expires_a_fresh_probe_decides_and_the_primary_is_used_again(self) -> None:
        until = NOW + 3600
        self.health.record("openai-sub", "exhausted", "spent", now=NOW, until=until)
        with mock.patch("ummanu.head_health._proc.run_isolated", return_value=self.pong):
            choice = self.walk(until + 1)

        self.assertEqual(choice.head, "codex-terra-high")
        self.assertFalse(choice.substituted)

    def test_no_named_reset_is_a_bounded_backoff(self) -> None:
        self.assertEqual(failure_until(KIND_QUOTA, 0.0, NOW), NOW + QUOTA_BACKOFF_SECONDS)
        self.assertEqual(failure_until(KIND_QUOTA, NOW + 10, NOW), NOW + 10)
        self.assertLess(failure_until(KIND_AUTH, 0.0, NOW), NOW + QUOTA_BACKOFF_SECONDS)
        self.assertEqual((failure_status(KIND_QUOTA), failure_status(KIND_AUTH)), ("exhausted", "unavailable"))

    def test_red_resources_lists_only_unexpired_verdicts(self) -> None:
        self.health.record("openai-sub", "exhausted", "spent", now=NOW, until=NOW + 60)
        self.health.record("claude-sub", "ready", "probe succeeded", now=NOW)
        self.assertEqual(list(self.health.red_resources(now=NOW)), ["openai-sub"])
        self.assertEqual(self.health.red_resources(now=NOW + 61), {})
        stored = json.loads((self.data / "dispatcher" / "resource_health.json").read_text())
        self.assertEqual(stored["openai-sub"]["until"], NOW + 60)


class CrossFamilyConfigTests(unittest.TestCase):
    PROFILES: ClassVar[dict[str, dict]] = {
        "codex-terra-high": {"adapter": "codex", "resource": "openai-sub", "effort": "high",
                             "fallback": ["claude-opus-high"]},
        "claude-opus-high": {"adapter": "claude", "resource": "claude-sub", "effort": "high",
                             "fallback": ["codex-terra-high"]},
        "hermes": {"adapter": "hermes", "resource": "openrouter", "fallback": []},
    }

    def test_a_complete_registry_has_no_gap(self) -> None:
        self.assertEqual(cross_family_gaps(self.PROFILES), [])

    def test_a_profile_with_no_cross_family_fallback_is_named(self) -> None:
        profiles = {**self.PROFILES, "claude-opus-medium": {
            "adapter": "claude", "resource": "claude-sub", "effort": "medium", "fallback": ["claude-opus-high"]
        }}
        gaps = cross_family_gaps(profiles)
        # Its chain does reach codex through claude-opus-high, but only at another tier.
        self.assertEqual(len(gaps), 1)
        self.assertIn("'claude-opus-medium'", gaps[0])
        profiles["claude-opus-medium"]["fallback"] = []
        self.assertIn("has no cross-family fallback", cross_family_gaps(profiles)[0])

    def test_ummanu_config_check_fails_naming_the_profile(self) -> None:
        from ummanu.infra.config_check import check_live_root

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "heads").mkdir()
        (root / "heads" / "heads.toml").write_text(
            '[resources.openai-sub]\nprobe = "x"\n[resources.claude-sub]\nprobe = "x"\n'
            '[profiles.codex-terra-high]\nresource = "openai-sub"\nadapter = "codex"\nmodel = "m"\n'
            'effort = "high"\nruntime = "local-pty"\nfallback = []\n'
            '[role_defaults]\nreviewer = "codex-terra-high"\n',
            encoding="utf-8",
        )
        (root / "instance.yaml").write_text("name: t\n", encoding="utf-8")
        with mock.patch("ummanu.infra.config_check.exported_files", return_value=[]):
            result = check_live_root(root)
        fallback = [finding for finding in result.findings if finding.startswith("fallback:")]
        self.assertEqual(len(fallback), 1, result.findings)
        self.assertIn("'codex-terra-high'", fallback[0])
        self.assertFalse(result.ok)

    def test_po_models_on_one_cli_only_have_nothing_to_fall_over_to(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        errors = fallback_errors(root, {"po": {"models": {"claude": ["fable"], "codex": []}}})
        self.assertEqual([error.path for error in errors], ["po.models"])
        self.assertEqual(fallback_errors(root, {"po": {"models": {"claude": ["fable"], "codex": ["x"]}}}), [])
        self.assertEqual(fallback_errors(root, {}), [])


class SurfaceTests(unittest.TestCase):
    def test_the_web_health_names_a_red_provider_and_when_it_comes_back(self) -> None:
        status = {"recovery": {"resources": [
            {"resource": "openai-sub", "state": "exhausted", "until": "2026-10-09T21:11:00Z",
             "reason": "resource quota is spent"},
            {"resource": "claude-sub", "state": "ready", "until": None, "reason": "probe succeeded"},
        ]}}
        summary = health_summary(status)

        self.assertEqual(
            summary["providers"][0],
            {"resource": "openai-sub", "state": "exhausted", "until": "2026-10-09T21:11:00Z",
             "reason": "resource quota is spent"},
        )
        self.assertIn("provider openai-sub is exhausted until 2026-10-09T21:11:00Z", summary["problems"][0])
        self.assertEqual(summary["colour"], "yellow")

    def test_an_observer_fallback_head_survives_its_record(self) -> None:
        record = ObserverRecord(sprint="sprint:1", head="claude-opus-high", fallback_head="codex-terra-high")
        self.assertEqual(ObserverRecord.from_json(record.to_json()).fallback_head, "codex-terra-high")
        self.assertNotIn("fallback_head", ObserverRecord(sprint="sprint:1").to_json())


if __name__ == "__main__":
    unittest.main()
