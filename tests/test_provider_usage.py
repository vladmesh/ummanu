from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from ummanu.web import provider_usage
from ummanu.web.provider_usage import CLAUDE_USAGE_URL, CODEX_USAGE_URL, ProviderUsageLayer

NOW = 1_800_000_000.0


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_live_usage_is_compact_and_credentials_never_leave_headers(tmp_path: Path) -> None:
    write(tmp_path / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "claude-secret"}})
    write(tmp_path / ".codex/auth.json", {"tokens": {"access_token": "codex-secret", "account_id": "acct"}})
    calls = []

    def fetch(url, headers, timeout):
        calls.append((url, headers, timeout))
        if url == CLAUDE_USAGE_URL:
            return {
                "five_hour": {"utilization": 26.0, "resets_at": NOW + 100},
                "seven_day": {"utilization": 72, "resets_at": NOW + 200},
            }
        assert url == CODEX_USAGE_URL
        return {
            "rate_limit": {
                "primary_window": {"used_percent": 41, "window_minutes": 300, "reset_at": NOW + 300},
                "secondary_window": {"used_percent": 9, "window_minutes": 10080, "reset_at": NOW + 400},
            }
        }

    layer = ProviderUsageLayer(home=tmp_path, fetch_json=fetch, now=lambda: NOW)
    document = layer.usage_snapshot()
    assert document["providers"][0]["windows"][0]["remaining_percent"] == 74.0
    assert document["providers"][1]["windows"][1]["remaining_percent"] == 91.0
    assert "secret" not in json.dumps(document)
    assert all(timeout == 3.0 for _, _, timeout in calls)
    assert layer.usage_snapshot() is document
    assert len(calls) == 2


def test_old_codex_session_is_truthfully_stale_when_live_read_fails(tmp_path: Path) -> None:
    write(tmp_path / ".codex/auth.json", {"tokens": {"access_token": "secret"}})
    session = tmp_path / ".codex/sessions/2026/01/01/rollout-test.jsonl"
    write(
        session,
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "info": {
                    "rate_limits": {
                        "primary": {"used_percent": 80, "window_minutes": 10080, "resets_at": NOW + 10}
                    }
                }
            },
        },
    )

    def fail(*_args):
        raise TimeoutError

    document = ProviderUsageLayer(home=tmp_path, fetch_json=fail, now=lambda: NOW).usage_snapshot()
    codex = document["providers"][1]
    assert codex["status"] == "stale"
    assert codex["windows"][0]["remaining_percent"] == 20.0
    assert codex["reason"] == "Latest Codex usage observation is stale"


class ClaudeRequestHeadersTest(unittest.TestCase):
    def test_usage_sends_cli_compatibility_headers(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tmp_path = Path(directory.name)
        write(tmp_path / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "test-secret"}})
        requests = []

        def open_usage(request, *, timeout):
            requests.append(request)
            if not request.get_header("User-agent", "").startswith("claude-code/"):
                raise HTTPError(request.full_url, 429, "unsupported client", Message(), None)
            assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"
            assert request.get_header("Authorization") == "Bearer test-secret"
            return io.BytesIO(json.dumps({"seven_day": {"utilization": 37}}).encode())

        with mock.patch.object(provider_usage, "urlopen", side_effect=open_usage):
            document = ProviderUsageLayer(home=tmp_path, environ={}, now=lambda: NOW).usage_snapshot()

        claude = document["providers"][0]
        assert claude["status"] == "available"
        assert claude["windows"][0]["remaining_percent"] == 63
        assert len(requests) == 1
        assert requests[0].full_url == CLAUDE_USAGE_URL


def test_missing_or_malformed_auth_is_explicitly_unavailable(tmp_path: Path) -> None:
    write(tmp_path / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": 3}})
    document = ProviderUsageLayer(home=tmp_path, now=lambda: NOW, environ={}).usage_snapshot()
    assert [(item["id"], item["status"], item["windows"]) for item in document["providers"]] == [
        ("claude", "unavailable", []),
        ("codex", "unavailable", []),
    ]


# The Codex fallback's cost is bounded by constants, not by the size of ~/.codex/sessions.

NEWEST_DAY = date(2026, 9, 15)


def rate_limit_event(used: float, when: datetime) -> dict[str, object]:
    return {
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "payload": {"info": {"rate_limits": {"primary": {"used_percent": used, "window_minutes": 300}}}},
    }


def build_tree(
    home: Path,
    *,
    days: int,
    files_per_day: int,
    padding: int,
    event: Callable[[int, int], dict[str, object] | None],
) -> None:
    """Write sessions/YYYY/MM/DD/rollout-*.jsonl for `days` days ending on NEWEST_DAY.

    Each file is `padding` sparse bytes followed by a few complete lines; `event(day, index)` puts a
    rate-limit line last when it returns one.  Newer days and later file names get later mtimes.
    """
    for day_index in range(days):
        day = NEWEST_DAY - timedelta(days=day_index)
        directory = home / ".codex/sessions" / f"{day:%Y/%m/%d}"
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(files_per_day):
            started = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=30 * (1 + index))
            path = directory / f"rollout-{started:%Y-%m-%dT%H-%M-%S}-{index:04d}.jsonl"
            with path.open("wb") as handle:
                handle.truncate(padding)
                handle.seek(padding)
                handle.write(b'\n{"payload": {"type": "turn"}}\n')
                line = event(day_index, index)
                if line is not None:
                    handle.write(json.dumps(line).encode() + b"\n")
                handle.write(b'{"payload": {"type": "task_complete"}}\n')
            os.utime(path, (started.timestamp(), started.timestamp()))


