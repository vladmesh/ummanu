"""Cheap real provider probes named by the shipped head registry as `resources.*.probe`.

`python3 -P -m ummanu.runtime.resource_probe --resource <id>` makes one provider call for
`claude-sub`, `openai-sub` or `openrouter`: exit 0 answered, 1 failed (one scrubbed, capped
`resource <id> probe failed; ...` stderr line), 2 unknown id. No cache, no files; `ummanu.head_health`
owns the verdict and TTL cache. A probe that cannot run is a failure, never an exception.
`head_health` reads `status=timeout`, `status=provider-unavailable` (provider failed while the
local login is valid) and `status=exhausted` (the subscription's usage limit is spent; the line
then carries `provider_error=` with the provider's own words and reset time) by name.

Timeouts are per resource: 75 s for `openai-sub` (Codex refuses slowly), 20 s otherwise;
`TA_PROBE_TIMEOUT_S` moves the default, `TA_PROBE_TIMEOUT_S_<RESOURCE>` sets one resource.
`head_health` derives its outer timeout from the same number so this classifier answers first.
See docs/PROTOCOLS.md "Resource probe statuses".
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ummanu.runtime.codex_home import installation_codex_home
from ummanu.runtime.codex_preflight import CodexHomeLoginMissing
from ummanu.runtime.provider_errors import (
    KIND_AUTH,
    KIND_QUOTA,
    KIND_RECONNECT,
    KIND_SERVER,
    classify_provider_error,
)
from ummanu.runtime.redact import redact

# Kills a slow or broken probe instead of hanging the dispatcher tick; env-overridable.
PROBE_TIMEOUT_S = int(os.environ.get("TA_PROBE_TIMEOUT_S", "20"))
PROBE_REASON_TEXT_LIMIT = int(os.environ.get("TA_PROBE_REASON_TEXT_LIMIT", "400"))
#: The default inner timeout of a resource with no entry below.
DEFAULT_PROBE_TIMEOUT_S = 20
#: Resources whose provider answers more slowly than the default, and how long they get.
RESOURCE_PROBE_TIMEOUTS_S: dict[str, int] = {"openai-sub": 75}
#: Inner failure status: provider answered with its own failure (see the module docstring).
STATUS_PROVIDER_UNAVAILABLE = "provider-unavailable"
# The subscription's usage limit is spent (ummanu-108). Read off the whole output before it is
# capped: Codex prints its banner first, so the usage-limit line sits past the reason's limit.
STATUS_EXHAUSTED = "exhausted"


def _env_seconds(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if value > 0 else None


def probe_timeout_env_name(resource_id: str) -> str:
    """The environment variable that sets one resource's probe timeout."""
    return "TA_PROBE_TIMEOUT_S_" + "".join(c if c.isalnum() else "_" for c in resource_id.upper())


def probe_timeout_s(resource_id: str) -> int:
    """This resource's probe timeout, read from the environment per call.

    Per-resource variable, then the resource's default, then `TA_PROBE_TIMEOUT_S`, then 20 s. Read
    per call so the dispatcher and the probe it spawns cannot disagree.
    """
    specific = _env_seconds(probe_timeout_env_name(resource_id))
    if specific is not None:
        return specific
    if resource_id in RESOURCE_PROBE_TIMEOUTS_S:
        return RESOURCE_PROBE_TIMEOUTS_S[resource_id]
    return _env_seconds("TA_PROBE_TIMEOUT_S") or DEFAULT_PROBE_TIMEOUT_S


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    probe_class: str
    command: str | None = None
    status: str = "ok"
    exit_code: int | None = None
    timeout_s: float | None = None
    http_status: int | None = None
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None
    exception: str | BaseException | None = None


def _clean_summary(value: object) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    text = " ".join(redact(text).strip().split())
    if not text:
        return None
    if len(text) > PROBE_REASON_TEXT_LIMIT:
        return text[:PROBE_REASON_TEXT_LIMIT] + "...[truncated]"
    return text


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _display_command(command: str | list[str]) -> str:
    return command if isinstance(command, str) else shlex.join(command)


