"""Read-only subscription usage snapshots for the local dashboard.

Credentials are used only for the providers' own usage request: never returned, logged, cached or
put in an error message. Codex session rollouts are a fallback, since the CLI records the same
rate-limit document on normal turns.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_RESET_CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
#: Spending a credit is an explicit owner act, not a background poll, so the provider gets more time.
CODEX_RESET_TIMEOUT = 30.0
#: The consume answer's known `code`s, each recorded as that outcome; any other code is an error.
CODEX_RESET_CODES = frozenset({"reset", "nothing_to_reset", "no_credit", "already_redeemed"})
STALE_AFTER_SECONDS = 15 * 60
CACHE_SECONDS = 5 * 60
# The Codex fallback reads a bounded amount however large ~/.codex/sessions grows: it descends the
# sessions/YYYY/MM/DD directories newest-first, opens at most CODEX_FALLBACK_FILES rollouts and reads
# only the last CODEX_TAIL_BYTES of each.
CODEX_FALLBACK_FILES = 20
CODEX_FALLBACK_LISTINGS = 32
CODEX_TAIL_BYTES = 256 * 1024


def _iso(epoch: float | str | None) -> str | None:
    if isinstance(epoch, str):
        try:
            parsed = datetime.fromisoformat(epoch)
        except ValueError:
            return None
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


#: The largest reset-credit count believed possible; larger is a malformed field, not a figure.
MAX_RESET_CREDITS = 1_000_000
#: A numeric credit moment below this is unix seconds, at or above it unix milliseconds.
CREDIT_MILLISECONDS_FROM = 10_000_000_000


def credit_count(value: Any) -> int | None:
    """A reset-credit count, floored and clamped at 0, or `None` when the value is not a count.

    The one normaliser for reset-credit counts. A bool, string, non-finite number or one beyond
    :data:`MAX_RESET_CREDITS` is `None`, never zero: no reading is not a reading of none.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value > MAX_RESET_CREDITS:
        return None
    return max(0, math.floor(value))


def credit_moment(value: Any) -> datetime | None:
    """A reset-credit moment as an aware UTC datetime, or `None` when it is not one.

    Reads ISO-8601 (no offset means UTC) or unix seconds/milliseconds, as a number or numeric string.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            try:
                moment = datetime.fromisoformat(text)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                return moment.astimezone(UTC)
            except (OverflowError, ValueError):
                return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return None
        if seconds >= CREDIT_MILLISECONDS_FROM:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, ValueError, OSError):
        return None


def credit_moment_iso(value: Any) -> str | None:
    """A reset-credit moment as the ISO-8601 `Z` string a provider document carries."""
    moment = credit_moment(value)
    return None if moment is None else _credit_iso(moment)


def _credit_iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _next_credit_expiry(listing: Any) -> str | None:
    """The earliest `expires_at` among the listed credits whose status is `available`."""
    credits = listing.get("credits") if isinstance(listing, dict) else None
    if not isinstance(credits, list):
        return None
    moments = [
        moment
        for credit in credits
        if isinstance(credit, dict)
        and isinstance(credit.get("status"), str)
        and credit["status"].lower() == "available"
        and (moment := credit_moment(credit.get("expires_at"))) is not None
    ]
    return _credit_iso(min(moments)) if moments else None


def _window(name: str, raw: Any, *, default_minutes: int | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("used_percent", raw.get("used_percentage")))
    if used is None:
        # Claude's `utilization` is already a percentage: a fresh window reads 1.0, not 0.01.
        used = _number(raw.get("utilization"))
    if used is None:
        return None
    minutes = raw.get("window_minutes")
    if minutes is None and isinstance(raw.get("limit_window_seconds"), (int, float)):
        minutes = round(raw["limit_window_seconds"] / 60)
    if minutes is None:
        minutes = default_minutes
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        minutes = default_minutes
    reset = raw.get("resets_at", raw.get("reset_at"))
    return {
        "name": name,
        "window_minutes": minutes,
        "remaining_percent": round(max(0.0, min(100.0, 100.0 - used)), 1),
        "resets_at": _iso(reset),
    }


def _without(text: str, *secrets: str | None) -> str:
    """`text` with every non-empty secret in it replaced, for a reason that quotes the provider."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _fetch_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read(1024 * 1024))
    if not isinstance(value, dict):
        raise TypeError("provider returned a non-object document")
    return value


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> Any:
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(1024 * 1024))