def legacy_latest_codex_event(home: Path) -> tuple[dict[str, object], float] | None:
    """Today's unbounded walk before secretary-1662, kept here as the reference answer."""
    root = home / ".codex" / "sessions"
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[:20]:
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                event = json.loads(line)
                limits = event["payload"]["info"]["rate_limits"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if isinstance(limits, dict):
                return limits, datetime.fromisoformat(event["timestamp"]).timestamp()
    return None


class CountingFile:
    def __init__(self, handle, counts: dict[str, int]) -> None:
        self.handle = handle
        self.counts = counts

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.handle.close()

    def seek(self, *args):
        return self.handle.seek(*args)

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.counts["bytes"] += len(data)
        return data


class BoundedCodexFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.homes = tempfile.TemporaryDirectory()
        self.addCleanup(self.homes.cleanup)

    def home(self, name: str) -> Path:
        return Path(self.homes.name) / name

    def measure(self, home: Path) -> tuple[tuple[dict[str, object], float] | None, dict[str, int]]:
        counts = {"listings": 0, "opens": 0, "bytes": 0}
        real_scandir = os.scandir

        def scandir(path):
            counts["listings"] += 1
            return real_scandir(path)

        def counting_open(path, mode="r", *args, **kwargs):
            counts["opens"] += 1
            return CountingFile(open(path, mode, *args, **kwargs), counts)

        layer = ProviderUsageLayer(home=home, now=lambda: NOW)
        with (
            mock.patch("os.scandir", scandir),
            mock.patch.object(provider_usage, "open", counting_open, create=True),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")),
        ):
            found = layer._latest_codex_event()
        return found, counts

    def assert_within_constants(self, counts: dict[str, int]) -> None:
        self.assertLessEqual(counts["listings"], provider_usage.CODEX_FALLBACK_LISTINGS)
        self.assertLessEqual(counts["opens"], provider_usage.CODEX_FALLBACK_FILES)
        self.assertLessEqual(
            counts["bytes"], provider_usage.CODEX_FALLBACK_FILES * provider_usage.CODEX_TAIL_BYTES
        )

    def test_cost_is_fixed_when_the_tree_grows_tenfold_with_much_larger_files(self) -> None:
        tail = provider_usage.CODEX_TAIL_BYTES
        nothing = lambda _day, _index: None
        small, large = self.home("small"), self.home("large")
        build_tree(small, days=10, files_per_day=4, padding=2 * tail, event=nothing)
        build_tree(large, days=100, files_per_day=4, padding=400 * tail, event=nothing)
        self.assertEqual(
            len(list(large.rglob("rollout-*.jsonl"))), 10 * len(list(small.rglob("rollout-*.jsonl")))
        )

        small_found, small_counts = self.measure(small)
        large_found, large_counts = self.measure(large)

        self.assertIsNone(small_found)
        self.assertIsNone(large_found)
        # The worst case — no event anywhere — opens the full file budget and reads a full tail of each.
        self.assertEqual(small_counts["opens"], provider_usage.CODEX_FALLBACK_FILES)
        self.assertEqual(small_counts["bytes"], provider_usage.CODEX_FALLBACK_FILES * tail)
        self.assert_within_constants(small_counts)
        self.assert_within_constants(large_counts)
        self.assertEqual(large_counts, small_counts)

    def test_listing_count_does_not_change_with_ten_times_more_days(self) -> None:
        nothing = lambda _day, _index: None
        small, large = self.home("small"), self.home("large")
        build_tree(small, days=30, files_per_day=1, padding=0, event=nothing)
        build_tree(large, days=300, files_per_day=1, padding=0, event=nothing)
        _found, small_counts = self.measure(small)
        _found, large_counts = self.measure(large)
        self.assertEqual(large_counts, small_counts)
        self.assert_within_constants(large_counts)

    def test_the_newest_event_within_bounds_is_the_legacy_answer(self) -> None:
        home = self.home("tree")

        def event(day: int, index: int) -> dict[str, object] | None:
            if day == 0 or (day == 1 and index == 3):
                return None  # the newest files carry no rate-limit line
            midnight = datetime.combine(NEWEST_DAY - timedelta(days=day), datetime.min.time(), UTC)
            return rate_limit_event(10 * day + index, midnight + timedelta(minutes=30 * (1 + index) + 5))

        build_tree(home, days=20, files_per_day=4, padding=3 * provider_usage.CODEX_TAIL_BYTES, event=event)
        found, counts = self.measure(home)
        self.assertEqual(found, legacy_latest_codex_event(home))
        assert found is not None
        self.assertEqual(found[0]["primary"]["used_percent"], 12)
        self.assertEqual(counts["opens"], 6)
        self.assert_within_constants(counts)

    def test_a_long_line_ending_the_tail_is_parsed_and_a_cut_line_is_skipped(self) -> None:
        home = self.home("tree")
        build_tree(home, days=1, files_per_day=1, padding=0, event=lambda _d, _i: None)
        path = next(home.rglob("rollout-*.jsonl"))
        cut = rate_limit_event(99, datetime(2026, 9, 15, tzinfo=UTC))
        cut["pad"] = "x" * provider_usage.CODEX_TAIL_BYTES
        whole = rate_limit_event(55, datetime(2026, 9, 15, 2, tzinfo=UTC))
        whole["pad"] = "y" * (provider_usage.CODEX_TAIL_BYTES // 2)
        path.write_text(json.dumps(cut) + "\n" + json.dumps(whole) + "\n", encoding="utf-8")
        found, _counts = self.measure(home)
        assert found is not None
        self.assertEqual(found[0]["primary"]["used_percent"], 55)
        path.write_text(json.dumps(cut) + "\n", encoding="utf-8")
        self.assertIsNone(self.measure(home)[0])

    def test_an_event_beyond_the_tail_is_reported_unavailable_not_read_for(self) -> None:
        home = self.home("tree")
        build_tree(home, days=1, files_per_day=1, padding=0, event=lambda _d, _i: None)
        path = next(home.rglob("rollout-*.jsonl"))
        buried = json.dumps(rate_limit_event(50, datetime(2026, 9, 15, tzinfo=UTC)))
        filler = "\n".join(['{"payload": {"type": "turn"}}'] * (provider_usage.CODEX_TAIL_BYTES // 16))
        path.write_text(buried + "\n" + filler + "\n", encoding="utf-8")
        self.assertIsNotNone(legacy_latest_codex_event(home))  # today's code reads the whole file for it
        found, counts = self.measure(home)
        self.assertIsNone(found)
        self.assertEqual(counts["bytes"], provider_usage.CODEX_TAIL_BYTES)

        def fail(*_args):
            raise TimeoutError

        write(home / ".codex/auth.json", {"tokens": {"access_token": "secret"}})
        document = ProviderUsageLayer(home=home, fetch_json=fail, now=lambda: NOW).usage_snapshot()
        codex = document["providers"][1]
        self.assertEqual(codex["status"], "unavailable")
        self.assertEqual(codex["reason"], "Codex usage is temporarily unavailable")

    def test_empty_day_directories_exhaust_the_listing_budget_and_stop(self) -> None:
        home = self.home("tree")
        build_tree(
            home,
            days=1,
            files_per_day=1,
            padding=0,
            event=lambda _d, _i: rate_limit_event(50, datetime(2025, 1, 1, tzinfo=UTC)),
        )
        # Move that one rollout into an old day behind a long run of newer, empty days.
        rollout = next(home.rglob("rollout-*.jsonl"))
        old_day = home / ".codex/sessions/2025/01/01"
        old_day.mkdir(parents=True)
        rollout.rename(old_day / rollout.name)
        for offset in range(provider_usage.CODEX_FALLBACK_LISTINGS * 2):
            day = NEWEST_DAY - timedelta(days=offset)
            (home / ".codex/sessions" / f"{day:%Y/%m/%d}").mkdir(parents=True, exist_ok=True)
        found, counts = self.measure(home)
        self.assertIsNone(found)
        self.assertEqual(counts["listings"], provider_usage.CODEX_FALLBACK_LISTINGS)
        self.assertEqual(counts["opens"], 0)


# The rollout shape Codex writes today: `payload.rate_limits` beside `payload.info`.  The structure is
# copied from a real token_count event; the values are made up.


def current_rollout_event(when: str, *, resets_at: float) -> dict[str, object]:
    return {
        "timestamp": when,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}},
            "rate_limits": {
                "limit_id": "codex",
                "limit_name": None,
                "primary": {"used_percent": 97.0, "window_minutes": 10080, "resets_at": resets_at},
                "secondary": None,
                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                "individual_limit": None,
                "spend_control_reached": None,
                "plan_type": "plus",
                "rate_limit_reached_type": None,
            },
        },
    }


class CurrentRolloutShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        write(self.home / ".codex/auth.json", {"tokens": {"access_token": "secret", "account_id": "acct"}})

    def write_rollout(self, name: str, lines: list[dict[str, object]]) -> None:
        path = self.home / ".codex/sessions/2026/09/26" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    def usage_when_the_live_source_fails(self) -> dict[str, object]:
        def fail(*_args):
            raise TimeoutError

        return ProviderUsageLayer(home=self.home, fetch_json=fail, now=lambda: NOW).codex_live()

    def test_the_current_shape_gives_a_reading_when_the_live_source_fails(self) -> None:
        observed = datetime.fromtimestamp(NOW - 60, UTC).isoformat().replace("+00:00", "Z")
        self.write_rollout(
            "rollout-2026-09-26T12-34-31-a.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "a"}},
                current_rollout_event(observed, resets_at=NOW + 600),
            ],
        )
        codex = self.usage_when_the_live_source_fails()
        self.assertEqual(codex["status"], "available")
        self.assertEqual(
            codex["windows"],
            [
                {
                    "name": "weekly",
                    "window_minutes": 10080,
                    "remaining_percent": 3.0,
                    "resets_at": provider_usage._iso(NOW + 600),
                }
            ],
            "primary set and secondary null is one window, named by its length",
        )
        self.assertEqual(codex["age_seconds"], 60.0)

    def test_an_old_current_shape_event_is_stale_and_keeps_its_raw_reset(self) -> None:
        self.write_rollout(
            "rollout-2026-01-01T00-00-00-a.jsonl",
            [current_rollout_event("2026-01-01T00:00:00Z", resets_at=NOW - 3600)],
        )
        codex = self.usage_when_the_live_source_fails()
        self.assertEqual(codex["status"], "stale")
        self.assertEqual([window["name"] for window in codex["windows"]], ["weekly"])
        self.assertEqual(codex["windows"][0]["resets_at"], provider_usage._iso(NOW - 3600))

    def test_one_helper_reads_both_shapes_and_nothing_else(self) -> None:
        limits = {"primary": {"used_percent": 1}}
        read = ProviderUsageLayer._event_rate_limits
        self.assertIs(read({"payload": {"rate_limits": limits, "info": None}}), limits)
        self.assertIs(read({"payload": {"info": {"rate_limits": limits}}}), limits)
        self.assertIs(read({"payload": {"rate_limits": limits, "info": {"rate_limits": {}}}}), limits)
        for event in (
            None,
            [],
            {"payload": None},
            {"payload": {"rate_limits": None, "info": None}},
            {"payload": {"info": {"rate_limits": []}}},
            {"payload": {"type": "turn"}},
        ):
            with self.subTest(event=event):
                self.assertIsNone(read(event))