def _run_subprocess_probe(
    command: list[str],
    probe_class: str,
    *,
    env: Mapping[str, str] | None = None,
    display_command: str | None = None,
    timeout_s: int | None = None,
) -> ProbeResult:
    shown = display_command or _display_command(command)
    timeout = timeout_s or PROBE_TIMEOUT_S
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)  # noqa: PLW1510
    except subprocess.TimeoutExpired as e:
        return ProbeResult(
            False,
            probe_class,
            command=shown,
            status="timeout",
            timeout_s=float(e.timeout or timeout),
            stdout=e.output,
            stderr=e.stderr,
            exception=_exception_text(e),
        )
    except OSError as e:
        return ProbeResult(
            False, probe_class, command=shown, status="exception", exception=_exception_text(e)
        )
    if p.returncode == 0:
        return ProbeResult(True, probe_class, command=shown)
    return ProbeResult(
        False,
        probe_class,
        command=shown,
        status="non-zero-exit",
        exit_code=p.returncode,
        stdout=p.stdout,
        stderr=p.stderr,
    )


def probe_failure_reason(resource_id: str, result: ProbeResult) -> dict[str, object]:
    reason: dict[str, object] = {
        "resource": resource_id,
        "probe_class": result.probe_class,
        "status": result.status,
    }
    command = _clean_summary(result.command)
    if command:
        reason["command"] = command
    if result.exit_code is not None:
        reason["exit_code"] = result.exit_code
    if result.timeout_s is not None:
        reason["timeout_s"] = result.timeout_s
    if result.http_status is not None:
        reason["http_status"] = result.http_status
    if result.status == STATUS_EXHAUSTED:
        found = classify_provider_error(_as_text(result.stdout) + "\n" + _as_text(result.stderr))
        if found is not None:
            reason["provider_error"] = found.summary
    for key in ("stderr", "stdout", "exception"):
        summary = _clean_summary(getattr(result, key))
        if summary:
            reason[key] = summary
    return reason


def format_probe_failure(resource_id: str, result: ProbeResult) -> str:
    reason = probe_failure_reason(resource_id, result)
    parts = [
        f"resource {resource_id} probe failed",
        f"class={reason['probe_class']}",
        f"status={reason['status']}",
    ]
    for key in (
        "provider_error", "command", "exit_code", "timeout_s", "http_status", "stderr", "stdout", "exception"
    ):
        if key in reason:
            parts.append(f"{key}={reason[key]}")
    return "; ".join(parts)


_OPENROUTER_ENV_FILE = Path(os.environ.get("TA_OPENROUTER_ENV_FILE", str(Path.home() / ".hermes" / ".env")))
# Key names accepted in _OPENROUTER_ENV_FILE (hermes `OPENROUTER_API_KEY`, legacy `open_router_key`);
# TA_OPENROUTER_ENV_KEY pins a single name.
_OPENROUTER_ENV_KEYS: tuple[str, ...] = (
    (os.environ["TA_OPENROUTER_ENV_KEY"],)
    if os.environ.get("TA_OPENROUTER_ENV_KEY")
    else ("OPENROUTER_API_KEY", "open_router_key")
)


def _read_openrouter_key() -> str | None:
    override = os.environ.get("TA_OPENROUTER_KEY")
    if override:
        return override
    try:
        text = _OPENROUTER_ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() in _OPENROUTER_ENV_KEYS:
            return val.strip().strip('"').strip("'") or None
    return None


def probe_claude_sub() -> ProbeResult:
    """One haiku token through the shared OAuth `claude` CLI: fails when the subscription is
    rate-limited or the API is unreachable."""
    result = _run_subprocess_probe(
        ["claude", "-p", "ping", "--model", "haiku", "--dangerously-skip-permissions"],
        "builtin:claude-sub",
        timeout_s=probe_timeout_s("claude-sub"),
    )
    if result.status == "non-zero-exit" and _quota_spent(_as_text(result.stdout) + "\n" + _as_text(result.stderr)):
        return replace(result, status=STATUS_EXHAUSTED)
    return result


def _http_failure_status(status: int | None) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate-limit"
    return "http-error"


def _read_http_error_body(err: urllib.error.HTTPError) -> bytes | None:
    try:
        return err.read()
    except Exception:  # noqa: BLE001 - a probe that cannot run is a failure, never an exception
        return None


