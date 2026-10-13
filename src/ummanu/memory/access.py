"""Launch-bound read authority for the Memory MCP service.

The bearer token is only an opaque lookup capability.  The authority it represents
is recovered by the service from a grant written at launch, then checked against
the head's versioned heartbeat.  Nothing supplied in a tool call can select a role,
subject or scope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.runtime.head import HeadRun, HeadRunError, TaskRefError
from ummanu.runtime.head.identity import head_process_status

MEMORY_ACCESS_BINDINGS_ENV = "MEMORY_ACCESS_BINDINGS"
MEMORY_ACCESS_TOKEN_ENV = "UMMANU_MEMORY_ACCESS_TOKEN"
GRANT_VERSION = 1
DEFAULT_GRANT_TTL_SECONDS = 12 * 60 * 60
PRODUCT_UMMANU_SCOPE = "product:ummanu"
PROJECT_UMMANU_SCOPE = "project:ummanu"
PO_REVIEW_SCOPE = "review:po"
PO_REVIEW_SCOPE_DIR = "po-review"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")


class MemoryAccessError(ValueError):
    """A grant cannot establish a usable memory-read identity."""


@dataclass(frozen=True)
class MemoryReadIdentity:
    role: str
    subject: dict[str, Any]
    scopes: frozenset[str] | None
    grant_id: str

    def audit_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "subject": dict(self.subject),
            "scopes": sorted(self.scopes) if self.scopes is not None else ["installation-wide"],
        }


@dataclass(frozen=True)
class MemoryAccessGrant:
    grant_id: str
    token: str
    identity: MemoryReadIdentity

    @property
    def launch_identity(self) -> dict[str, str]:
        """The only launch-time value a client needs to send the bearer token."""
        return {MEMORY_ACCESS_TOKEN_ENV: self.token}


@dataclass(frozen=True)
class MemoryAccessDenial:
    code: str
    identity: MemoryReadIdentity | None = None

    def response(self) -> dict[str, str]:
        # Do not disclose grant paths, facts, requested scopes, or bearer material.
        return {"status": "denied", "error": self.code}


def bindings_dir(data_dir: str | Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / "memory" / "access-grants"
    configured = os.environ.get(MEMORY_ACCESS_BINDINGS_ENV)
    if configured:
        return Path(configured)
    base = Path(os.environ.get("UMMANU_DATA_DIR", Path.home() / "ummanu-data"))
    return base / "memory" / "access-grants"


def card_subject(reference: str, project: str) -> dict[str, str]:
    return {
        "kind": "card",
        "ref": _reference(reference, "card reference"),
        "project": _name(project, "project"),
    }


def sprint_subject(reference: str, reservations: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return {
        "kind": "sprint",
        "ref": _reference(reference, "sprint reference"),
        "reservations": sorted({_name(project, "reservation") for project in reservations}),
    }


def interactive_po_subject(subject: str = "interactive") -> dict[str, str]:
    return {"kind": "interactive", "ref": _reference(subject, "interactive subject")}


def standing_subject(role: str) -> dict[str, str]:
    """The durable standing duty used by the scheduled maintenance heads."""
    name = _name(role, "role")
    return {"kind": "standing", "ref": name}


def _name(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _NAME.fullmatch(text):
        raise MemoryAccessError(f"invalid {label}")
    return text


def _reference(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _REFERENCE.fullmatch(text):
        raise MemoryAccessError(f"invalid {label}")
    return text


def _scopes(role: str, subject: Mapping[str, Any], run: HeadRun) -> frozenset[str] | None:
    role = _name(role, "role")
    kind = str(subject.get("kind") or "")
    reference = _reference(subject.get("ref"), "subject reference")
    if run.role != role:
        raise MemoryAccessError("HeadRun role does not match memory role")
    if run.task_ref.ref != reference:
        raise MemoryAccessError("HeadRun subject does not match memory subject")

    if role == "po":
        if kind != "interactive" or run.task_ref.kind != "standing":
            raise MemoryAccessError("PO memory access requires an interactive HeadRun")
        return None
    if role in {"worker", "reviewer"}:
        if kind != "card" or run.task_ref.kind != "card":
            raise MemoryAccessError("execution memory access requires a card HeadRun")
        project = _name(subject.get("project"), "project")
        return frozenset({f"project:{project}", PRODUCT_UMMANU_SCOPE})
    if role == "observer":
        if kind != "sprint" or run.task_ref.kind != "sprint":
            raise MemoryAccessError("observer memory access requires a sprint HeadRun")
        reservations = subject.get("reservations")
        if not isinstance(reservations, list):
            raise MemoryAccessError("observer memory access has malformed sprint reservations")
        return frozenset(
            {
                PRODUCT_UMMANU_SCOPE,
                *(f"project:{_name(project, 'reservation')}" for project in reservations),
            }
        )
    if role in {"curator", "retro", "steward"}:
        if kind != "standing" or run.task_ref.kind != "standing":
            raise MemoryAccessError("scheduled memory access requires a standing HeadRun")
        if reference != role:
            raise MemoryAccessError("scheduled memory access subject does not match role")
        # The curator and retro compare canon facts across projects.  Steward's system watch
        # only needs the Ummanu product and its implementation project.
        if role in {"curator", "retro"}:
            return None
        return frozenset({PRODUCT_UMMANU_SCOPE, PROJECT_UMMANU_SCOPE})
    raise MemoryAccessError(f"memory read role {role!r} is not permitted")


def issue_grant(
    run: HeadRun,
    subject: Mapping[str, Any],
    *,
    data_dir: str | Path | None = None,
    ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    now: float | None = None,
) -> MemoryAccessGrant:
    """Persist a digest-only grant before the head command is rendered."""
    if ttl_seconds <= 0:
        raise MemoryAccessError("memory grant TTL must be positive")
    copied_subject = json.loads(json.dumps(dict(subject)))
    scopes = _scopes(run.role, copied_subject, run)
    issued = int(time.time() if now is None else now)
    grant_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    token = f"{grant_id}.{secret}"
    payload = {
        "version": GRANT_VERSION,
        "grant_id": grant_id,
        "token_digest": _digest(token),
        "issued_at": issued,
        "expires_at": issued + ttl_seconds,
        "head_run": run.to_json(),
        "subject": copied_subject,
    }
    directory = bindings_dir(data_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    _write_json(directory / f"{grant_id}.json", payload)
    identity = MemoryReadIdentity(run.role, copied_subject, scopes, grant_id)
    return MemoryAccessGrant(grant_id, token, identity)


def resolve_token(
    token: object, *, data_dir: str | Path | None = None, now: float | None = None
) -> MemoryReadIdentity | MemoryAccessDenial:
    if not isinstance(token, str):
        return MemoryAccessDenial("runtime_identity_missing")
    grant_id, separator, _ = token.partition(".")
    if not separator or not re.fullmatch(r"[0-9a-f]{32}", grant_id):
        return MemoryAccessDenial("runtime_identity_malformed")
    return _resolve_payload(_read_payload(grant_id, data_dir), token=token, now=now)


def resolve_grant_id(
    grant_id: object, *, data_dir: str | Path | None = None, now: float | None = None
) -> MemoryReadIdentity | MemoryAccessDenial:
    """Re-check a bearer-authenticated grant at each tool call.

    FastMCP has already verified the bearer secret.  The second check is intentionally
    still performed so a stopped or superseded head cannot keep an existing MCP session.
    """
    if not isinstance(grant_id, str) or not re.fullmatch(r"[0-9a-f]{32}", grant_id):
        return MemoryAccessDenial("runtime_identity_missing")
    return _resolve_payload(_read_payload(grant_id, data_dir), token=None, now=now)


def narrow(
    identity: MemoryReadIdentity, requested_scope: str | None
) -> MemoryReadIdentity | MemoryAccessDenial:
    try:
        scope = normalize_scope(requested_scope)
    except MemoryAccessError:
        return MemoryAccessDenial("scope_malformed", identity)
    if scope is None:
        return identity
    if identity.scopes is None:
        return MemoryReadIdentity(identity.role, identity.subject, frozenset({scope}), identity.grant_id)
    if scope not in identity.scopes:
        return MemoryAccessDenial("scope_not_permitted", identity)
    return MemoryReadIdentity(identity.role, identity.subject, frozenset({scope}), identity.grant_id)


def normalize_scope(scope: str | None) -> str | None:
    if not scope:
        return None
    value = str(scope).strip()
    if not value:
        return None
    if value in {"global", PRODUCT_UMMANU_SCOPE, PO_REVIEW_SCOPE}:
        return value
    if value.startswith("project:"):
        name = value.removeprefix("project:")
        return f"project:{_name(name, 'scope')}"
    return f"project:{_name(value, 'scope')}"


def _read_payload(grant_id: str, data_dir: str | Path | None) -> dict[str, Any] | None:
    path = bindings_dir(data_dir) / f"{grant_id}.json"
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_payload(
    payload: dict[str, Any] | None, *, token: str | None, now: float | None
) -> MemoryReadIdentity | MemoryAccessDenial:
    """Resolve one stored grant, or deny it with the first reason that holds.

    A grant's `head_run` is read with `HeadRun.from_json`, so a legacy Orca record — one naming
    `orca-legacy`, or no runtime — loads like any other and is never `runtime_identity_malformed`
    for its runtime alone. Its runtime is not an identity fact here: liveness is. The head it names
    was an Orca pane, and none is alive, so it is denied as `runtime_identity_unbound` (no pid
    file) or `runtime_identity_stale` (a heartbeat that is not this run's live one). A deliberate
    runtime denial would also cut off a live PO bridge or health probe that wrote its grant before
    they named `local-pty` (both hand-build their spec, and a hand-built spec is legacy by default).
    """
    if payload is None:
        return MemoryAccessDenial("runtime_identity_unknown")
    if payload.get("version") != GRANT_VERSION or not isinstance(payload.get("grant_id"), str):
        return MemoryAccessDenial("runtime_identity_malformed")
    if token is not None:
        expected = payload.get("token_digest")
        if not isinstance(expected, str) or not hmac.compare_digest(expected, _digest(token)):
            return MemoryAccessDenial("runtime_identity_mismatch")
    try:
        expires = int(payload["expires_at"])
    except (KeyError, TypeError, ValueError):
        return MemoryAccessDenial("runtime_identity_malformed")
    if expires < int(time.time() if now is None else now):
        return MemoryAccessDenial("runtime_identity_stale")
    try:
        run = HeadRun.from_json(payload["head_run"])
        subject = payload["subject"]
        if not isinstance(subject, dict):
            raise MemoryAccessError("subject is not an object")
        scopes = _scopes(run.role, subject, run)
    except (KeyError, TypeError, MemoryAccessError, HeadRunError, TaskRefError, ValueError):
        return MemoryAccessDenial("runtime_identity_malformed")
    if not run.pid_file:
        return MemoryAccessDenial("runtime_identity_unbound")
    status = head_process_status(
        run.pid_file,
        expected={"run_id": run.run_id, "role": run.role, "task": f"{run.task_ref.kind}:{run.task_ref.ref}"},
    )
    if status.get("state") != "live-match":
        return MemoryAccessDenial("runtime_identity_stale")
    return MemoryReadIdentity(run.role, dict(subject), scopes, str(payload["grant_id"]))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        # One owner of the descriptor: this frame until `fdopen` takes it, the handle after. A second
        # close of a number another thread has reused since would close that thread's file.
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise
