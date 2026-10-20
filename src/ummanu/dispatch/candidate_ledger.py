"""The card's ledger of rejected candidates, read from its durable board audit.

A red review or a substantive red gate is ledgered on the dispatcher move that acts on it, under
`REJECTED_CANDIDATE_KEY`; every attempt of the card reads the same audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ummanu.board.task_protocol.rows import REJECTED_CANDIDATE_KEY
from ummanu.dispatch.state import REVIEW_REJECTION_REASON, DispatcherRecord
from ummanu.kernel.errors import TaskError

REVIEW_SOURCE = "review"
GATE_SOURCE = "gate"


@dataclass(frozen=True)
class RejectedCandidate:
    """A candidate SHA rejected for one card, and the event that rejected it."""

    sha: str
    source: str
    event_id: str

    def named(self) -> str:
        if self.source == REVIEW_SOURCE:
            return f"the red review verdict {self.event_id}"
        return f"the red mechanical gate {self.event_id}"


def _protocol_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if event.get("record_type") == "board.protocol_event" else None
    return data if isinstance(data, dict) else {}


def rejected_candidate(audit: Any, reference: str, sha: str) -> RejectedCandidate | None:
    """The first ledger entry of this card for `sha`, or None when no verdict rejected it."""
    if not sha:
        return None
    for event in audit.events(reference):
        entry = _protocol_data(event).get(REJECTED_CANDIDATE_KEY)
        if not isinstance(entry, dict) or entry.get("sha") != sha:
            continue
        if entry.get("source") == REVIEW_SOURCE:
            return RejectedCandidate(sha, REVIEW_SOURCE, str(entry["verdict_event_id"]))
        # A gate rejection is ledgered on the gate-red move itself, so that move is its event.
        return RejectedCandidate(sha, GATE_SOURCE, str(event["event_id"]))
    return None


def ledger_entry(runtime: Any, reference: str, record: DispatcherRecord) -> dict[str, str] | None:
    """The entry for the rejection `record` holds, or None when that rejection judged no code."""
    if not record.rejected_sha or record.rejected_failure_class != "substantive":
        return None
    events = runtime.audit.events(reference)
    entry = {"sha": record.rejected_sha}
    for event in events:
        check = _protocol_data(event).get("worker_check")
        if isinstance(check, dict) and check.get("candidate_sha") == record.rejected_sha:
            entry["tree"] = str(check["tree_sha"])
    if record.rejected_failure_reason != REVIEW_REJECTION_REASON:
        entry["source"] = GATE_SOURCE
        return entry
    verdicts = [str(event["event_id"]) for event in events if _protocol_data(event).get("marker") == "review:red"]
    if not verdicts:
        raise TaskError("audit_unavailable", f"{reference} has no review:red verdict event to ledger", 4)
    entry["source"] = REVIEW_SOURCE
    entry["verdict_event_id"] = verdicts[-1]
    return entry