def probe_openrouter() -> ProbeResult:
    """One 1-token completion against OpenRouter: fails on a missing key, non-2xx, timeout or any
    transport error; never raises."""
    command = "POST https://openrouter.ai/api/v1/chat/completions model=google/gemini-2.5-flash max_tokens=1"
    key = _read_openrouter_key()
    if not key:
        return ProbeResult(
            False, "builtin:openrouter", command=command, status="auth", exception="missing OpenRouter key"
        )
    body = json.dumps(
        {
            "model": "google/gemini-2.5-flash",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=probe_timeout_s("openrouter")) as resp:
            status = getattr(resp, "status", None)
            if status is not None and 200 <= status < 300:
                return ProbeResult(True, "builtin:openrouter", command=command)
            return ProbeResult(
                False,
                "builtin:openrouter",
                command=command,
                status=_http_failure_status(status),
                http_status=status,
            )
    except urllib.error.HTTPError as e:
        return ProbeResult(
            False,
            "builtin:openrouter",
            command=command,
            status=_http_failure_status(e.code),
            http_status=e.code,
            stderr=_read_http_error_body(e),
            exception=_exception_text(e),
        )
    except TimeoutError as e:
        return ProbeResult(
            False,
            "builtin:openrouter",
            command=command,
            status="timeout",
            timeout_s=float(probe_timeout_s("openrouter")),
            exception=_exception_text(e),
        )
    except Exception as e:  # noqa: BLE001 — any transport outcome is just a failed probe
        return ProbeResult(
            False,
            "builtin:openrouter",
            command=command,
            status="transport-error",
            exception=_exception_text(e),
        )


def probe_openai_sub() -> ProbeResult:
    """One read-only `codex exec` "ping" through the ChatGPT-authed CODEX_HOME.

    CODEX_HOME is set explicitly because a plain subprocess does not inherit it.
    """
    try:
        home = installation_codex_home().path
    except CodexHomeLoginMissing as e:
        # No login is a failed probe whose message names the fix.
        return ProbeResult(False, "builtin:openai-sub", status="no-login", exception=_exception_text(e))
    env = {**os.environ, "CODEX_HOME": home}
    cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "ping"]
    result = _run_subprocess_probe(
        cmd,
        "builtin:openai-sub",
        env=env,
        display_command=f"CODEX_HOME={home} {_display_command(cmd)}",
        timeout_s=probe_timeout_s("openai-sub"),
    )
    text = _as_text(result.stdout) + "\n" + _as_text(result.stderr)
    if result.status == "non-zero-exit" and _quota_spent(text):
        return replace(result, status=STATUS_EXHAUSTED)
    if result.status == "non-zero-exit" and codex_provider_side_failure(text, Path(home)):
        return replace(result, status=STATUS_PROVIDER_UNAVAILABLE)
    return result


def _quota_spent(text: str) -> bool:
    found = classify_provider_error(text)
    return found is not None and found.kind == KIND_QUOTA


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def codex_chatgpt_login(home: Path) -> bool:
    """Whether CODEX_HOME holds a ChatGPT-mode login; only the mode is read, no token leaves."""
    try:
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    return (
        isinstance(auth, dict)
        and str(auth.get("auth_mode") or "").lower() == "chatgpt"
        and bool(auth.get("tokens"))
    )


def codex_provider_side_failure(text: str, home: Path) -> bool:
    """Whether a failed `codex exec` was refused by the provider rather than for the account.

    5xx and exhausted reconnects are always the provider's. A 401/403 "Incorrect API key provided"
    is the provider's only under a valid ChatGPT login (a backend fault re-login cannot fix);
    otherwise it is the account's (`unauthenticated`).
    """
    found = classify_provider_error(text)
    if found is None:
        return False
    if found.kind in (KIND_SERVER, KIND_RECONNECT):
        return True
    if found.kind == KIND_AUTH:
        return codex_chatgpt_login(home) and "incorrect api key provided" in text.lower()
    return False


BUILTIN_PROBES: dict[str, Callable[[], ProbeResult]] = {
    "claude-sub": probe_claude_sub,
    "openrouter": probe_openrouter,
    "openai-sub": probe_openai_sub,
}


def main(argv: list[str] | None = None) -> int:
    """Run the named built-in resource probe: 0 healthy, 1 failed, 2 no such probe."""
    parser = argparse.ArgumentParser(prog="ummanu.runtime.resource_probe")
    parser.add_argument("--resource", required=True)
    args = parser.parse_args(argv)
    probe = BUILTIN_PROBES.get(args.resource)
    if probe is None:
        print(
            f"health probe: no builtin probe for {args.resource!r} (known: {', '.join(sorted(BUILTIN_PROBES))})",
            file=sys.stderr,
        )
        return 2
    result = probe()
    if not result.ok:
        print(format_probe_failure(args.resource, result), file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