class ClaudeUtilizationTest(unittest.TestCase):
    def test_utilization_is_a_percentage_and_never_a_fraction(self) -> None:
        for utilization, left in ((1.0, 99.0), (0.5, 99.5), (4.0, 96.0), (26, 74.0), (100.0, 0.0)):
            with self.subTest(utilization=utilization):
                window = provider_usage._window("5-hour", {"utilization": utilization}, default_minutes=300)
                assert window is not None
                self.assertEqual(window["remaining_percent"], left)

    def test_used_percent_is_read_as_before(self) -> None:
        for key in ("used_percent", "used_percentage"):
            with self.subTest(key=key):
                window = provider_usage._window("weekly", {key: 41, "utilization": 3.0})
                assert window is not None
                self.assertEqual(window["remaining_percent"], 59.0)


CREDITS_EXPIRE_AT = "2026-10-22T20:24:55.697042Z"


def codex_usage(credits: object = None, *, carry: bool = True) -> dict[str, object]:
    raw: dict[str, object] = {
        "rate_limit": {"primary_window": {"used_percent": 41, "window_minutes": 300, "reset_at": NOW + 300}}
    }
    if carry:
        raw["rate_limit_reset_credits"] = credits
    return raw


def credits_listing(*credits: dict[str, object]) -> dict[str, object]:
    return {"credits": list(credits), "available_count": len(credits), "total_earned_count": 0}


def credit(expires_at: object, status: object = "available") -> dict[str, object]:
    return {
        "id": "c",
        "reset_type": "codex_rate_limits",
        "status": status,
        "granted_at": "2026-09-22T20:24:55.697042Z",
        "expires_at": expires_at,
        "redeemed_at": None,
    }


class ResetCreditsTest(unittest.TestCase):
    """The Codex reset credits: two counts from the usage response and the nearest expiry from the list."""

    def setUp(self) -> None:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        write(self.home / ".codex/auth.json", {"tokens": {"access_token": "secret", "account_id": "acct"}})
        self.fetched: list[tuple[str, dict[str, str], float]] = []

    def codex(self, usage: object, listing: object = None) -> dict[str, object]:
        def fetch(url, headers, timeout):
            self.fetched.append((url, headers, timeout))
            if url == CODEX_USAGE_URL:
                return usage
            assert url == provider_usage.CODEX_RESET_CREDITS_URL
            if isinstance(listing, BaseException):
                raise listing
            return listing

        return ProviderUsageLayer(home=self.home, fetch_json=fetch, now=lambda: NOW).codex_live()

    def test_counts_and_the_nearest_available_expiry_are_one_document(self) -> None:
        codex = self.codex(
            codex_usage({"available_count": 1, "applicable_available_count": 0}),
            credits_listing(credit(CREDITS_EXPIRE_AT)),
        )
        self.assertEqual(codex["status"], "available")
        self.assertEqual(
            codex["reset_credits"],
            {"available": 1, "applicable": 0, "next_expires_at": CREDITS_EXPIRE_AT},
        )
        (_, usage_headers, _), (url, headers, timeout) = self.fetched
        self.assertEqual(url, provider_usage.CODEX_RESET_CREDITS_URL)
        self.assertEqual(headers, usage_headers, "the list is asked with the usage request's own headers")
        self.assertEqual(timeout, 3.0)
        self.assertNotIn("secret", json.dumps(codex))

    def test_a_failing_list_keeps_the_counts_and_drops_the_expiry(self) -> None:
        for failure in (TimeoutError(), OSError("down"), RuntimeError("anything"), KeyError("x")):
            with self.subTest(failure=failure):
                codex = self.codex(
                    codex_usage({"available_count": 2, "applicable_available_count": 1}), failure
                )
                self.assertEqual(codex["status"], "available")
                self.assertEqual(
                    codex["reset_credits"], {"available": 2, "applicable": 1, "next_expires_at": None}
                )

    def test_no_available_credit_asks_for_no_list(self) -> None:
        codex = self.codex(codex_usage({"available_count": 0, "applicable_available_count": 0}))
        self.assertEqual(codex["reset_credits"], {"available": 0, "applicable": 0, "next_expires_at": None})
        self.assertEqual([url for url, _, _ in self.fetched], [CODEX_USAGE_URL])

    def test_usage_without_the_field_carries_no_key(self) -> None:
        for usage in (codex_usage(carry=False), codex_usage(None), codex_usage([]), codex_usage("1")):
            with self.subTest(usage=usage):
                self.fetched.clear()
                codex = self.codex(usage)
                self.assertEqual(codex["status"], "available")
                self.assertNotIn("reset_credits", codex)
                self.assertEqual([url for url, _, _ in self.fetched], [CODEX_USAGE_URL])

    def test_the_rollout_fallback_carries_no_key(self) -> None:
        path = self.home / ".codex/sessions/2026/09/26/rollout-2026-09-26T00-00-00-a.jsonl"
        event = {
            "timestamp": "2026-09-26T00:00:00Z",
            "payload": {
                "rate_limits": {"primary": {"used_percent": 1, "window_minutes": 300}},
                "rate_limit_reset_credits": {"available_count": 1},
            },
        }
        write(path, event)

        def fail(*_args):
            raise TimeoutError

        codex = ProviderUsageLayer(home=self.home, fetch_json=fail, now=lambda: NOW).codex_live()
        self.assertIn(codex["status"], ("available", "stale"))
        self.assertTrue(codex["windows"])
        self.assertNotIn("reset_credits", codex)

    def test_every_hostile_count_is_absent_or_a_clamped_whole_number(self) -> None:
        cases: list[tuple[object, int | None]] = [
            (1, 1),
            (0, 0),
            (2.9, 2),
            (0.5, 0),
            (-3, 0),
            (-(10**400), 0),
            (-1e300, 0),
            (True, None),
            (False, None),
            ("1", None),
            (None, None),
            ([1], None),
            ({"n": 1}, None),
            (float("nan"), None),
            (float("inf"), None),
            (float("-inf"), None),
            (1e300, None),
            (10**400, None),
            (provider_usage.MAX_RESET_CREDITS, provider_usage.MAX_RESET_CREDITS),
            (provider_usage.MAX_RESET_CREDITS + 1, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(provider_usage.credit_count(value), expected)
                self.fetched.clear()
                codex = self.codex(
                    codex_usage({"available_count": value, "applicable_available_count": value}),
                    credits_listing(credit(CREDITS_EXPIRE_AT)),
                )
                self.assertEqual(codex["status"], "available")
                if expected is None:
                    self.assertNotIn("reset_credits", codex)
                else:
                    self.assertEqual(codex["reset_credits"]["available"], expected)
                    self.assertEqual(codex["reset_credits"]["applicable"], expected)
                    self.assertEqual(len(self.fetched), 2 if expected > 0 else 1)

    def test_a_malformed_applicable_count_is_null_and_keeps_the_available_one(self) -> None:
        for value in (True, "0", None, float("nan"), 10**400):
            with self.subTest(value=value):
                codex = self.codex(
                    codex_usage({"available_count": 1, "applicable_available_count": value}), {"credits": []}
                )
                self.assertEqual(
                    codex["reset_credits"], {"available": 1, "applicable": None, "next_expires_at": None}
                )

    def test_every_hostile_moment_is_absent_or_one_utc_moment(self) -> None:
        cases: list[tuple[object, str | None]] = [
            (CREDITS_EXPIRE_AT, CREDITS_EXPIRE_AT),
            ("2026-10-22T22:24:55+02:00", "2026-10-22T20:24:55Z"),
            ("2026-10-22T20:24:55", "2026-10-22T20:24:55Z"),
            ("  2026-10-22T20:24:55Z  ", "2026-10-22T20:24:55Z"),
            (1_792_700_000, "2026-10-22T20:13:20Z"),
            (1_792_700_000_000, "2026-10-22T20:13:20Z"),
            ("1792700000", "2026-10-22T20:13:20Z"),
            (1_792_700_000.5, "2026-10-22T20:13:20.500000Z"),
            ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"),
            ("0001-01-01T00:00:00+14:00", None),
            ("garbage", None),
            ("", None),
            ("   ", None),
            ("nan", None),
            ("inf", None),
            ("1e400", None),
            (10**400, None),
            (-(10**400), None),
            (1e300, None),
            (-1e300, None),
            (float("nan"), None),
            (True, None),
            (None, None),
            ([], None),
            ({}, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(provider_usage.credit_moment_iso(value), expected)

    def test_the_nearest_expiry_is_the_earliest_available_one_and_hostile_lists_are_null(self) -> None:
        earliest = "2026-10-01T00:00:00Z"
        cases: list[tuple[object, str | None]] = [
            (credits_listing(credit(CREDITS_EXPIRE_AT), credit(earliest)), earliest),
            (credits_listing(credit(earliest, "redeemed"), credit(CREDITS_EXPIRE_AT)), CREDITS_EXPIRE_AT),
            (credits_listing(credit(earliest, "AVAILABLE")), earliest),
            (credits_listing(credit(earliest, None), credit(earliest, 1)), None),
            (
                credits_listing(credit("garbage"), credit(10**400), credit(CREDITS_EXPIRE_AT)),
                CREDITS_EXPIRE_AT,
            ),
            (
                credits_listing(credit("1970-01-01T00:00:00Z"), credit(CREDITS_EXPIRE_AT)),
                "1970-01-01T00:00:00Z",
            ),
            (credits_listing(credit("garbage")), None),
            (credits_listing(), None),
            ({"credits": "not a list"}, None),
            ({"credits": {"0": credit(earliest)}}, None),
            ({"credits": [None, 1, "x", [], credit(earliest)]}, earliest),
            ({"available_count": 1}, None),
            ([credit(earliest)], None),
            ("not an object", None),
            (None, None),
        ]
        for listing, expected in cases:
            with self.subTest(listing=listing):
                codex = self.codex(
                    codex_usage({"available_count": 1, "applicable_available_count": 1}), listing
                )
                self.assertEqual(codex["status"], "available")
                self.assertEqual(codex["reset_credits"]["next_expires_at"], expected)
                self.assertEqual(codex["reset_credits"]["available"], 1)


class InstallationCodexHomeTest(unittest.TestCase):
    """The bar reads the Codex account the heads run on (`<data_dir>/codex-home`), not `~/.codex`.

    On production `~/.codex` held a login no head refreshes any more: its live read answered 401
    and its newest rollout was two days old, so the bar showed a stale reading while the heads'
    own home had a current one.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.codex_home = self.root / "data" / "codex-home"

    def test_live_read_uses_the_installation_login(self) -> None:
        write(self.root / ".codex/auth.json", {"tokens": {"access_token": "dead-user-login"}})
        write(self.codex_home / "auth.json", {"tokens": {"access_token": "head-login", "account_id": "a"}})
        seen: list[str] = []

        def fetch(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
            seen.append(headers["Authorization"])
            return {"rate_limit": {"primary_window": {"used_percent": 29, "limit_window_seconds": 604800}}}

        layer = ProviderUsageLayer(
            home=self.root, fetch_json=fetch, now=lambda: NOW, codex_home=lambda: self.codex_home
        )
        codex = layer.codex_live()
        self.assertEqual(seen, ["Bearer head-login"])
        self.assertEqual(codex["status"], "available")
        self.assertEqual(codex["windows"][0]["name"], "weekly")

    def test_fallback_scans_the_installation_sessions(self) -> None:
        write(self.codex_home / "auth.json", {"tokens": {"access_token": "secret"}})
        write(
            self.codex_home / "sessions/2026/01/01/rollout-test.jsonl",
            {
                "timestamp": datetime.fromtimestamp(NOW - 60, UTC).isoformat(),
                "payload": {"rate_limits": {"primary": {"used_percent": 10, "window_minutes": 300}}},
            },
        )

        def fail(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
            raise OSError("offline")

        layer = ProviderUsageLayer(
            home=self.root, fetch_json=fail, now=lambda: NOW, codex_home=self.codex_home
        )
        codex = layer.codex_live()
        self.assertEqual(codex["status"], "available")
        self.assertEqual(codex["windows"][0]["remaining_percent"], 90.0)

    def test_no_installation_login_falls_back_to_the_user_home(self) -> None:
        layer = ProviderUsageLayer(home=self.root, codex_home=lambda: None)
        self.assertEqual(layer.codex_dir, self.root / ".codex")

    def test_installation_codex_dir_needs_a_login(self) -> None:
        from ummanu.runtime.codex_home import installation_codex_dir

        data_dir = self.root / "data"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UMMANU_CODEX_HOME", None)
            os.environ.pop("TA_CODEX_HOME", None)
            self.assertIsNone(installation_codex_dir(data_dir))
            write(self.codex_home / "auth.json", {"tokens": {"access_token": "x"}})
            self.assertEqual(installation_codex_dir(data_dir), self.codex_home)


CLAUDE_TOKEN = "claude-token-value"


def http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(CLAUDE_USAGE_URL, code, f"refused {CLAUDE_TOKEN}", headers, None)


class ProviderStateTest(unittest.TestCase):
    """Each provider keeps its last success and its own next-attempt moment."""

    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.credentials = self.home / ".claude/.credentials.json"
        write(self.credentials, {"claudeAiOauth": {"accessToken": CLAUDE_TOKEN}})
        write(self.home / ".codex/auth.json", {"tokens": {"access_token": "codex-token-value"}})
        self.clock = NOW
        self.fetched: list[str] = []
        self.responses: dict[str, BaseException | dict[str, object]] = {}
        self.claude_answers: list[BaseException | None] = []
        self.layer = ProviderUsageLayer(home=self.home, fetch_json=self.fetch, now=lambda: self.clock)

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        self.fetched.append(url)
        if url in self.responses:
            response = self.responses[url]
            if isinstance(response, BaseException):
                raise response
            return response
        if url == CLAUDE_USAGE_URL:
            failure = self.claude_answers.pop(0) if self.claude_answers else None
            if failure is not None:
                raise failure
            return {"five_hour": {"utilization": 30.0}, "seven_day": {"utilization": 60.0}}
        assert url == CODEX_USAGE_URL
        return {"rate_limit": {"primary_window": {"used_percent": 10, "window_minutes": 300}}}

    def claude(self) -> dict[str, object]:
        return self.layer.usage_snapshot()["providers"][0]

    def claude_calls(self) -> int:
        return self.fetched.count(CLAUDE_USAGE_URL)

    def test_a_rate_limit_after_a_success_is_stale_until_the_retry_after_moment(self) -> None:
        later = NOW + provider_usage.CACHE_SECONDS
        retry = later + 3600
        for header in ("3600", format_datetime(datetime.fromtimestamp(retry, UTC), usegmt=True)):
            with self.subTest(header=header):
                self.setUp()
                success = self.claude()
                self.clock = later
                self.claude_answers.append(http_error(429, header))
                stale = self.claude()
                retry_iso = datetime.fromtimestamp(retry, UTC).isoformat().replace("+00:00", "Z")
                self.assertEqual(stale["status"], "stale")
                self.assertEqual(stale["windows"], success["windows"])
                self.assertEqual(stale["observed_at"], success["observed_at"])
                self.assertEqual(stale["age_seconds"], float(provider_usage.CACHE_SECONDS))
                self.assertEqual(stale["next_attempt_at"], retry_iso)
                self.assertEqual(stale["reason"], f"Claude is rate limited until {retry_iso}")

    def test_no_claude_request_before_its_next_attempt_even_after_codex_reads(self) -> None:
        self.claude()
        self.clock += provider_usage.CACHE_SECONDS
        self.claude_answers.append(http_error(429, "3600"))
        self.claude()
        retry = self.clock + 3600
        codex_before = self.fetched.count(CODEX_USAGE_URL)
        self.clock = retry - 1
        self.layer.invalidate()
        self.claude()
        self.layer.codex_live()
        self.layer.invalidate()
        self.claude()
        self.assertEqual(self.claude_calls(), 2)
        self.assertEqual(self.fetched.count(CODEX_USAGE_URL), codex_before + 3)
        self.clock = retry
        self.assertEqual(self.claude()["status"], "available")
        self.assertEqual(self.claude_calls(), 3)

    def test_a_headerless_rate_limit_backs_off_up_to_its_ceiling_and_a_success_resets_it(self) -> None:
        expected = []
        delay = provider_usage.RATE_LIMIT_BACKOFF_START
        while not expected or expected[-1] < provider_usage.RATE_LIMIT_BACKOFF_CEILING:
            expected.append(min(delay, provider_usage.RATE_LIMIT_BACKOFF_CEILING))
            delay *= 2
        expected.append(provider_usage.RATE_LIMIT_BACKOFF_CEILING)

        def delays(count: int) -> list[float]:
            seen = []
            for _ in range(count):
                self.claude_answers.append(http_error(429))
                document = self.claude()
                due = datetime.fromisoformat(document["next_attempt_at"]).timestamp()
                seen.append(due - self.clock)
                self.clock = due
            return seen

        self.assertEqual(delays(len(expected)), expected)
        self.assertEqual(self.claude()["status"], "available")
        self.clock += provider_usage.CACHE_SECONDS
        self.assertEqual(delays(1), [provider_usage.RATE_LIMIT_BACKOFF_START])

    def test_a_rejected_login_has_its_own_reason_and_leaves_the_token_alone(self) -> None:
        before = self.credentials.read_bytes()
        reasons = {}
        for name, failure in (
            ("401", http_error(401)),
            ("403", http_error(403)),
            ("429", http_error(429, "60")),
            ("timeout", TimeoutError()),
            ("unreachable", URLError(ConnectionRefusedError())),
        ):
            with self.subTest(failure=name):
                self.clock += provider_usage.RATE_LIMIT_BACKOFF_CEILING
                self.claude_answers.append(failure)
                document = self.layer.usage_snapshot()
                self.assertNotIn(CLAUDE_TOKEN, json.dumps(document))
                reasons[name] = document["providers"][0]["reason"]
        source = "from .credentials.json claudeAiOauth.accessToken"
        self.assertEqual(reasons["401"], f"Claude rejected the login {source} (HTTP 401)")
        self.assertEqual(reasons["403"], f"Claude rejected the login {source} (HTTP 403)")
        self.assertEqual(len(set(reasons.values())), len(reasons))
        self.assertEqual(self.credentials.read_bytes(), before)
        self.assertEqual(set(self.fetched), {CLAUDE_USAGE_URL, CODEX_USAGE_URL})

    def test_a_failure_with_no_success_is_unavailable_and_never_replaces_a_success(self) -> None:
        self.claude_answers.append(TimeoutError())
        first = self.claude()
        self.assertEqual((first["status"], first["windows"]), ("unavailable", []))
        self.assertEqual(first["reason"], "Claude did not answer within 3 s")
        self.clock += provider_usage.CACHE_SECONDS
        success = self.claude()
        for step in (1, 2):
            self.clock += provider_usage.CACHE_SECONDS
            self.claude_answers.append(TypeError())
            stale = self.claude()
            self.assertEqual(stale["status"], "stale")
            self.assertEqual(stale["windows"], success["windows"])
            self.assertEqual(stale["age_seconds"], float(step * provider_usage.CACHE_SECONDS))
            self.assertEqual(stale["reason"], "Claude's answer was not a JSON object")
            self.assertNotIn("next_attempt_at", stale)

    def test_concurrent_snapshots_on_a_miss_make_one_request_per_provider(self) -> None:
        callers = 6
        contention = {url: threading.Barrier(callers) for url in (CLAUDE_USAGE_URL, CODEX_USAGE_URL)}

        class ContendedLock:
            def __init__(self, url: str) -> None:
                self.lock = threading.Lock()
                self.waiting = contention[url]

            def __enter__(self) -> None:
                if not self.lock.acquire(blocking=False):
                    self.waiting.wait(5)
                    self.lock.acquire()

            def __exit__(self, *args: object) -> None:
                self.lock.release()

        for name, url in (("claude", CLAUDE_USAGE_URL), ("codex", CODEX_USAGE_URL)):
            self.layer._states[name].lock = ContendedLock(url)
        plain_fetch = self.fetch

        def blocking_fetch(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
            contention[url].wait(5)
            return plain_fetch(url, headers, timeout)

        self.layer.fetch_json = blocking_fetch
        start = threading.Barrier(callers + 1)
        documents: list[dict[str, object]] = []

        def render() -> None:
            start.wait(5)
            documents.append(self.layer.usage_snapshot())

        threads = [threading.Thread(target=render) for _ in range(callers)]
        for thread in threads:
            thread.start()
        start.wait(5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(documents), callers)
        self.assertEqual(self.fetched.count(CLAUDE_USAGE_URL), 1)
        self.assertEqual(self.fetched.count(CODEX_USAGE_URL), 1)
        self.assertEqual({id(document) for document in documents}, {id(documents[0])})

    def test_malformed_usage_is_cached_then_preserves_the_last_success(self) -> None:
        for index, url in enumerate((CLAUDE_USAGE_URL, CODEX_USAGE_URL)):
            for reset in (253402300800, 1e100):
                with self.subTest(url=url, reset=reset):
                    self.setUp()
                    malformed = (
                        {"five_hour": {"utilization": 30, "resets_at": reset}}
                        if index == 0
                        else {"rate_limit": {"primary_window": {"used_percent": 10, "resets_at": reset}}}
                    )
                    self.responses[url] = malformed
                    first = self.layer.usage_snapshot()["providers"][index]
                    self.assertEqual((first["status"], first["windows"]), ("unavailable", []))
                    if index == 0 or reset == 1e100:
                        self.assertEqual(
                            first["reason"], f"{first['label']}'s answer was not a valid usage document"
                        )
                    self.clock += provider_usage.CACHE_SECONDS - 1
                    self.assertIs(self.layer.usage_snapshot()["providers"][index], first)
                    self.assertEqual(self.fetched.count(url), 1)
                    self.responses.pop(url)
                    self.clock += 1
                    success = self.layer.usage_snapshot()["providers"][index]
                    self.assertEqual(success["status"], "available")
                    self.responses[url] = malformed
                    self.clock += provider_usage.CACHE_SECONDS
                    stale = self.layer.usage_snapshot()["providers"][index]
                    self.assertEqual(stale["status"], "stale")
                    self.assertEqual(stale["windows"], success["windows"])
                    self.assertEqual(stale["observed_at"], success["observed_at"])
                    self.assertEqual(stale["age_seconds"], provider_usage.CACHE_SECONDS)
                    self.assertIs(self.layer.usage_snapshot()["providers"][index], stale)
                    self.assertEqual(self.fetched.count(url), 3)

    def test_invalidate_preserves_codex_retry_after_and_doubling_backoff(self) -> None:
        for header in ("3600", None):
            with self.subTest(header=header):
                self.setUp()
                self.responses[CODEX_USAGE_URL] = http_error(429, header)
                for delay in (600, 1200):
                    document = self.layer.usage_snapshot()["providers"][1]
                    retry = self.clock + (int(header) if header else delay)
                    self.assertEqual(document["next_attempt_at"], provider_usage._iso(retry))
                    self.clock = retry - 1
                    self.layer.invalidate()
                    self.assertIs(self.layer.usage_snapshot()["providers"][1], document)
                    self.clock = retry
                self.assertEqual(self.fetched.count(CODEX_USAGE_URL), 2)
                self.responses.pop(CODEX_USAGE_URL)
                self.assertEqual(self.layer.usage_snapshot()["providers"][1]["status"], "available")
                self.layer.invalidate()
                self.layer.usage_snapshot()
                self.assertEqual(self.fetched.count(CODEX_USAGE_URL), 4)
                self.responses[CODEX_USAGE_URL] = http_error(429, header)
                self.clock += provider_usage.CACHE_SECONDS
                document = self.layer.usage_snapshot()["providers"][1]
                retry = self.clock + (int(header) if header else provider_usage.RATE_LIMIT_BACKOFF_START)
                self.assertEqual(document["next_attempt_at"], provider_usage._iso(retry))

    def test_non_ascii_retry_after_uses_rate_limit_backoff(self) -> None:
        for index, url in enumerate((CLAUDE_USAGE_URL, CODEX_USAGE_URL)):
            with self.subTest(url=url):
                self.setUp()
                self.responses[url] = http_error(429, "²")
                document = self.layer.usage_snapshot()["providers"][index]
                retry = self.clock + provider_usage.RATE_LIMIT_BACKOFF_START
                self.assertEqual(document["next_attempt_at"], provider_usage._iso(retry))
                self.clock = retry - 1
                self.layer.invalidate()
                self.assertIs(self.layer.usage_snapshot()["providers"][index], document)
                self.assertEqual(self.fetched.count(url), 1)


class DamagedAuthTest(unittest.TestCase):
    """Invalid UTF-8 auth files behave like missing tokens without changing their bytes."""

    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fetch = mock.Mock(side_effect=AssertionError("unexpected provider request"))
        self.layer = ProviderUsageLayer(home=self.home, fetch_json=self.fetch, now=lambda: NOW, environ={})

    def damage(self, source: str) -> Path:
        path = self.home / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff")
        return path

    def test_each_damaged_source_is_unavailable_without_a_request(self) -> None:
        for source in (".claude/.credentials.json", ".claude/settings.json", ".codex/auth.json"):
            with self.subTest(source=source):
                path = self.damage(source)
                layer = ProviderUsageLayer(home=self.home, fetch_json=self.fetch, now=lambda: NOW, environ={})
                providers = layer.usage_snapshot()["providers"]
                self.assertEqual(
                    [(p["status"], p["windows"]) for p in providers],
                    [("unavailable", []), ("unavailable", [])],
                )
                self.fetch.assert_not_called()
                self.assertEqual(path.read_bytes(), b"\xff")
                path.unlink()

    def test_damaged_credentials_fall_through_to_environment_then_settings(self) -> None:
        path = self.damage(".claude/.credentials.json")
        write(self.home / ".claude/settings.json", {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "settings-token"}})
        environ = {"CLAUDE_CODE_OAUTH_TOKEN": "env-token"}
        self.fetch.side_effect = None
        self.fetch.return_value = {"five_hour": {"utilization": 30.0}}
        for token in ("env-token", "settings-token"):
            with self.subTest(token=token):
                layer = ProviderUsageLayer(
                    home=self.home, fetch_json=self.fetch, now=lambda: NOW, environ=environ
                )
                document = layer.usage_snapshot()
                self.assertEqual(document["providers"][0]["status"], "available")
                self.assertEqual(self.fetch.call_args.args[1]["Authorization"], f"Bearer {token}")
                self.assertNotIn(token, json.dumps(document))
                self.assertEqual(path.read_bytes(), b"\xff")
                environ.clear()
        self.assertEqual(self.fetch.call_count, 2)

    def test_damaged_codex_auth_falls_back_to_a_session_without_a_request(self) -> None:
        path = self.damage(".codex/auth.json")
        write(
            self.home / ".codex/sessions/2026/01/01/rollout-test.jsonl",
            rate_limit_event(20, datetime.fromtimestamp(NOW - 60, UTC)),
        )
        codex = self.layer.codex_live()
        self.assertEqual(codex["status"], "available")
        self.assertEqual(codex["windows"][0]["remaining_percent"], 80.0)
        self.fetch.assert_not_called()
        self.assertEqual(path.read_bytes(), b"\xff")


class ClaudeLoginSourceTest(unittest.TestCase):
    """The Claude token comes from credentials, then the environment, then settings.json."""

    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.credentials = self.home / ".claude/.credentials.json"
        self.settings = self.home / ".claude/settings.json"
        self.environ: dict[str, str] = {}
        self.authorizations: list[str] = []
        self.answer: BaseException | None = None
        self.clock = NOW
        self.layer = ProviderUsageLayer(
            home=self.home, fetch_json=self.fetch, now=lambda: self.clock, environ=self.environ
        )

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        assert url == CLAUDE_USAGE_URL
        self.authorizations.append(headers["Authorization"])
        if self.answer is not None:
            raise self.answer
        return {"five_hour": {"utilization": 30.0}}

    def claude(self) -> dict[str, object]:
        return self.layer.usage_snapshot()["providers"][0]

    def test_an_empty_credentials_file_falls_through_to_settings_and_stays_unchanged(self) -> None:
        write(self.credentials, {"claudeAiOauth": {"accessToken": "", "expiresAt": 0}})
        write(self.settings, {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "settings-token"}})
        before = self.credentials.read_bytes()
        self.assertEqual(self.claude()["status"], "available")
        self.assertEqual(self.authorizations, ["Bearer settings-token"])
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_each_due_fetch_rereads_credentials_then_environment_then_settings(self) -> None:
        write(self.settings, {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "settings-token"}})
        self.claude()
        self.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "env-token"
        self.clock += provider_usage.CACHE_SECONDS
        self.claude()
        write(self.credentials, {"claudeAiOauth": {"accessToken": "credentials-token"}})
        self.clock += provider_usage.CACHE_SECONDS
        self.claude()
        self.assertEqual(
            self.authorizations, ["Bearer settings-token", "Bearer env-token", "Bearer credentials-token"]
        )

    def test_a_malformed_credentials_file_falls_through_to_a_later_source(self) -> None:
        self.credentials.parent.mkdir(parents=True)
        self.credentials.write_text("{not json", encoding="utf-8")
        self.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "env-token"
        self.assertEqual(self.claude()["status"], "available")
        self.assertEqual(self.authorizations, ["Bearer env-token"])

    def test_no_source_is_unavailable_names_the_sources_and_makes_no_request(self) -> None:
        write(self.settings, {"env": {}})
        claude = self.claude()
        self.assertEqual(claude["status"], "unavailable")
        self.assertEqual(
            claude["reason"],
            "Claude login is unavailable (looked in .credentials.json claudeAiOauth.accessToken, "
            "environment CLAUDE_CODE_OAUTH_TOKEN, settings.json CLAUDE_CODE_OAUTH_TOKEN)",
        )
        self.assertEqual(self.authorizations, [])

    def test_a_rejected_settings_token_names_its_source_and_never_the_token(self) -> None:
        write(self.settings, {"env": {"CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN}})
        self.answer = http_error(401)
        claude = self.claude()
        self.assertEqual(
            claude["reason"],
            "Claude rejected the login from settings.json CLAUDE_CODE_OAUTH_TOKEN (HTTP 401)",
        )
        self.assertNotIn(CLAUDE_TOKEN, json.dumps(claude))


class ClaudeStatuslineTest(unittest.TestCase):
    def setUp(self) -> None:
        from ummanu.runtime import claude_usage

        self.capture = claude_usage.capture
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.cache = self.home / ".claude" / claude_usage.CACHE_NAME
        self.calls = []
        self.clock = NOW
        self.limits = {
            "five_hour": {"used_percentage": 0, "resets_at": NOW + 300},
            "seven_day": {"used_percentage": 67, "resets_at": NOW + 10080},
        }
        self.layer = ProviderUsageLayer(home=self.home, fetch_json=self.fetch, now=lambda: self.clock)
        write(self.home / ".claude/settings.json", {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "secret"}})

    def fetch(self, url, headers, timeout):
        self.calls.append(url)
        message = Message()
        message["Retry-After"] = "3600"
        raise HTTPError(url, 429, "throttled", message, None)

    def claude(self):
        return self.layer.usage_snapshot()["providers"][0]

    def record(self, limits=None, observed=None):
        return self.capture(
            json.dumps(
                {
                    "rate_limits": self.limits if limits is None else limits,
                    "token": "secret",
                    "transcript_path": "/private/session",
                }
            ).encode(),
            self.home,
            observed=self.clock if observed is None else observed,
        )

    def test_real_payload_becomes_remaining_percent_without_http_or_auth(self) -> None:
        (self.home / ".claude/settings.json").unlink()
        self.assertTrue(self.record())
        reading = self.claude()
        self.assertEqual(reading["status"], "available")
        self.assertEqual([w["remaining_percent"] for w in reading["windows"]], [100, 33])
        self.assertEqual(reading["observed_at"], provider_usage._iso(NOW))
        self.assertEqual(reading["age_seconds"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("secret", self.cache.read_text())
        self.assertNotIn("transcript", self.cache.read_text())

    def test_new_cli_reading_visible_during_existing_http_cooldown(self) -> None:
        failed = self.claude()
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(self.calls, [CLAUDE_USAGE_URL])
        self.clock += 10
        self.record()
        self.assertEqual(self.claude()["status"], "available")
        self.clock += 1000
        stale = self.claude()
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["age_seconds"], 1000)
        self.assertEqual(stale["observed_at"], provider_usage._iso(NOW + 10))
        self.assertEqual(stale["windows"][1]["remaining_percent"], 33)
        self.assertEqual(stale["next_attempt_at"], failed["next_attempt_at"])
        self.layer.invalidate()
        self.layer.codex_live()
        self.assertEqual(self.claude()["status"], "stale")
        self.assertEqual(self.calls, [CLAUDE_USAGE_URL])
        self.clock = NOW + 3601
        self.claude()
        self.assertEqual(self.calls, [CLAUDE_USAGE_URL, CLAUDE_USAGE_URL])

    def test_reset_window_is_stale_and_never_becomes_zero(self) -> None:
        self.record()
        self.clock += 301
        reading = self.claude()
        self.assertEqual(reading["status"], "stale")
        self.assertEqual(reading["windows"][1]["remaining_percent"], 33)
        self.assertEqual(reading["age_seconds"], 301)

    def test_missing_or_malformed_payload_keeps_previous_snapshot(self) -> None:
        self.record()
        before = self.cache.read_bytes()
        for raw in (b"{}", b"not json", b"[]", b"\xff", b"x" * (1024 * 1024 + 1)):
            with self.subTest(raw=raw[:20]):
                self.assertFalse(self.capture(raw, self.home))
                self.assertEqual(self.cache.read_bytes(), before)
        self.clock += 1  # A valid changed window must now replace the existing snapshot.
        for value in (True, -1, 101, float("nan"), float("inf"), 10**1000):
            self.assertFalse(self.record({"five_hour": {"used_percentage": value, "resets_at": NOW}}))
        for value in (True, -1, float("nan"), float("inf"), 10**1000, 253402300800):
            self.assertFalse(self.record({"five_hour": {"used_percentage": 0, "resets_at": value}}))
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertTrue(self.record({"five_hour": {"used_percentage": 50, "resets_at": NOW + 300}}))
        self.assertEqual(json.loads(self.cache.read_text())["observed_at"], NOW + 1)

    def test_bounded_corrupt_and_future_cache_falls_back_to_http(self) -> None:
        self.cache.parent.mkdir(exist_ok=True)
        for raw in (b"x" * 4097, b"\xff", b"[]", b"{broken", b"{}"):
            self.cache.write_bytes(raw)
            self.assertIsNone(self.layer._claude_statusline(NOW))
        self.record(observed=NOW + 1)
        self.assertIsNone(self.layer._claude_statusline(NOW))
        self.assertEqual(self.claude()["status"], "unavailable")
        self.assertEqual(self.calls, [CLAUDE_USAGE_URL])

    def test_only_present_valid_windows_are_kept(self) -> None:
        self.record({"seven_day": self.limits["seven_day"], "five_hour": None})
        reading = self.claude()
        self.assertEqual(len(reading["windows"]), 1)
        self.assertEqual(reading["windows"][0]["name"], "weekly")

    def test_newer_successful_http_reading_wins_over_old_cli_data(self) -> None:
        self.record(observed=NOW - 1000)
        self.layer.fetch_json = lambda *_: {"seven_day": {"utilization": 70}}
        reading = self.claude()
        self.assertEqual(reading["status"], "available")
        self.assertEqual(reading["windows"][0]["remaining_percent"], 30)

    def test_cache_write_failure_does_not_break_capture(self) -> None:
        with mock.patch("ummanu.runtime.claude_usage.os.replace", side_effect=PermissionError):
            self.assertFalse(self.record())
        self.assertEqual(list((self.home / ".claude").glob(".ummanu-usage-*")), [])

    def test_wrapper_forwards_exact_input_and_existing_stdout(self) -> None:
        import subprocess
        import sys

        transcript = self.home / "transcript.jsonl"
        transcript.write_text("private contents")
        payload = json.dumps(
            {"rate_limits": self.limits, "session_id": "private-session", "transcript_path": str(transcript)}
        ).encode()
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys, runpy; sys.path.insert(0, "
                + repr(str(Path(provider_usage.__file__).parents[2]))
                + "); runpy.run_path("
                + repr(str(Path(provider_usage.__file__).parents[1] / "runtime/claude_usage.py"))
                + ", run_name='__main__')",
                "--forward",
                "cat",
            ],
            input=payload,
            capture_output=True,
            check=True,
            env={**os.environ, "HOME": str(self.home)},
        )
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"")
        self.assertNotIn("session", self.cache.read_text())

    def test_rerenders_and_older_sessions_cannot_refresh_or_replace_snapshot(self) -> None:
        self.record()
        before = self.cache.read_bytes()
        self.clock += 1000
        self.assertFalse(self.record())
        self.assertEqual(self.cache.read_bytes(), before)
        older = {"seven_day": {"used_percentage": 65, "resets_at": NOW + 10080}}
        self.assertFalse(self.record(older, observed=NOW - 1))
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(self.claude()["status"], "stale")

    def test_transcript_is_only_statted_and_observation_uses_its_mtime(self) -> None:
        transcript = self.home / "turn.jsonl"
        transcript.write_text("not JSON, must not be read")
        os.utime(transcript, (NOW - 100, NOW - 100))
        payload = json.dumps({"transcript_path": str(transcript), "rate_limits": self.limits}).encode()
        with mock.patch("ummanu.runtime.claude_usage.time.time", return_value=NOW):
            self.assertTrue(self.capture(payload, self.home))
        data = json.loads(self.cache.read_text())
        self.assertEqual(data["observed_at"], NOW - 100)
        self.assertNotIn("transcript", self.cache.read_text())

    def test_first_render_without_limits_forwards_without_cache_changes(self) -> None:
        import subprocess
        import sys

        self.record()
        before = self.cache.read_bytes()
        payload = b'{"session_id":"first-render"}'
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys, runpy; sys.path.insert(0, "
                + repr(str(Path(provider_usage.__file__).parents[2]))
                + "); runpy.run_path("
                + repr(str(Path(provider_usage.__file__).parents[1] / "runtime/claude_usage.py"))
                + ", run_name='__main__')",
                "--forward",
                "cat",
            ],
            input=payload,
            capture_output=True,
            check=True,
            cwd=self.home,
            env={**os.environ, "HOME": str(self.home), "PYTHONPATH": str(self.home)},
        )
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.cache.read_bytes(), before)

    def test_isolated_wrapper_survives_shadowing_modules_and_capture_exception(self) -> None:
        import subprocess
        import sys

        for name in ("json", "argparse", "subprocess", "tempfile", "typing"):
            (self.home / (name + ".py")).write_text("raise RuntimeError('workspace module')")
        command = (
            "import sys, runpy; sys.path.insert(0, "
            + repr(str(Path(provider_usage.__file__).parents[2]))
            + "); data=runpy.run_path("
            + repr(str(Path(provider_usage.__file__).parents[1] / "runtime/claude_usage.py"))
            + "); "
            "exec('def broken(*a, **k): raise RuntimeError()'); "
            "data['main'].__globals__['capture']=broken; data['main']()"
        )
        payload = b'{"rate_limits": {}}'
        result = subprocess.run(
            [sys.executable, "-I", "-c", command, "--forward", "cat"],
            input=payload,
            capture_output=True,
            check=True,
            cwd=self.home,
            env={**os.environ, "HOME": str(self.home), "PYTHONPATH": str(self.home)},
        )
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"")

    def test_new_response_replaces_existing_snapshot_and_updates_the_bar(self) -> None:
        transcript = self.home / "turn.jsonl"
        transcript.write_text("private transcript contents are never read")
        payload = {"transcript_path": str(transcript), "rate_limits": self.limits}
        os.utime(transcript, (NOW, NOW))
        with mock.patch("ummanu.runtime.claude_usage.time.time", return_value=NOW):
            self.assertTrue(self.capture(json.dumps(payload).encode(), self.home))
        self.assertEqual(self.claude()["windows"][1]["remaining_percent"], 33)
        payload["rate_limits"] = {
            "five_hour": {"used_percentage": 3, "resets_at": NOW + 300},
            "seven_day": {"used_percentage": 68, "resets_at": NOW + 10080},
        }
        self.clock = NOW + 60
        os.utime(transcript, (self.clock, self.clock))
        with mock.patch("ummanu.runtime.claude_usage.time.time", return_value=self.clock):
            self.assertTrue(self.capture(json.dumps(payload).encode(), self.home))
        data = json.loads(self.cache.read_text())
        self.assertEqual(data["observed_at"], NOW + 60)
        self.assertEqual(data["rate_limits"], payload["rate_limits"])
        reading = self.claude()
        self.assertEqual(reading["status"], "available")
        self.assertEqual(reading["observed_at"], provider_usage._iso(NOW + 60))
        self.assertEqual([w["remaining_percent"] for w in reading["windows"]], [97, 32])
        self.assertEqual(self.calls, [])
