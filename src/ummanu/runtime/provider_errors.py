"""Classify provider refusals of a head turn or resource probe, without secrets.

Shared by the resource probe (`ummanu.head_health`, `resource_probe`) and the dispatcher's
first-turn check (`dispatch.provider_failure`). In scope: HTTP 401/403, 429, 5xx, and a connection
the client gave up on after its own retries; anything else is not classified here. Readers are
pure and return only bounded, secret-free summaries. See docs/PROTOCOLS.md "Provider failure on a
head's first turn".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ummanu.runtime.redact import scrub_secrets

#: `auth` = 401/403, `rate_limit` = 429, `server` = 5xx, `reconnect` = client gave up reconnecting.
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
    """One classified provider refusal: its kind, its status when it had one, and a safe summary."""

    kind: str
    status: int | None
    summary: str
    at: float = 0.0
    source: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "at": self.at,
            "source": self.source,
        }


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


def codex_first_turn_failure(events: Iterable[Any]) -> ProviderError | None:
    """The provider error a Codex session's first turn ended on, or None.

    Read from the rollout journal: a refused turn ends with a `task_complete` carrying
    `error.message`; older journals emit a separate `error` event and close the turn with no agent
    message. Answers the last completed turn's error only while no earlier completed turn was clean;
    an open turn answers nothing.
    """
    turns: list[tuple[ProviderError | None, bool]] = []
    pending: ProviderError | None = None
    for event in events:
        view = _codex_view(event)
        kind = str(view.get("type") or "")
        stamp = _epoch(event.get("timestamp") if isinstance(event, Mapping) else None)
        if kind == "task_started":
            pending = None
        elif kind in ("error", "stream_error"):
            found = classify_provider_error(_codex_error_text(view.get("message") or view.get("error")))
            if found is not None and kind == "error":
                pending = ProviderError(found.kind, found.status, found.summary, stamp, "codex-rollout")
        elif kind == "task_complete":
            error_text = _codex_error_text(view.get("error"))
            if error_text:
                found = classify_provider_error(error_text)
                failure = (
                    ProviderError(found.kind, found.status, found.summary, stamp, "codex-rollout")
                    if found is not None
                    else None
                )
                turns.append((failure, False))
            elif pending is not None and not view.get("last_agent_message"):
                turns.append(
                    (
                        ProviderError(pending.kind, pending.status, pending.summary, stamp, pending.source),
                        False,
                    )
                )
            else:
                turns.append((None, True))
            pending = None
    if not turns:
        return None
    last_failure, _ = turns[-1]
    if last_failure is None:
        return None
    if any(clean for _, clean in turns[:-1]):
        return None
    return last_failure


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
    return ProviderError(
        found.kind, found.status, found.summary, _epoch(record.get("timestamp")), "claude-session"
    )


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
        if "api error" not in lowered and not any(marker in lowered for marker in _CLAUDE_AUTH_MARKERS):
            continue
        text = line[lowered.find("api error") :] if "api error" in lowered else line.lstrip("⎿ ").strip()
        found = classify_provider_error(text)
        if found is not None:
            return ProviderError(found.kind, found.status, found.summary, 0.0, "pty-screen")
    return None
