"""Classify provider refusals of a head turn or resource probe, without secrets.

Shared by the resource probe (`ummanu.head_health`, `resource_probe`), the dispatcher's turn check
(`dispatch.provider_failure`) and the PO runner. In scope: a spent subscription (a usage/quota
limit, with the reset time the provider names), HTTP 401/403, 429, 5xx, and a connection the client
gave up on after its own retries; anything else is not classified here. Readers are pure and return
only bounded, secret-free summaries. See docs/PROTOCOLS.md "Provider failure of a head's turn".
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ummanu.runtime.redact import scrub_secrets

#: `quota` = the subscription's usage limit is spent until a reset (ummanu-108), `auth` = 401/403,
#: `rate_limit` = 429, `server` = 5xx, `reconnect` = client gave up reconnecting.
KIND_QUOTA = "quota"
KIND_AUTH = "auth"
KIND_RATE_LIMIT = "rate_limit"
KIND_SERVER = "server"
KIND_RECONNECT = "reconnect"

SUMMARY_LIMIT = 200

# A status is read only where the text labels it one: a request id or port must not become a 503.
_STATUS_PATTERNS = (
    re.compile(r"unexpected status\s+(\d{3})\b", re.IGNORECASE),
    re.compile(r"\bapi error:?\s*(\d{3})\b", re.IGNORECASE),
    re.compile(r"\blast status:?\s*(\d{3})\b", re.IGNORECASE),
    re.compile(r"\b(?:http[_ ]?)?status(?:[_ ]code)?\s*[=:]\s*(\d{3})\b", re.IGNORECASE),
    re.compile(
        r"\b(\d{3})\s+(?:unauthorized|forbidden|too many requests|internal server error|bad gateway|"
        r"service unavailable|gateway time-?out|overloaded)\b",
        re.IGNORECASE,
    ),
)
_RECONNECT_RE = re.compile(r"reconnecting\.{2,3}\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
# Printed once a client's retries are spent; only read on a message that ended a turn or probe.
_GAVE_UP_MARKERS = (
    "exceeded retry limit",
    "stream disconnected before completion",
    "error sending request for url",
)
# A spent subscription, in each provider's own words. Codex: "You've hit your usage limit. ... try
# again at Oct 9th, 2026 9:11 PM." (and `codex_error_info: usage_limit_exceeded`); Claude Code:
# "You've hit your weekly limit · resets 1am (UTC)", "5-hour limit reached ∙ resets 3pm", the older
# "Claude AI usage limit reached|<epoch>". Read before the status: Claude sends these as a 429, and
# a 429 that names a reset is a spent quota, not a burst to retry through.
_QUOTA_MARKERS = (
    "usage limit",
    "usage_limit_exceeded",
    "insufficient_quota",
    "quota exceeded",
    "hit your limit",
    "weekly limit",
    "out of credits",
)
# "hit your weekly limit", "5-hour limit reached", "session limit reached"; not a bare "rate limit
# reached", which is a burst the client retries through.
_QUOTA_HIT_RE = re.compile(
    r"hit your [a-z0-9 -]{0,24}limit|(?:usage|weekly|daily|monthly|session|opus|\d+-hour) limit reached",
    re.IGNORECASE,
)
#: Codex's typed `codex_error_info` values that mean the subscription is spent.
CODEX_QUOTA_ERROR_INFOS = frozenset({"usage_limit_exceeded", "insufficient_quota", "quota_exceeded"})
# Claude Code's own words for an account the API refused, on a line that carries no status code.
_CLAUDE_AUTH_MARKERS = (
    "invalid api key",
    "please run /login",
    "oauth token has expired",
    "oauth token revoked",
)
# Claude Code's typed `error` field on an API error record.
_CLAUDE_ERROR_KINDS = {
    "authentication_failed": KIND_AUTH,
    "permission_error": KIND_AUTH,
    "rate_limit": KIND_RATE_LIMIT,
    "rate_limit_error": KIND_RATE_LIMIT,
    "server_error": KIND_SERVER,
    "overloaded": KIND_SERVER,
    "overloaded_error": KIND_SERVER,
    "api_error": KIND_SERVER,
}
# Masked/partial keys, and the url/ray/request-id tail Codex appends.
_KEY_RE = re.compile(r"\b(?:sk|rk|pk|sess)-[A-Za-z0-9*._\-]{3,}|\S*\*{4,}\S*")
_TAIL_RE = re.compile(r"[,;]?\s*(?:url|cf-ray|request[ _-]?id|x-request-id)\s*[:=].*$", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True)
class ProviderError:
    """One classified provider refusal: its kind, its status when it had one, and a safe summary.

    `reset_at` is the epoch the provider itself names for the end of the refusal ("try again at",
    "resets"), 0.0 when it names none.
    """

    kind: str
    status: int | None
    summary: str
    at: float = 0.0
    source: str = ""
    reset_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "at": self.at,
            "source": self.source,
            "reset_at": self.reset_at,
        }

    def stamped(self, at: float, source: str) -> ProviderError:
        """The same error, dated and attributed; a relative reset ("in 3 days") counts from `at`."""
        reset_at = self.reset_at
        if at and self.summary:
            reset_at = reset_time(self.summary, now=at) or reset_at
        return ProviderError(self.kind, self.status, self.summary, at, source, reset_at)


def is_quota_text(text: str) -> bool:
    """Whether a provider message says the subscription's usage limit is spent."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS) or bool(_QUOTA_HIT_RE.search(text or ""))


