from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

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


def test_missing_or_malformed_auth_is_explicitly_unavailable(tmp_path: Path) -> None:
    write(tmp_path / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": 3}})
    document = ProviderUsageLayer(home=tmp_path, now=lambda: NOW).usage_snapshot()
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

        return ProviderUsageLayer(home=self.home, fetch_json=fail, now=lambda: NOW)._codex(NOW)

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

        return ProviderUsageLayer(home=self.home, fetch_json=fetch, now=lambda: NOW)._codex(NOW)

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

        codex = ProviderUsageLayer(home=self.home, fetch_json=fail, now=lambda: NOW)._codex(NOW)
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
        codex = layer._codex(NOW)
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
        codex = layer._codex(NOW)
        self.assertEqual(codex["status"], "available")
        self.assertEqual(codex["windows"][0]["remaining_percent"], 90.0)

    def test_no_installation_login_falls_back_to_the_user_home(self) -> None:
        layer = ProviderUsageLayer(home=self.root, codex_home=lambda: None)
        self.assertEqual(layer.codex_dir, self.root / ".codex")

    def test_installation_codex_dir_needs_a_login(self) -> None:
        from ummanu.runtime.codex_home import installation_codex_dir

        data_dir = self.root / "data"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TA_CODEX_HOME", None)
            self.assertIsNone(installation_codex_dir(data_dir))
            write(self.codex_home / "auth.json", {"tokens": {"access_token": "x"}})
            self.assertEqual(installation_codex_dir(data_dir), self.codex_home)
