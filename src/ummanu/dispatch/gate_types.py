"""Mechanical gate result identity, import-time limits and shared fingerprint bytes."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

# Pending CI has a bounded watchdog so missing checks cannot strand a card.
GATE_PENDING_STALL_SECONDS = int(os.environ.get("UMMANU_GATE_PENDING_STALL_SECONDS", str(6 * 3600)))

GATE_LOG_FRAGMENT_LINES = int(os.environ.get("UMMANU_GATE_LOG_FRAGMENT_LINES", "40"))

# Consecutive backend silence is bounded before human escalation.
GATE_TRANSPORT_MAX_ATTEMPTS = max(1, int(os.environ.get("UMMANU_GATE_TRANSPORT_MAX_ATTEMPTS", "5")))

# Enumerated CI-service failures rerun per SHA with a bounded cap.
GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS = max(
    1, int(os.environ.get("UMMANU_GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS", "2"))
)

PR_BODY_SECTION_CHARS = int(os.environ.get("UMMANU_PR_BODY_SECTION_CHARS", "800"))


@dataclass
class GateResult:
    status: str  # "green" | "red" | "pending"
    summary: str
    log: str = ""
    fingerprint: str = ""
    # Structured terminal reds decide whether the exact SHA may rerun.
    # "substantive" | "infrastructure" | "publication" | "topology"
    failure_class: str = "substantive"
    failure_reason: str = ""
    # The Actions run which supplied an infrastructure-classified red.  Only this concrete run
    # can be rerun; an external status or a log without a run URL remains a red verdict.
    failed_run_id: str = ""
    failed_run_repo: str = ""
    # Reusable green evidence names the exact tree and terminal checks.
    attestation: dict[str, object] | None = None


def _fingerprint(*parts: str) -> str:
    """Stable short digest of supplied fields, used to compare repeated failures.

Include the head SHA in the caller's detail so different candidates do not
collapse to one apparent repeat.
    """
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "surrogateescape")).hexdigest()[:16]
