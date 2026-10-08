"""Shared vocabulary for what a prompt delivery into a live interactive head left behind.

The delivery itself belongs to the head backend (`local_pty_head` and its supervisor). This module
holds the delivery stages, readiness and pre-delivery states, persisted `DeliveryEvidence` and the
one receipt derivation; pane-era records carry the same fields and read back unchanged. Pure: no
boards, roles, sessions or terminals. See `docs/PROTOCOLS.md` "A settled head is not a delivered
prompt" and "A live head is not a delivered pointer".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .agent_prompt_transport import AGENT_PROMPT_TRANSPORT_VERSION, TRANSPORT_POLICY

# One delivery attempt's result. `accepted`: the head took the prompt into a turn and the caller's
# own proof of delivery arrives later.
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_ACCEPTED = "accepted"

# Transport acceptance proves only that bytes entered the terminal, not that a turn began.
STAGE_NONE = "none"
STAGE_PAYLOAD_WRITTEN = "payload_written"
STAGE_ENTER_ACCEPTED = "enter_accepted"
STAGE_TURN_OBSERVED = "turn_observed"
STAGE_ACKNOWLEDGED = "acknowledged"

# The composer's content, without prompt text. `unknown`: screen unreadable or no composer marker;
# delivery then relies on readiness alone.
COMPOSER_UNKNOWN = "unknown"
COMPOSER_EMPTY = "empty"

# `blocked`: held in a dialog, neither ready nor working. `unknown`: a failed probe, not a busy head.
READINESS_READY = "ready"
READINESS_BUSY = "busy"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"
# Refused-wait states (stale terminal binding, unavailable transport); recovery must not read them as busy.
READINESS_UNAVAILABLE = "unavailable"
READINESS_STALE_HANDLE = "stale_handle"

# Screen states before a prompt can be taken: the TUI is quiescent yet swallows keystrokes.
PRE_DELIVERY_NONE = ""
# Codex's `Update available!` modal.
PRE_DELIVERY_UPDATE_MODAL = "update-modal"
# `Starting MCP servers` / `tab to queue message`: input is queued, not submitted. Only ever observed
# after the write, in `pre_delivery_after`.
PRE_DELIVERY_STARTING = "starting"
# A screen shaped like a dialog that was not recognised. Nothing is typed at it.
PRE_DELIVERY_UNKNOWN_DIALOG = "unknown-dialog"

# Pre-write sendability. There is no `established`: nothing before a write proves an idle composer,
# so a delivery rests on the post-write receipt.
SENDABILITY_UNESTABLISHED = "unestablished"
SENDABILITY_DIALOG_REFUSED = "dialog-refused"

# `unobserved`: the carrier never reached the delivery boundary (bring-up failed before a prompt); it
# is neither a receipt nor a refusal.
DELIVERY_RECEIPT_ACCEPTED = "accepted"
DELIVERY_RECEIPT_REFUSED = "refused"
DELIVERY_RECEIPT_UNOBSERVED = "unobserved"


def delivery_receipt_state(carrier: Any) -> str:
    """Whether the composer accepted the pointer this evidence was taken for.

    The one predicate launch, recovery and adoption share. A live pid, a writable pane and a
    transport's `accepted`/`bytesWritten` are never consulted. Only evidence carrying a `stage` is the
    boundary's own; anything else is `unobserved`.
    """
    evidence = getattr(carrier, "evidence", carrier)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if not isinstance(evidence, dict) or "stage" not in evidence:
        return DELIVERY_RECEIPT_UNOBSERVED
    if bool(evidence.get("payload_left_in_composer")):
        # Prompt-specific proof the pointer is still unsent: determinate, outranks a confirmed turn.
        return DELIVERY_RECEIPT_REFUSED
    if bool(evidence.get("turn_confirmed")):
        return DELIVERY_RECEIPT_ACCEPTED
    return DELIVERY_RECEIPT_REFUSED


@dataclass
class DeliveryEvidence:
    """What one delivery attempt saw, persistable beside the head.

    Only identifiers, bounded classifications and digests: the prompt is kept as size and hash, never text.
    """

    handle: str = ""
    subject: str = ""
    stage: str = STAGE_NONE
    payload_bytes: int = 0
    payload_sha256: str = ""
    # `nudge-file`: the pane got a bounded line naming a document, so `payload_bytes` is that line's
    # size; the path is kept, the document text never. Empty: the delivery carried its own content.
    delivery_mode: str = ""
    document_path: str = ""
    # The terminal-send adapter used. Body and submit writes are recorded separately; neither proves a
    # turn began, which only the later confirmation stages do.
    transport_version: str = AGENT_PROMPT_TRANSPORT_VERSION
    adapter: str = ""
    framing: str = ""
    transport_policy: str = TRANSPORT_POLICY
    body_write_accepted: bool = False
    body_bytes_written: int = 0
    body_write_count: int = 0
    submit_write_accepted: bool = False
    submit_bytes_written: int = 0
    submit_count: int = 0
    turn_confirmed: bool = False
    # The transport's own `accepted`/`bytesWritten` answer: one stage of delivery, not proof of it.
    send_accepted: bool = False
    bytes_written: int = 0
    # One attempt is one Enter: the first send and every re-entry after it.
    attempts: int = 0
    resends: int = 0
    # Typed outcome of a readiness wait that failed before any probe or write. Empty (older records)
    # reads as unknown, not busy, via `delivery_readiness_state`.
    readiness_state: str = ""
    readiness_before: str = ""
    readiness_after: str = ""
    composer_before: str = COMPOSER_UNKNOWN
    composer_after: str = COMPOSER_UNKNOWN
    payload_left_in_composer: bool = False
    modal_before: bool = False
    modal_after: bool = False
    # Modal-resolution telemetry; says nothing about receipt.
    pre_delivery_before: str = PRE_DELIVERY_NONE
    pre_delivery_after: str = PRE_DELIVERY_NONE
    # Never "established": either a dialog refused the write, or sendability was unestablished and the
    # receipt is what the delivery rests on; the latter is not a proof.
    sendability: str = ""
    modal_resolution: str = ""
    modal_answers: int = 0
    # Provider binding: the caller's criterion (what the provider recorded) answered yes.
    # `turn_confirmed` is what the pane showed; neither implies the other.
    provider_bound: bool = False
    provider_source_state: str = ""
    cursor_before: str = ""
    cursor_after: str = ""
    cursor_moved: bool = False
    # True: the cursors are the backend's own; False: a tail digest stood in for them.
    cursor_from_backend: bool = False
    reason: str = ""
    # A production handoff that is not finished yet stopped at this stage (`runtime.head.handoff`):
    # settle, typed or submitted. Empty for every finished or refused delivery.
    handoff_stage: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "subject": self.subject,
            "stage": self.stage,
            "payload_bytes": self.payload_bytes,
            "payload_sha256": self.payload_sha256,
            "delivery_mode": self.delivery_mode,
            "document_path": self.document_path,
            "transport_version": self.transport_version,
            "adapter": self.adapter,
            "framing": self.framing,
            "transport_policy": self.transport_policy,
            "body_write_accepted": self.body_write_accepted,
            "body_bytes_written": self.body_bytes_written,
            "body_write_count": self.body_write_count,
            "submit_write_accepted": self.submit_write_accepted,
            "submit_bytes_written": self.submit_bytes_written,
            "submit_count": self.submit_count,
            "turn_confirmed": self.turn_confirmed,
            "send_accepted": self.send_accepted,
            "bytes_written": self.bytes_written,
            "attempts": self.attempts,
            "resends": self.resends,
            "readiness_state": self.readiness_state,
            "readiness_before": self.readiness_before,
            "readiness_after": self.readiness_after,
            "composer_before": self.composer_before,
            "composer_after": self.composer_after,
            "payload_left_in_composer": self.payload_left_in_composer,
            "modal_before": self.modal_before,
            "modal_after": self.modal_after,
            "pre_delivery_before": self.pre_delivery_before,
            "pre_delivery_after": self.pre_delivery_after,
            "sendability": self.sendability,
            "modal_resolution": self.modal_resolution,
            "modal_answers": self.modal_answers,
            "provider_bound": self.provider_bound,
            "provider_source_state": self.provider_source_state,
            # Derived; persisted so readers need not re-derive it.
            "delivery_receipt": self.receipt,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "cursor_moved": self.cursor_moved,
            "cursor_from_backend": self.cursor_from_backend,
            "reason": self.reason,
            **({"handoff_stage": self.handoff_stage} if self.handoff_stage else {}),
        }

    @property
    def receipt(self) -> str:
        """`delivery_receipt_state` over the three stored fields (not `self`, to avoid a `to_json` cycle)."""
        return delivery_receipt_state(
            {
                "stage": self.stage,
                "payload_left_in_composer": self.payload_left_in_composer,
                "turn_confirmed": self.turn_confirmed,
            }
        )

    @classmethod
    def from_json(cls, payload: Any) -> DeliveryEvidence:
        if not isinstance(payload, dict):
            return cls()
        fields = cls()
        for name, value in payload.items():
            # Only stored fields are restored; derived keys such as `delivery_receipt` are ignored.
            if name not in cls.__dataclass_fields__:
                continue
            current = getattr(fields, name)
            if isinstance(current, bool):
                setattr(fields, name, bool(value))
            elif isinstance(current, int):
                try:
                    setattr(fields, name, int(value))
                except (TypeError, ValueError):
                    pass
            else:
                setattr(fields, name, str(value or ""))
        return fields


class DeliveryOutcome(str):
    """The delivery verdict a caller compares, carrying the evidence that produced it."""

    evidence: DeliveryEvidence

    def __new__(cls, value: str, evidence: DeliveryEvidence) -> DeliveryOutcome:
        outcome = super().__new__(cls, value)
        outcome.evidence = evidence
        return outcome


class TuiDeliveryError(RuntimeError):
    """A delivery that did not reach its confirmation, with what was seen of it attached."""

    def __init__(self, message: str, *, evidence: DeliveryEvidence | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence if evidence is not None else DeliveryEvidence(reason=message)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def payload_fingerprint(prompt: str) -> tuple[int, str]:
    """The size and hash of a payload, which is all of it that is ever recorded."""
    raw = (prompt or "").encode("utf-8", "replace")
    return len(raw), _digest(prompt or "")


def delivery_readiness_state(carrier: Any) -> str:
    """The typed readiness state carried by a failed delivery, conservatively.

    Anything not explicitly busy, blocked, unavailable or stale-handle (including records predating
    `readiness_state`) is unknown, never busy.
    """
    evidence = getattr(carrier, "evidence", carrier)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if isinstance(evidence, dict):
        state = str(evidence.get("readiness_state") or "")
    else:
        state = str(getattr(evidence, "readiness_state", "") or "")
    if state in {READINESS_BUSY, READINESS_BLOCKED, READINESS_UNAVAILABLE, READINESS_STALE_HANDLE}:
        return state
    return READINESS_UNKNOWN