_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}
# Codex: "try again at Oct 9th, 2026 9:11 PM", "try again at 9:11 PM".
_CODEX_AT_RE = re.compile(
    r"try again at\s+(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(?:(\d{4}),?\s+)?)?"
    r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?",
)
# "try again in 3 days", "in 2 hours 5 minutes".
_RELATIVE_RE = re.compile(r"try again in\s+((?:\d+\s*(?:day|hour|hr|minute|min|second|sec)s?[\s,and]*)+)", re.IGNORECASE)
_RELATIVE_PART_RE = re.compile(r"(\d+)\s*(day|hour|hr|minute|min|second|sec)", re.IGNORECASE)
# Claude Code: "resets 1am (UTC)", "resets 3:30pm (Europe/London)", "resets Oct 9, 1am (UTC)",
# "resets Oct 9 at 1am".
_CLAUDE_RESETS_RE = re.compile(
    r"(?:resets|reset at|continuing automatically at)\s+(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?"
    r"(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])\s*(?:\(([^)]+)\))?",
)
# Claude Code before 2.x: "Claude AI usage limit reached|1759712400".
_EPOCH_RE = re.compile(r"limit reached\|(\d{10})\b", re.IGNORECASE)


def _hour(hour: int, meridiem: str | None) -> int:
    if not meridiem:
        return hour
    hour %= 12
    return hour + 12 if meridiem.lower() == "pm" else hour


def _zone(name: str | None) -> Any:
    if not name:
        return None
    if name.strip().upper() in ("UTC", "GMT", "Z"):
        return UTC
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _next_at(now: float, zone: Any, month: int | None, day: int | None, year: int | None,
             hour: int, minute: int) -> float:
    """The first moment at or after `now` matching the named wall time, in `zone` (local when None)."""
    current = datetime.fromtimestamp(now, zone) if zone is not None else datetime.fromtimestamp(now)
    try:
        candidate = current.replace(
            year=year or current.year,
            month=month or current.month,
            day=day or current.day,
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
    except ValueError:
        return 0.0
    if candidate.timestamp() < now - 60:
        if month is None:
            candidate += timedelta(days=1)
        elif year is None:
            try:
                candidate = candidate.replace(year=candidate.year + 1)
            except ValueError:
                return 0.0
    return candidate.timestamp()


def reset_time(text: str, *, now: float | None = None) -> float:
    """The epoch at which the provider says the refusal ends, or 0.0 when it names no time.

    A wall time with no zone is read in this host's local zone, the zone the CLI printed it in.
    """
    reference = time.time() if now is None else now
    text = str(text or "")
    match = _EPOCH_RE.search(text)
    if match:
        return float(match.group(1))
    match = _CODEX_AT_RE.search(text)
    if match:
        month = _MONTHS.get((match.group(1) or "").lower()[:3]) if match.group(1) else None
        day = int(match.group(2)) if match.group(2) else None
        year = int(match.group(3)) if match.group(3) else None
        return _next_at(reference, None, month, day, year,
                        _hour(int(match.group(4)), match.group(6)), int(match.group(5)))
    match = _CLAUDE_RESETS_RE.search(text)
    if match:
        month = _MONTHS.get((match.group(1) or "").lower()[:3]) if match.group(1) else None
        day = int(match.group(2)) if match.group(2) else None
        return _next_at(reference, _zone(match.group(6)), month, day, None,
                        _hour(int(match.group(3)), match.group(5)), int(match.group(4) or 0))
    match = _RELATIVE_RE.search(text)
    if match:
        units = {"day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
        seconds = sum(int(count) * units[unit.lower()] for count, unit in _RELATIVE_PART_RE.findall(match.group(1)))
        return reference + seconds if seconds else 0.0
    return 0.0


def status_code(text: str) -> int | None:
    """The HTTP status a provider message names, or None when it names none."""
    for pattern in _STATUS_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return int(match.group(1))
    return None


def reconnect_exhausted(text: str) -> bool:
    """Whether the text shows a client's reconnect loop running out ("Reconnecting... 5/5")."""
    return any(int(done) >= int(total) > 0 for done, total in _RECONNECT_RE.findall(text or ""))


def kind_of_status(status: int | None) -> str:
    """The provider-error kind a status code names, or "" for one outside this module's scope."""
    if status in (401, 403):
        return KIND_AUTH
    if status == 429:
        return KIND_RATE_LIMIT
    if status is not None and 500 <= status <= 599:
        return KIND_SERVER
    return ""


def classify_provider_error(text: str, *, status: int | None = None) -> ProviderError | None:
    """Classify one provider message, or None when out of scope.

    `status` is a caller-held status (Claude's `apiErrorStatus`), else read from the text. A status
    wins over reconnect wording (reconnected five times, then 401, is a 401).
    """
    code = status if status is not None else status_code(text)
    if is_quota_text(text):
        # The line that says so, not the first line: a CLI's banner comes before its error.
        line = next((line for line in str(text).splitlines() if is_quota_text(line)), text)
        return ProviderError(
            KIND_QUOTA, code if kind_of_status(code) else None, summarize_provider_error(line),
            reset_at=reset_time(text),
        )
    kind = kind_of_status(code)
    if not kind:
        lowered = (text or "").lower()
        if reconnect_exhausted(text) or any(marker in lowered for marker in _GAVE_UP_MARKERS):
            kind = KIND_RECONNECT
        elif any(marker in lowered for marker in _CLAUDE_AUTH_MARKERS):
            kind = KIND_AUTH
    if not kind:
        return None
    return ProviderError(kind, code if kind_of_status(code) else None, summarize_provider_error(text))


def summarize_provider_error(text: str, *, limit: int = SUMMARY_LIMIT) -> str:
    """The first meaningful line of a provider message, bounded and secret-free.

    The echoed key and the url/ray/request-id tail are removed before the usual scrub.
    """
    lines = [_ANSI_RE.sub("", line).strip() for line in str(text or "").splitlines()]
    line = next((line for line in lines if line), "")
    line = _TAIL_RE.sub("", line)
    line = _KEY_RE.sub("[key]", line)
    line = scrub_secrets(line)
    line = " ".join(line.split())
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line


def _epoch(value: Any) -> float:
    """A record's timestamp as epoch seconds, 0.0 when it carries none a reader may believe."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _codex_view(event: Any) -> Mapping[str, Any]:
    """The provider event inside Codex's persisted `event_msg` envelope, or the event itself."""
    if not isinstance(event, Mapping):
        return {}
    payload = event.get("payload")
    if event.get("type") == "event_msg" and isinstance(payload, Mapping):
        return payload
    return event


def _codex_error_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("message") or "")
    return str(value or "")


def _codex_turn_error(value: Any) -> ProviderError | None:
    """The provider error one `task_complete.error` (or `error` event) carries, or None.

    Codex's typed `codex_error_info` names a spent subscription even where its words change.
    """
    text = _codex_error_text(value)
    info = str(value.get("codex_error_info") or "") if isinstance(value, Mapping) else ""
    found = classify_provider_error(text) if text else None
    if info in CODEX_QUOTA_ERROR_INFOS and (found is None or found.kind != KIND_QUOTA):
        return ProviderError(
            KIND_QUOTA, None, summarize_provider_error(text or info), reset_at=reset_time(text)
        )
    return found


def _codex_turns(events: Iterable[Any]) -> list[tuple[ProviderError | None, bool]]:
    """Each completed turn of a Codex rollout, in order: the provider error it ended on, and clean."""
    turns: list[tuple[ProviderError | None, bool]] = []
    pending: ProviderError | None = None
    for event in events:
        view = _codex_view(event)
        kind = str(view.get("type") or "")
        stamp = _epoch(event.get("timestamp") if isinstance(event, Mapping) else None)
        if kind == "task_started":
            pending = None
        elif kind in ("error", "stream_error"):
            found = _codex_turn_error(view.get("error") or view.get("message"))
            if found is not None and kind == "error":
                pending = found.stamped(stamp, "codex-rollout")
        elif kind == "task_complete":
            raw_error = view.get("error")
            if _codex_error_text(raw_error) or (
                isinstance(raw_error, Mapping) and raw_error.get("codex_error_info")
            ):
                found = _codex_turn_error(raw_error)
                turns.append((found.stamped(stamp, "codex-rollout") if found is not None else None, False))
            elif pending is not None and not view.get("last_agent_message"):
                turns.append((pending.stamped(stamp, pending.source), False))
            else:
                turns.append((None, True))
            pending = None
    return turns


def codex_first_turn_failure(events: Iterable[Any]) -> ProviderError | None:
    """The provider error a Codex session's first turn ended on, or None.

    Read from the rollout journal: a refused turn ends with a `task_complete` carrying
    `error.message`; older journals emit a separate `error` event and close the turn with no agent
    message. Answers the last completed turn's error only while no earlier completed turn was clean;
    an open turn answers nothing.
    """
    turns = _codex_turns(events)
    if not turns or turns[-1][0] is None:
        return None
    if any(clean for _, clean in turns[:-1]):
        return None
    return turns[-1][0]


def codex_turn_failure(events: Iterable[Any]) -> ProviderError | None:
    """The provider error a Codex session's last completed turn ended on, whichever turn it was.

    The mid-run reading (ummanu-108): a head that worked for an hour and then ran into its usage
    limit is as refused as one refused on its first turn. A later clean turn ends it; an open turn
    after the failed one answers nothing.
    """
    items = list(events)
    if _codex_turn_open(items):
        return None
    turns = _codex_turns(items)
    return turns[-1][0] if turns else None


def _codex_turn_open(events: list[Any]) -> bool:
    """Whether the rollout's last turn started and has not completed."""
    for event in reversed(events):
        kind = str(_codex_view(event).get("type") or "")
        if kind == "task_complete":
            return False
        if kind == "task_started":
            return True
    return False


def _claude_text(record: Mapping[str, Any]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def claude_api_error(record: Mapping[str, Any]) -> ProviderError | None:
    """The provider error one Claude Code session record carries, or None.

    Such a record is an assistant record with `isApiErrorMessage`, `apiErrorStatus` and a typed
    `error`, written after Claude Code's own retries are spent.
    """
    if record.get("type") != "assistant" or record.get("isApiErrorMessage") is not True:
        return None
    text = _claude_text(record)
    raw_status = record.get("apiErrorStatus")
    status = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
    found = classify_provider_error(text, status=status) if (status or text) else None
    if found is None:
        kind = _CLAUDE_ERROR_KINDS.get(str(record.get("error") or ""))
        if kind is None:
            return None
        found = ProviderError(kind, None, summarize_provider_error(text or str(record.get("error"))))
    return found.stamped(_epoch(record.get("timestamp")), "claude-session")


def _claude_clean_turn_end(record: Mapping[str, Any]) -> bool:
    message = record.get("message")
    return (
        record.get("type") == "assistant"
        and record.get("isApiErrorMessage") is not True
        and isinstance(message, Mapping)
        and message.get("stop_reason") == "end_turn"
    )


def claude_first_turn_failure(records: Iterable[Any]) -> ProviderError | None:
    """The provider error a Claude Code session's first turn ended on, or None.

    The last user/assistant record must be an API error record, and no earlier assistant record may
    have ended a turn cleanly (`stop_reason: end_turn`).
    """
    last: Mapping[str, Any] | None = None
    clean_before = False
    for record in records:
        if not isinstance(record, Mapping) or record.get("type") not in ("user", "assistant"):
            continue
        if last is not None and _claude_clean_turn_end(last):
            clean_before = True
        last = record
    if last is None or clean_before:
        return None
    return claude_api_error(last)


def claude_turn_failure(records: Iterable[Any]) -> ProviderError | None:
    """The provider error a Claude Code session's last turn ended on, whichever turn it was.

    The mid-run reading (ummanu-108): the last user/assistant record is an API error record. A new
    prompt or a clean answer after it ends it.
    """
    last: Mapping[str, Any] | None = None
    for record in records:
        if isinstance(record, Mapping) and record.get("type") in ("user", "assistant"):
            last = record
    return claude_api_error(last) if last is not None else None


#: How many bottom screen lines may hold the error line the turn ended on (error, blank, prompt box).
SCREEN_ERROR_WINDOW = 12


def screen_turn_failure(lines: Iterable[str]) -> ProviderError | None:
    """The provider error the bottom of a Claude head's screen shows, or None.

    Last resort when no session record is readable. Only lines just above the prompt in Claude
    Code's own error shape count, so earlier output is not mistaken for the turn's end.
    """
    visible = [line.strip() for line in lines if line.strip()]
    for line in reversed(visible[-SCREEN_ERROR_WINDOW:]):
        lowered = line.lower()
        quota = is_quota_text(line)
        if "api error" not in lowered and not quota and not any(
            marker in lowered for marker in _CLAUDE_AUTH_MARKERS
        ):
            continue
        text = line[lowered.find("api error") :] if "api error" in lowered else line.lstrip("⎿ ").strip()
        found = classify_provider_error(text)
        if found is not None:
            return found.stamped(time.time(), "pty-screen")
    return None
