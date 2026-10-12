"""Typed exact-SHA evidence produced by an executed mechanical gate.

This module owns the receipt schema, normalization, identity digest and rendering.  Dispatcher
lifecycle code only decides when an accepted receipt is persisted or published; it must not grow
parallel interpretations of what constitutes reusable validation evidence.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from ummanu.dispatch.helpers import safe_one_line

_EXACT_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_CONCLUSIONS = frozenset(
    {
        "SUCCESS",
        "NEUTRAL",
        "SKIPPED",
        "FAILURE",
        "TIMED_OUT",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STALE",
        "STARTUP_FAILURE",
    }
)
_PASSED_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_EXECUTED_MODES = frozenset({"local", "github"})
_ALLOWED_MODES = _EXECUTED_MODES | {"none"}


@dataclass(frozen=True)
class TerminalCheck:
    name: str
    conclusion: str
    url: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "conclusion": self.conclusion, "url": self.url}


@dataclass(frozen=True)
class GateReceipt:
    validated_sha: str
    base_sha: str
    gate_mode: str
    required_checks: tuple[TerminalCheck, ...]
    completed_at: str
    command_or_check_set_digest: str

    @classmethod
    def accept(cls, payload: object, *, current_sha: str) -> GateReceipt | None:
        """Validate untrusted persisted/result data against the checkout being judged."""
        if not isinstance(payload, Mapping) or not is_exact_sha(current_sha):
            return None
        validated_sha = str(payload.get("validated_sha") or "")
        base_sha = str(payload.get("base_sha") or "")
        gate_mode = str(payload.get("gate_mode") or "")
        completed_at = safe_one_line(payload.get("completed_at") or "")
        digest = str(payload.get("command_or_check_set_digest") or "")
        raw_checks = payload.get("required_checks")
        if (
            gate_mode not in _EXECUTED_MODES
            or not is_exact_sha(validated_sha)
            or validated_sha != current_sha
            or not is_exact_sha(base_sha)
            or not completed_at
            or not _DIGEST_RE.fullmatch(digest)
            or not isinstance(raw_checks, list)
            or not raw_checks
        ):
            return None
        checks: list[TerminalCheck] = []
        for raw in raw_checks:
            if not isinstance(raw, Mapping):
                return None
            name = safe_one_line(raw.get("name") or "")
            conclusion = safe_one_line(raw.get("conclusion") or "").upper()
            if not name or conclusion not in _PASSED_CONCLUSIONS:
                return None
            checks.append(TerminalCheck(name, conclusion, safe_one_line(raw.get("url") or "")))
        return cls(validated_sha, base_sha, gate_mode, tuple(checks), completed_at, digest)

    def as_dict(self) -> dict[str, object]:
        return {
            "validated_sha": self.validated_sha,
            "base_sha": self.base_sha,
            "gate_mode": self.gate_mode,
            "required_checks": [check.as_dict() for check in self.required_checks],
            "completed_at": self.completed_at,
            "command_or_check_set_digest": self.command_or_check_set_digest,
        }

    def render(self) -> str:
        checks = [
            f"  - {check.name}: {check.conclusion}" + (f" ({check.url})" if check.url else "")
            for check in self.required_checks
        ]
        return "\n".join(
            [
                f"- validated_sha: {self.validated_sha}",
                f"- base_sha: {self.base_sha}",
                f"- gate_mode: {self.gate_mode}",
                "- required terminal checks:",
                *checks,
                f"- completed_at: {self.completed_at}",
                f"- command_or_check_set_digest: {self.command_or_check_set_digest}",
            ]
        )


@dataclass(frozen=True)
class AcceptedGreenGate:
    """The single policy result used at initial validation, park and release."""

    receipt: GateReceipt | None
    policy_valid: bool

    @classmethod
    def accept(cls, payload: object, *, current_sha: str, gate_mode: str, noop: bool) -> AcceptedGreenGate:
        if gate_mode not in _ALLOWED_MODES:
            return cls(None, False)
        receiptless = payload is None or payload == {}
        if noop or gate_mode == "none":
            return cls(None, receiptless)
        receipt = GateReceipt.accept(payload, current_sha=current_sha)
        if receipt is not None and receipt.gate_mode != gate_mode:
            receipt = None
        return cls(receipt, receipt is not None)


def mint_gate_receipt(
    *,
    validated_sha: str,
    base_sha: str,
    gate_mode: str,
    required_checks: Iterable[Mapping[str, object]],
    check_set_identity: str,
) -> dict[str, object] | None:
    """Create canonical terminal evidence. Failed checks may be represented, but only a passing
    receipt is accepted for downstream no-rerun decisions."""
    checks: list[TerminalCheck] = []
    for raw in required_checks:
        name = safe_one_line(raw.get("name") or "")
        conclusion = safe_one_line(raw.get("conclusion") or "").upper()
        if not name or conclusion not in _TERMINAL_CONCLUSIONS:
            return None
        checks.append(TerminalCheck(name, conclusion, safe_one_line(raw.get("url") or "")))
    if (
        gate_mode not in _EXECUTED_MODES
        or not is_exact_sha(validated_sha)
        or not is_exact_sha(base_sha)
        or not checks
    ):
        return None
    receipt = GateReceipt(
        validated_sha,
        base_sha,
        gate_mode,
        tuple(checks),
        datetime.now(UTC).replace(microsecond=0).isoformat(),
        hashlib.sha256(check_set_identity.encode("utf-8", "surrogateescape")).hexdigest(),
    )
    return receipt.as_dict()


def accepted_receipt(payload: object, current_sha: str) -> dict[str, object]:
    receipt = GateReceipt.accept(payload, current_sha=current_sha)
    return receipt.as_dict() if receipt else {}


def render_receipt(payload: object) -> str:
    """Render already-accepted evidence; invalid input never gains an attestation claim."""
    if not isinstance(payload, Mapping):
        return "No valid exact-SHA gate receipt is available; do not treat validation as attested."
    receipt = GateReceipt.accept(payload, current_sha=str(payload.get("validated_sha") or ""))
    if receipt is None:
        return "No valid exact-SHA gate receipt is available; do not treat validation as attested."
    return receipt.render()


def is_exact_sha(value: object) -> bool:
    return bool(_EXACT_SHA_RE.fullmatch(str(value or "")))