class ProviderUsageLayer:
    """Collect compact Claude and Codex usage documents without mutating provider auth."""

    def __init__(
        self,
        *,
        home: str | os.PathLike[str] | None = None,
        fetch_json: Callable[[str, dict[str, str], float], dict[str, Any]] = _fetch_json,
        post_json: Callable[[str, dict[str, str], dict[str, Any], float], Any] = _post_json,
        now: Callable[[], float] = time.time,
        timeout: float = 3.0,
        codex_home: str | os.PathLike[str] | Callable[[], Path | None] | None = None,
    ) -> None:
        self.home = Path(home) if home is not None else Path.home()
        # The CODEX_HOME the installation's heads run with (`<data_dir>/codex-home`): its login is kept
        # refreshed and its `sessions/` written by the heads. `~/.codex` is only the fallback.
        self._codex_home = codex_home
        self.fetch_json = fetch_json
        self.post_json = post_json
        self.now = now
        self.timeout = timeout
        self._cached: tuple[float, dict[str, Any]] | None = None

    @property
    def codex_dir(self) -> Path:
        """The Codex home the bar reads its login and session fallback from, resolved per read."""
        configured = self._codex_home() if callable(self._codex_home) else self._codex_home
        return Path(configured) if configured is not None else self.home / ".codex"

    def invalidate(self) -> None:
        """Forget the cached snapshot, so the next render asks the providers again."""
        self._cached = None

    def codex_live(self) -> dict[str, Any]:
        """The Codex reading as it is now, past the cache and without replacing what the cache holds."""
        return self._codex(self.now())

    def consume_codex_reset(self, redeem_request_id: str) -> tuple[str, str | None]:
        """Spend one Codex rate-limit reset credit under `redeem_request_id`: `(outcome, reason)`.

        The outcome is a :data:`CODEX_RESET_CODES` code with no reason, or `error` with one; it never
        raises. The provider deduplicates on `redeem_request_id`. The token goes only into the header;
        no reason carries it.
        """
        auth = self.codex_dir / "auth.json"
        token = self._auth(auth, ("tokens", "access_token"))
        if token is None:
            return "error", "Codex login is unavailable"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        account = self._auth(auth, ("tokens", "account_id"))
        if account:
            headers["ChatGPT-Account-Id"] = account
        try:
            answer = self.post_json(
                CODEX_RESET_CONSUME_URL,
                headers,
                {"redeem_request_id": redeem_request_id},
                CODEX_RESET_TIMEOUT,
            )
        except HTTPError as exc:
            return "error", f"the provider answered HTTP {exc.code}"
        except (TimeoutError, socket.timeout):
            return "error", f"the provider did not answer within {CODEX_RESET_TIMEOUT:g} s"
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return "error", f"the provider did not answer within {CODEX_RESET_TIMEOUT:g} s"
            return "error", f"the provider could not be reached ({type(exc.reason).__name__})"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "error", "the provider's answer was not JSON"
        except OSError as exc:
            return "error", f"the provider could not be reached ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001 -- an injected or future transport failure is still an outcome
            return "error", f"the consume call failed ({type(exc).__name__})"
        if not isinstance(answer, dict):
            return "error", "the provider's answer was not a JSON object"
        code = answer.get("code")
        if isinstance(code, str) and code in CODEX_RESET_CODES:
            return code, None
        shown = code[:40] if isinstance(code, str) else type(code).__name__
        return "error", _without(f"the provider answered an unknown code: {shown!r}", token, account)

    def usage_snapshot(self) -> dict[str, Any]:
        observed = self.now()
        if self._cached is not None and observed - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        document = {
            "kind": "provider_usage",
            "observed_at": _iso(observed),
            "providers": [self._claude(observed), self._codex(observed)],
        }
        self._cached = (observed, document)
        return document

    def _auth(self, path: Path, keys: tuple[str, ...]) -> str | None:
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
            for key in keys:
                value = value[key]
            return value if isinstance(value, str) and value else None
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _claude(self, observed: float) -> dict[str, Any]:
        token = self._auth(self.home / ".claude" / ".credentials.json", ("claudeAiOauth", "accessToken"))
        if token is None:
            return self._unavailable("claude", "Claude", "Claude login is unavailable")
        try:
            raw = self.fetch_json(
                CLAUDE_USAGE_URL,
                {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
                self.timeout,
            )
            windows = [
                item
                for item in (
                    _window("5-hour", raw.get("five_hour"), default_minutes=300),
                    _window("weekly", raw.get("seven_day"), default_minutes=10080),
                )
                if item is not None
            ]
            return (
                self._available("claude", "Claude", observed, windows)
                if windows
                else self._unavailable("claude", "Claude", "Claude did not report usage windows")
            )
        except (OSError, TypeError, ValueError, HTTPError, URLError, TimeoutError):
            return self._unavailable("claude", "Claude", "Claude usage is temporarily unavailable")

    def _codex(self, observed: float) -> dict[str, Any]:
        auth = self.codex_dir / "auth.json"
        token = self._auth(auth, ("tokens", "access_token"))
        if token is not None:
            try:
                headers = {"Authorization": f"Bearer {token}"}
                account = self._auth(auth, ("tokens", "account_id"))
                if account:
                    headers["ChatGPT-Account-Id"] = account
                raw = self.fetch_json(CODEX_USAGE_URL, headers, self.timeout)
                windows = self._codex_windows(raw)
                if windows:
                    result = self._available("codex", "Codex", observed, windows)
                    credits = self._codex_reset_credits(raw, headers)
                    if credits is not None:
                        result["reset_credits"] = credits
                    return result
            except (OSError, TypeError, ValueError, HTTPError, URLError, TimeoutError):
                pass
        fallback = self._latest_codex_event()
        if fallback is None:
            reason = (
                "Codex login is unavailable" if token is None else "Codex usage is temporarily unavailable"
            )
            return self._unavailable("codex", "Codex", reason)
        raw, source_time = fallback
        windows = self._codex_windows(raw)
        age = max(0.0, observed - source_time)
        if not windows:
            return self._unavailable("codex", "Codex", "Codex did not report usage windows")
        result = self._available("codex", "Codex", source_time, windows)
        result["age_seconds"] = round(age, 1)
        if age > STALE_AFTER_SECONDS:
            result["status"] = "stale"
            result["reason"] = "Latest Codex usage observation is stale"
        return result

    def _codex_reset_credits(self, raw: dict[str, Any], headers: dict[str, str]) -> dict[str, Any] | None:
        """The live reading's reset credits, or `None` when it carries no count this layer can read.

        The credits list is fetched only when a credit is available; any failure there only drops
        the expiry and never makes the Codex reading unavailable.
        """
        summary = raw.get("rate_limit_reset_credits")
        if not isinstance(summary, dict):
            return None
        available = credit_count(summary.get("available_count"))
        if available is None:
            return None
        expires = None
        if available > 0:
            try:
                expires = _next_credit_expiry(self.fetch_json(CODEX_RESET_CREDITS_URL, headers, self.timeout))
            except Exception:  # noqa: BLE001 -- any failure of the list only drops the expiry
                expires = None
        return {
            "available": available,
            "applicable": credit_count(summary.get("applicable_available_count")),
            "next_expires_at": expires,
        }

    def _codex_windows(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        limits = raw.get("rate_limits", raw.get("rate_limit", raw))
        if not isinstance(limits, dict):
            return []
        primary = limits.get("primary", limits.get("primary_window"))
        secondary = limits.get("secondary", limits.get("secondary_window"))
        return [
            item
            for item in (
                _window(self._window_name(primary, "primary"), primary),
                _window(self._window_name(secondary, "secondary"), secondary),
            )
            if item is not None
        ]

    @staticmethod
    def _window_name(raw: Any, fallback: str) -> str:
        minutes = raw.get("window_minutes") if isinstance(raw, dict) else None
        if (
            minutes is None
            and isinstance(raw, dict)
            and isinstance(raw.get("limit_window_seconds"), (int, float))
        ):
            minutes = round(raw["limit_window_seconds"] / 60)
        if minutes == 300:
            return "5-hour"
        if minutes == 10080:
            return "weekly"
        return fallback

    def _latest_codex_event(self) -> tuple[dict[str, Any], float] | None:
        for path, mtime in self._newest_codex_rollouts():
            for line in self._tail_lines(path):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                limits = self._event_rate_limits(event)
                if limits is not None:
                    timestamp = event.get("timestamp")
                    try:
                        source_time = datetime.fromisoformat(timestamp).timestamp()
                    except (AttributeError, ValueError):
                        source_time = mtime
                    return limits, source_time
        return None

    @staticmethod
    def _event_rate_limits(event: Any) -> dict[str, Any] | None:
        """The rate-limit document a rollout event carries, in the current or the older shape.

        Current rollouts put it at `payload.rate_limits` beside `payload.info`; older ones nested it
        at `payload.info.rate_limits`.
        """
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return None
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            return limits
        info = payload.get("info")
        limits = info.get("rate_limits") if isinstance(info, dict) else None
        return limits if isinstance(limits, dict) else None

    def _newest_codex_rollouts(self) -> list[tuple[Path, float]]:
        """Return up to CODEX_FALLBACK_FILES rollouts from the newest days, newest mtime first.

        Directories are listed newest-first, at most CODEX_FALLBACK_LISTINGS of them; within a day the
        file name (which starts with the session's start time) picks the newest ones.
        """
        budget = [CODEX_FALLBACK_LISTINGS]
        found: list[Path] = []

        def listing(path: Path) -> list[os.DirEntry[str]]:
            if budget[0] <= 0:
                return []
            budget[0] -= 1
            try:
                with os.scandir(path) as entries:
                    return list(entries)
            except OSError:
                return []

        def dated(entries: list[os.DirEntry[str]]) -> list[os.DirEntry[str]]:
            directories = []
            for entry in entries:
                try:
                    if entry.name.isdigit() and entry.is_dir():
                        directories.append(entry)
                except OSError:
                    continue
            return sorted(directories, key=lambda entry: entry.name, reverse=True)

        def descend(path: Path, depth: int) -> None:
            entries = listing(path)
            if depth == 3:
                names = sorted(
                    (
                        entry.name
                        for entry in entries
                        if entry.name.startswith("rollout-") and entry.name.endswith(".jsonl")
                    ),
                    reverse=True,
                )
                found.extend(path / name for name in names[: CODEX_FALLBACK_FILES - len(found)])
                return
            for entry in dated(entries):
                if len(found) >= CODEX_FALLBACK_FILES or budget[0] <= 0:
                    return
                descend(path / entry.name, depth + 1)

        descend(self.codex_dir / "sessions", 0)
        dated_files = []
        for path in found:
            try:
                dated_files.append((path, path.stat().st_mtime))
            except OSError:
                continue
        return sorted(dated_files, key=lambda item: item[1], reverse=True)

    @staticmethod
    def _tail_lines(path: Path) -> list[str]:
        """Return the complete lines in the last CODEX_TAIL_BYTES of a file, last line first."""
        try:
            with open(path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - CODEX_TAIL_BYTES)
                handle.seek(start)
                tail = handle.read(CODEX_TAIL_BYTES)
        except OSError:
            return []
        lines = tail.split(b"\n")
        if start > 0:
            lines = lines[1:]
        return [line.decode("utf-8", errors="replace") for line in reversed(lines) if line.strip()]

    @staticmethod
    def _available(
        provider_id: str, label: str, observed: float, windows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "id": provider_id,
            "label": label,
            "status": "available",
            "reason": None,
            "observed_at": _iso(observed),
            "age_seconds": 0.0,
            "windows": windows,
        }

    @staticmethod
    def _unavailable(provider_id: str, label: str, reason: str) -> dict[str, Any]:
        return {
            "id": provider_id,
            "label": label,
            "status": "unavailable",
            "reason": reason,
            "observed_at": None,
            "age_seconds": None,
            "windows": [],
        }
