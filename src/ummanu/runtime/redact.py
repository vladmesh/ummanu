"""Secret redaction before a transcript reaches the model, canon or a board card.

`redact` layers: (1) exact values from known .env files, (2) regexes for well-known key shapes.
`scrub_secrets` adds, for board text, secret-named KEY=value assignments and long token-like blobs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path

from ummanu.runtime.paths import default_instance_path
from ummanu.runtime.role_env import is_sensitive_env_name
from ummanu.runtime_env import RuntimeEnvError, parse_env_value

# .env files whose secret-named values are scrubbed verbatim.
DEFAULT_ENV_FILES = [
    Path.home() / ".hermes" / ".env",
    default_instance_path() / "runtime.env",
]

# Set by the head sandbox launcher on its confined child (`ummanu.runtime.head.sandbox`). Inside it the
# kernel refuses the secret store and the env files; a value the process cannot read cannot reach its
# text either, so a refused input is skipped and the process's own environment is scrubbed instead.
HEAD_SANDBOX_ENV = "UMMANU_HEAD_SANDBOX"


def in_head_sandbox(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process runs inside the head sandbox, where refused redaction inputs are expected."""
    return (os.environ if environ is None else environ).get(HEAD_SANDBOX_ENV) == "1"


def secret_store_refused(values_dir: Path) -> bool:
    """Whether this sandboxed process is refused the secret store (`values/` or the `secrets/` above it).

    Only then may board redaction go without the store; outside the sandbox, or when the store can be
    listed, it is read as always and any failure stays an error.
    """
    if not in_head_sandbox():
        return False
    for directory in (values_dir.parent, values_dir):
        try:
            os.listdir(directory)
        except PermissionError:
            return True
        except OSError:
            continue
    return False


def environment_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """The sensitive-named values of a process environment: what a confined head can know."""
    source = os.environ if environ is None else environ
    return [
        value
        for name, value in source.items()
        if is_sensitive_env_name(name) and len(value) >= MIN_ENV_VALUE_LEN
    ]


# Shorter values ("true", "8077") are config and would over-redact.
MIN_ENV_VALUE_LEN = 12

REDACTED = "«REDACTED»"

# A key cannot start inside a word: `task-<hex>` must not read as `sk-<hex>`.
_KEY_START = r"(?<![A-Za-z0-9_-])"

# Most specific first.
PATTERNS = [
    (re.compile(_KEY_START + r"AGE-SECRET-KEY-1[0-9A-Z]{50,}"), "age-secret-key"),
    (re.compile(_KEY_START + r"sk-ant-[A-Za-z0-9_-]{20,}"), "anthropic-key"),
    (re.compile(_KEY_START + r"sk-or-v1-[A-Za-z0-9]{20,}"), "openrouter-key"),
    (re.compile(_KEY_START + r"sk-proj-[A-Za-z0-9_-]{20,}"), "openai-project-key"),
    (re.compile(_KEY_START + r"sk-[A-Za-z0-9]{32,}"), "openai-key"),
    (re.compile(_KEY_START + r"gh[pousr]_[A-Za-z0-9]{36,}"), "github-token"),
    (re.compile(_KEY_START + r"github_pat_[A-Za-z0-9_]{20,}"), "github-pat"),
    (re.compile(_KEY_START + r"xox[baprs]-[A-Za-z0-9-]{10,}"), "slack-token"),
    (
        re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"),
        "slack-webhook",
    ),
    (re.compile(_KEY_START + r"AKIA[0-9A-Z]{16}"), "aws-access-key-id"),
    (re.compile(_KEY_START + r"AIza[0-9A-Za-z_-]{35}"), "google-api-key"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"), "bearer-token"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        "private-key-block",
    ),
]

# `runtime.env` is configuration, not a secret list: exact-value redaction keys on sensitive
# variable names, so long config values (a board URL) are not masked. Exception: a URL with
# userinfo may carry a password whatever its name. PATTERNS remain the backstop.
_URL_WITH_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s@]+@")


def looks_like_credential(value: str) -> bool:
    """Whether plaintext itself has a known credential or credential-URL shape."""
    return bool(_URL_WITH_USERINFO_RE.match(value)) or any(
        pattern.search(value) for pattern, _label in PATTERNS
    )


def _load_env_values(env_files: Iterable[Path | str]) -> list[str]:
    values: list[str] = []
    for path in env_files:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except PermissionError:
            if in_head_sandbox():
                continue
            raise
        for raw in data.decode("utf-8", errors="replace").split("\n"):
            line = raw.removesuffix("\r").lstrip(" \t")
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            name, _, val = line.partition("=")
            name = name.strip()
            serialized = val.strip()
            # Keep the old conservative matches even for malformed operator files;
            # supported values also contribute exactly what the process receives.
            forms = {serialized, serialized.strip('"').strip("'")}
            with suppress(RuntimeEnvError):
                forms.add(parse_env_value(val))
            if is_sensitive_env_name(name) or any(_URL_WITH_USERINFO_RE.match(value) for value in forms):
                values.extend(value for value in forms if len(value) >= MIN_ENV_VALUE_LEN)
    # Longest first so a value that contains another gets scrubbed whole.
    return sorted(set(values), key=len, reverse=True)


def redact(
    text: str,
    env_files: Iterable[Path | str] | None = None,
    secret_values: Iterable[object] | None = None,
) -> str:
    """Return `text` with known secrets replaced by a labeled placeholder."""
    if not text:
        return text
    files = [*DEFAULT_ENV_FILES, *(env_files or ())]
    values = [*(_load_env_values(files)), *(secret_values or ())]
    if in_head_sandbox():
        values.extend(environment_values())
        # The board credentials stay readable to a sandboxed head (it reaches the board with them).
        instance = os.environ.get("UMMANU_INSTANCE", "").strip()
        if instance:
            values.extend(_load_env_values([Path(instance) / "board-store.env"]))
    for val in sorted(
        {str(value) for value in values if len(str(value)) >= MIN_ENV_VALUE_LEN}, key=len, reverse=True
    ):
        if val in text:
            text = text.replace(val, f"{REDACTED}:env-value")
    for pat, label in PATTERNS:
        text = pat.sub(f"{REDACTED}:{label}", text)
    return text


# Board-comment layer on top of `redact`: secret-named KEY=value assignments and long
# base64/hex-like blobs (no `/`, so paths survive). An upstream redaction marker is kept verbatim
# so a repeated scrub does not rewrite it.
_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)"
    r"\s*=\s*(?!<redacted>|«REDACTED»)(\S+)"
)
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+=_-]{40,}\b")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _is_git_sha(blob: str) -> bool:
    """A full or abbreviated git sha (plain hex), spared from blob masking."""
    return bool(_HEX_RE.match(blob))


def scrub_secrets(
    text: str,
    env_files: Iterable[Path | str] | None = None,
    secret_values: Iterable[object] | None = None,
) -> str:
    """Mask secret-looking material in `text` before it reaches a board comment (git shas spared)."""
    if not text:
        return text
    text = redact(text, env_files=env_files, secret_values=secret_values)
    text = _ASSIGN_RE.sub(rf"\1={REDACTED}", text)
    return _BLOB_RE.sub(lambda m: m.group(0) if _is_git_sha(m.group(0)) else f"{REDACTED}:blob", text)


if __name__ == "__main__":
    sample = (
        "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX and "
        "AGE-SECRET-KEY-1QQPQRSTUVWXYZ0123456789QQPQRSTUVWXYZ0123456789QQPQ "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
    )
    print(redact(sample))
