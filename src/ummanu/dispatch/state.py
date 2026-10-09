"""State helpers for the production dispatcher."""

from __future__ import annotations

import copy
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ummanu.dispatch.types import DispatcherError
from ummanu.dispatch.worker_lifecycle import (
    WorkerContinuation,
    WorkerContinuationLiveness,
    WorkerReportNudge,
)
from ummanu.routing_journal import RoutingHeadSnapshot
from ummanu.runtime.head import HeadRun
from ummanu.runtime.tui_delivery import DeliveryEvidence

# ``VitalityEpisode`` is imported lazily in ``DispatcherRecord.from_json``: the episode module
# reads the heartbeat vocabulary from ``dispatcher_watchdog``, whose own imports reach back into
# this module, so an eager import here would close a cycle at load time. A deferred import keeps
# the record's typed persistence without making the state module an ancestor of the vocabulary.

if TYPE_CHECKING:
    from ummanu.dispatch.gate_receipt import GateReceipt
    from ummanu.dispatch.head_vitality_episode import VitalityEpisode

    # Registry of claim skips: Ready records these and continues scanning.
CLAIM_SKIP_RESOURCE_NOT_READY = "resource-not-ready"
CLAIM_SKIP_FAILOVER_COLLAPSE = "failover-collapses-roles"
# The project's remote gave the bounded Git access preflight no answer: nothing is known about the
# credential, so the card stays Ready rather than being blocked on silence.
CLAIM_SKIP_GIT_ACCESS_UNREACHABLE = "project-git-access-unreachable"
# A code card linked to no sprint whose project's sprint reservations could not be verified. Nothing
# is written: the Blocked move would meet the same unverifiable index at the write guard.
CLAIM_SKIP_SPRINT_RESERVATION_UNVERIFIABLE = "sprint-reservation-unverifiable"
CLAIM_SKIP_ACTIONS = frozenset(
    {
        CLAIM_SKIP_RESOURCE_NOT_READY,
        CLAIM_SKIP_FAILOVER_COLLAPSE,
        CLAIM_SKIP_GIT_ACCESS_UNREACHABLE,
        CLAIM_SKIP_SPRINT_RESERVATION_UNVERIFIABLE,
    }
)


class OutcomeTerminalPath(str, Enum):
    """Whether a terminal effect follows the round's accepted worker report.

    This is deliberately independent of the report handoff.  The handoff may
    be unavailable precisely when the terminal obligation needs to say that
    its forward lineage is incomplete.
    """

    NO_ACCEPTED_REPORT = "no_accepted_report"
    FOLLOWS_ACCEPTED_REPORT = "follows_accepted_report"


# `rejected_failure_reason` of a checkout a red review rejected, as opposed to one a mechanical
# gate bounced: both rejections are `substantive`, and only the reason tells them apart.
REVIEW_REJECTION_REASON = "red-review"


def outcome_terminal_path(value: Any, *, state: str) -> OutcomeTerminalPath:
    """Read the durable path, conservatively classifying pre-field records."""
    if value in {path.value for path in OutcomeTerminalPath}:
        return OutcomeTerminalPath(str(value))
    if value not in (None, ""):
        raise DispatcherError("invalid_outcome_terminal_path", f"unknown outcome terminal path {value!r}")
    # A record from before the explicit field can already have accepted a
    # report and left In progress.  Its state is the dispatcher-owned path
    # fact, never a source-handoff or marker lookup.
    if state in {"validate", "review_starting", "reviewing", "assessment"}:
        return OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT
    return OutcomeTerminalPath.NO_ACCEPTED_REPORT


def is_claim_skip(outcome: dict[str, Any]) -> bool:
    """Whether a claim outcome is "not this card, next card" rather than the pass's answer."""
    return str(outcome.get("action") or "") in CLAIM_SKIP_ACTIONS


@dataclass(frozen=True)
class GatePrAuthorship:
    """The exact pull-request text identity the gate is allowed to refresh."""

    number: int
    digest: str
    sent: str = ""

    @classmethod
    def from_json(cls, payload: Any) -> GatePrAuthorship | None:
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict):
            return None
        try:
            number = int(payload.get("number") or 0)
        except (TypeError, ValueError):
            return None
        digest = str(payload.get("digest") or "")
        if number <= 0 or not digest:
            return None
        return cls(number=number, digest=digest, sent=str(payload.get("sent") or ""))

    def to_json(self) -> dict[str, Any]:
        return {"number": self.number, "digest": self.digest, "sent": self.sent}


@dataclass(frozen=True)
class GatePublishedRef:
    """The remote branch/object pair last published by the gate itself."""

    branch: str
    sha: str

    @classmethod
    def from_json(cls, payload: Any) -> GatePublishedRef | None:
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict):
            return None
        branch = str(payload.get("branch") or "")
        if not branch:
            return None
        return cls(branch=branch, sha=str(payload.get("sha") or ""))

    def to_json(self) -> dict[str, Any]:
        return {"branch": self.branch, "sha": self.sha}


class PersistedGateReceipt(dict[str, Any]):
    """One durable exact-SHA gate receipt with an exact compatibility projection."""

    __slots__ = ("_receipt",)

    def __init__(self, value: Any = None) -> None:
        # Lazy to avoid dispatch.state -> gate_receipt -> dispatch.helpers -> dispatch.state
        # at module import time. By the time a record is instantiated the modules are fully loaded.
        from ummanu.dispatch.gate_receipt import GateReceipt

        typed: GateReceipt | None = value if isinstance(value, GateReceipt) else None
        if typed is not None:
            payload = typed.as_dict()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)
        if typed is None and payload:
            typed = GateReceipt.accept(payload, current_sha=str(payload.get("validated_sha") or ""))
        self._receipt = typed

    @classmethod
    def from_value(cls, value: Any) -> PersistedGateReceipt:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def receipt(self) -> GateReceipt | None:
        return self._receipt

    def to_json(self) -> dict[str, Any]:
        return dict(self)


class PersistedGatePrAuthorship(dict[str, Any]):
    """Durable PR authorship with a canonical typed view and unchanged JSON."""

    __slots__ = ("_authorship",)

    def __init__(self, value: Any = None) -> None:
        typed: GatePrAuthorship | None = value if isinstance(value, GatePrAuthorship) else None
        if typed is not None:
            payload = typed.to_json()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)
        if typed is None and payload:
            typed = GatePrAuthorship.from_json(payload)
        self._authorship = typed

    @classmethod
    def from_value(cls, value: Any) -> PersistedGatePrAuthorship:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def authorship(self) -> GatePrAuthorship | None:
        return self._authorship

    def to_json(self) -> dict[str, Any]:
        return dict(self)


class PersistedGatePublishedRef(dict[str, Any]):
    """Durable publication lease with a canonical typed view and unchanged JSON."""

    __slots__ = ("_published_ref",)

    def __init__(self, value: Any = None) -> None:
        typed: GatePublishedRef | None = value if isinstance(value, GatePublishedRef) else None
        if typed is not None:
            payload = typed.to_json()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)
        if typed is None and payload:
            typed = GatePublishedRef.from_json(payload)
        self._published_ref = typed

    @classmethod
    def from_value(cls, value: Any) -> PersistedGatePublishedRef:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def published_ref(self) -> GatePublishedRef | None:
        return self._published_ref

    def to_json(self) -> dict[str, Any]:
        return dict(self)


class PersistedDeliveryEvidence(dict[str, Any]):
    """Durable prompt-delivery evidence with a typed view and exact historical mapping."""

    __slots__ = ("_evidence",)

    def __init__(self, value: Any = None) -> None:
        typed: DeliveryEvidence | None = value if isinstance(value, DeliveryEvidence) else None
        if typed is not None:
            payload = typed.to_json()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)
        if typed is None and payload:
            typed = DeliveryEvidence.from_json(payload)
        self._evidence = typed

    @classmethod
    def from_value(cls, value: Any) -> PersistedDeliveryEvidence:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def evidence(self) -> DeliveryEvidence | None:
        return self._evidence

    def to_json(self) -> dict[str, Any]:
        return dict(self)

@dataclass(frozen=True)
class LaunchDelivery:
    """Typed view of the retry/delivery receipt nested inside one launch intent."""

    state: str = ""
    receipt: str = ""
    attempts: int = 0
    next_at: float = 0.0
    evidence: DeliveryEvidence | None = None

    @classmethod
    def from_json(cls, payload: Any) -> LaunchDelivery | None:
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict) or not payload:
            return None
        try:
            attempts = int(payload.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        try:
            next_at = float(payload.get("next_at") or 0.0)
        except (TypeError, ValueError):
            next_at = 0.0
        evidence_payload = payload.get("evidence")
        evidence = (
            DeliveryEvidence.from_json(evidence_payload)
            if isinstance(evidence_payload, dict)
            else None
        )
        return cls(
            state=str(payload.get("state") or ""),
            receipt=str(payload.get("receipt") or ""),
            attempts=attempts,
            next_at=next_at,
            evidence=evidence,
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.state:
            payload["state"] = self.state
        if self.receipt:
            payload["receipt"] = self.receipt
        if self.attempts:
            payload["attempts"] = self.attempts
        if self.next_at:
            payload["next_at"] = self.next_at
        if self.evidence is not None:
            payload["evidence"] = self.evidence.to_json()
        return payload


@dataclass(frozen=True)
class LaunchIntent:
    """Canonical in-memory value for one crash-recoverable worker/reviewer launch."""

    role: str
    action: str = ""
    head: str = ""
    workspace: str = ""
    pid_file: str = ""
    run_id: str = ""
    task: str = ""
    attempt_id: str = ""
    round_number: int = 0
    opens_round: bool = False
    respawns: int = 0
    at: float = 0.0
    handle: str = ""
    leaf: str = ""
    routing_run: RoutingHeadSnapshot | None = None
    head_run: HeadRun | None = None
    delivery: LaunchDelivery | None = None
    launched: bool = False
    aborted: bool = False

    @classmethod
    def from_json(cls, payload: Any) -> LaunchIntent | None:
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict):
            return None
        role = str(payload.get("role") or "")
        if not role:
            return None
        try:
            round_number = int(payload.get("round") or 0)
        except (TypeError, ValueError):
            round_number = 0
        try:
            respawns = int(payload.get("respawns") or 0)
        except (TypeError, ValueError):
            respawns = 0
        try:
            launched_at = float(payload.get("at") or 0.0)
        except (TypeError, ValueError):
            launched_at = 0.0

        routing_run: RoutingHeadSnapshot | None = None
        raw_run = payload.get("run")
        if isinstance(raw_run, dict) and raw_run:
            try:
                routing_run = RoutingHeadSnapshot.from_json(raw_run)
            except (KeyError, TypeError, ValueError):
                routing_run = None

        head_run: HeadRun | None = None
        raw_head_run = payload.get("head_run")
        if isinstance(raw_head_run, dict) and raw_head_run:
            try:
                head_run = HeadRun.from_json(raw_head_run)
            except (KeyError, RuntimeError, TypeError, ValueError):
                head_run = None

        return cls(
            role=role,
            action=str(payload.get("action") or ""),
            head=str(payload.get("head") or ""),
            workspace=str(payload.get("workspace") or ""),
            pid_file=str(payload.get("pid_file") or ""),
            run_id=str(payload.get("run_id") or ""),
            task=str(payload.get("task") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            round_number=round_number,
            opens_round=bool(payload.get("opens_round", False)),
            respawns=respawns,
            at=launched_at,
            handle=str(payload.get("handle") or ""),
            leaf=str(payload.get("leaf") or ""),
            routing_run=routing_run,
            head_run=head_run,
            delivery=LaunchDelivery.from_json(payload.get("delivery")),
            launched=bool(payload.get("launched", False)),
            aborted=bool(payload.get("aborted", False)),
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": self.role,
            "action": self.action,
            "head": self.head,
            "workspace": self.workspace,
            "pid_file": self.pid_file,
            "run_id": self.run_id,
            "task": self.task,
            "attempt_id": self.attempt_id,
            "round": self.round_number,
            "opens_round": self.opens_round,
            "respawns": self.respawns,
            "at": self.at,
        }
        if self.handle:
            payload["handle"] = self.handle
        if self.leaf:
            payload["leaf"] = self.leaf
        if self.routing_run is not None:
            payload["run"] = self.routing_run.to_json()
        if self.head_run is not None:
            payload["head_run"] = self.head_run.to_json()
        if self.delivery is not None:
            payload["delivery"] = self.delivery.to_json()
        if self.launched:
            payload["launched"] = True
        if self.aborted:
            payload["aborted"] = True
        return payload


class PersistedLaunchIntent(dict[str, Any]):
    """Durable launch intent with a typed view and exact historical JSON projection."""

    def __init__(self, value: Any = None) -> None:
        if isinstance(value, LaunchIntent):
            payload = value.to_json()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)

    @classmethod
    def from_value(cls, value: Any) -> PersistedLaunchIntent:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def intent(self) -> LaunchIntent | None:
        # Launch recovery still has legacy in-place mapping writes. Parse the current mapping on
        # access so the typed view cannot go stale while those call sites are migrated.
        return LaunchIntent.from_json(self)

    def to_json(self) -> dict[str, Any]:
        return dict(self)


@dataclass(frozen=True)
class HeadlessRecoveryEpisode:
    """One durable episode where an active card has no worker identity to recover from."""

    since: float = 0.0
    comment_baseline: int = 0
    record_state: str = ""
    handle_known: bool = False
    heartbeat: str = ""
    workspace: str = ""
    branch: str = ""
    expected_branch: str = ""
    dirty: bool | None = None
    candidate_sha: str = ""
    report_generation: int = 0
    recovery_error: str = ""

    @classmethod
    def from_json(cls, payload: Any) -> HeadlessRecoveryEpisode | None:
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict) or not payload:
            return None
        try:
            since = float(payload.get("since") or 0.0)
        except (TypeError, ValueError):
            since = 0.0
        try:
            comment_baseline = int(payload.get("comment_baseline") or 0)
        except (TypeError, ValueError):
            comment_baseline = 0
        try:
            report_generation = int(payload.get("report_generation") or 0)
        except (TypeError, ValueError):
            report_generation = 0
        dirty_value = payload.get("dirty")
        dirty = dirty_value if isinstance(dirty_value, bool) else None
        return cls(
            since=since,
            comment_baseline=comment_baseline,
            record_state=str(payload.get("record_state") or ""),
            handle_known=bool(payload.get("handle_known", False)),
            heartbeat=str(payload.get("heartbeat") or ""),
            workspace=str(payload.get("workspace") or ""),
            branch=str(payload.get("branch") or ""),
            expected_branch=str(payload.get("expected_branch") or ""),
            dirty=dirty,
            candidate_sha=str(payload.get("candidate_sha") or ""),
            report_generation=report_generation,
            recovery_error=str(payload.get("recovery_error") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "comment_baseline": self.comment_baseline,
            "record_state": self.record_state,
            "handle_known": self.handle_known,
            "heartbeat": self.heartbeat,
            "workspace": self.workspace,
            "branch": self.branch,
            "expected_branch": self.expected_branch,
            "dirty": self.dirty,
            "candidate_sha": self.candidate_sha,
            "report_generation": self.report_generation,
            "recovery_error": self.recovery_error,
        }


class PersistedHeadlessRecoveryEpisode(dict[str, Any]):
    """Durable headless recovery state with a canonical typed view."""

    def __init__(self, value: Any = None) -> None:
        if isinstance(value, HeadlessRecoveryEpisode):
            payload = value.to_json()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)

    @classmethod
    def from_value(cls, value: Any) -> PersistedHeadlessRecoveryEpisode:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def episode(self) -> HeadlessRecoveryEpisode | None:
        # Recovery annotates the episode in place with its final refusal. Keep the typed view live.
        return HeadlessRecoveryEpisode.from_json(self)

    def to_json(self) -> dict[str, Any]:
        return dict(self)


class PersistedHeadRun(dict[str, Any]):
    """One durable lifecycle HeadRun with an exact compatibility projection.

    The released dispatcher state stores the lifecycle run as a JSON object and a large amount of
    existing recovery/status code still treats that object as a mapping.  A13 starts the migration
    without rewriting that wire contract: the value keeps the exact mapping for those callers while
    also parsing a canonical typed ``HeadRun`` once at the record boundary.

    Historical/minimal records that predate the complete HeadRun schema remain byte-for-byte
    readable.  They deliberately expose ``run is None`` instead of being silently upgraded into a
    typed run whose missing identity fields would be invented.  Current complete records carry the
    canonical value in ``run``.
    """

    __slots__ = ("_run",)

    def __init__(self, value: Any = None) -> None:
        typed: HeadRun | None = value if isinstance(value, HeadRun) else None
        if typed is not None:
            payload = typed.to_json()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)
        if typed is None and payload:
            try:
                typed = HeadRun.from_json(payload)
            except (KeyError, RuntimeError, TypeError, ValueError):
                typed = None
        self._run = typed

    @classmethod
    def from_value(cls, value: Any) -> PersistedHeadRun:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def run(self) -> HeadRun | None:
        """The canonical lifecycle value when this record has the complete modern schema."""
        return self._run

    def to_json(self) -> dict[str, Any]:
        """Project the exact released dispatcher-state object."""
        return dict(self)


class PersistedRoutingHeadSnapshot(dict[str, Any]):
    """One durable routing snapshot with an exact compatibility projection.

    The task journal already has the canonical immutable RoutingHeadSnapshot, but dispatcher state
    historically stored worker_run/review_run as raw dictionaries. This boundary parses a typed
    snapshot once while retaining the exact released mapping for restart and status callers.

    Empty values still mean "no routing snapshot". Historical partial mappings remain readable;
    their typed view is normalized by RoutingHeadSnapshot.from_json while to_json() preserves the
    exact persisted keys that were loaded.
    """

    __slots__ = ("_snapshot",)

    def __init__(self, value: Any = None) -> None:
        typed: RoutingHeadSnapshot | None = value if isinstance(value, RoutingHeadSnapshot) else None
        if typed is not None:
            payload = typed.to_json()
        elif isinstance(value, dict):
            payload = dict(value)
        else:
            payload = {}
        dict.__init__(self, payload)
        if typed is None and payload:
            try:
                typed = RoutingHeadSnapshot.from_json(payload)
            except (KeyError, TypeError, ValueError):
                typed = None
        self._snapshot = typed

    @classmethod
    def from_value(cls, value: Any) -> PersistedRoutingHeadSnapshot:
        if isinstance(value, cls):
            return value
        return cls(value)

    @property
    def snapshot(self) -> RoutingHeadSnapshot | None:
        """The canonical routing value when this record carries a snapshot."""
        return self._snapshot

    def to_json(self) -> dict[str, Any]:
        """Project the exact released dispatcher-state object."""
        return dict(self)


@dataclass
class PoSubmission:
    """A `decision`/`operation` card handed to its sprint's PO session (secretary-1758).

    Empty (`kind == ""`) on every record of a card a head runs. The request ids are derived at
    claim from the card ref and the claim attempt and never change afterwards: an unanswered resolve
    or submit is repeated under the same id, because a fresh one can open a second PO session. The
    input text is frozen the first time it is composed for the same reason, since the service binds
    a submit id to its exact text.
    """

    kind: str = ""
    sprint_ref: str = ""
    session_request_id: str = ""
    submit_request_id: str = ""
    complete_request_id: str = ""
    # The id the input offers the PO for `task handover` (secretary-1761); empty on a record of card 3.
    handover_request_id: str = ""
    # The resolve's answer: the session, and whether the service opened it (`created`) or the
    # sprint already recorded it (`recorded`).
    session_id: str = ""
    session_outcome: str = ""
    text: str = ""
    # The card facts the submit carries beside the text (secretary-1764), frozen with it: the service
    # binds the submit id to both. Empty on a record from before them.
    card: dict[str, Any] = field(default_factory=dict)
    # The submit's answer: accepted, and the turn it became once the service has claimed it.
    submitted: bool = False
    seq: int | None = None
    # Consecutive resolve/submit calls the service did not answer, and what the last one said.
    unanswered: int = 0
    last_error: str = ""
    # The latest follow-up input carrying the owner's answer to a card the PO handed to the owner
    # (secretary-1761): the owner comment's event id it answers, its request id (derived from the
    # card ref and that event id), its frozen text, and whether the service accepted it.
    owner_event_id: str = ""
    owner_request_id: str = ""
    owner_text: str = ""
    owner_submitted: bool = False

    def __bool__(self) -> bool:
        return bool(self.kind)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sprint_ref": self.sprint_ref,
            "session_request_id": self.session_request_id,
            "submit_request_id": self.submit_request_id,
            "complete_request_id": self.complete_request_id,
            "handover_request_id": self.handover_request_id,
            "session_id": self.session_id,
            "session_outcome": self.session_outcome,
            "text": self.text,
            "card": dict(self.card),
            "submitted": self.submitted,
            "seq": self.seq,
            "unanswered": self.unanswered,
            "last_error": self.last_error,
            "owner_event_id": self.owner_event_id,
            "owner_request_id": self.owner_request_id,
            "owner_text": self.owner_text,
            "owner_submitted": self.owner_submitted,
        }

    @classmethod
    def from_json(cls, payload: Any) -> PoSubmission:
        if not isinstance(payload, dict):
            return cls()
        seq = payload.get("seq")
        return cls(
            kind=str(payload.get("kind") or ""),
            sprint_ref=str(payload.get("sprint_ref") or ""),
            session_request_id=str(payload.get("session_request_id") or ""),
            submit_request_id=str(payload.get("submit_request_id") or ""),
            complete_request_id=str(payload.get("complete_request_id") or ""),
            handover_request_id=str(payload.get("handover_request_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            session_outcome=str(payload.get("session_outcome") or ""),
            text=str(payload.get("text") or ""),
            card=dict(payload["card"]) if isinstance(payload.get("card"), dict) else {},
            submitted=bool(payload.get("submitted", False)),
            seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
            unanswered=int(payload.get("unanswered") or 0),
            last_error=str(payload.get("last_error") or ""),
            owner_event_id=str(payload.get("owner_event_id") or ""),
            owner_request_id=str(payload.get("owner_request_id") or ""),
            owner_text=str(payload.get("owner_text") or ""),
            owner_submitted=bool(payload.get("owner_submitted", False)),
        )


@dataclass(frozen=True)
class ActivationRecovery:
    """The refused release's exact facts and board requests, owed until terminal settlement."""

    facts: dict[str, Any]
    operation: dict[str, Any]
    comment_request_id: str
    block_request_id: str
    step: str

    def to_json(self) -> dict[str, Any]:
        return copy.deepcopy({
            "facts": self.facts,
            "operation": self.operation,
            "comment_request_id": self.comment_request_id,
            "block_request_id": self.block_request_id,
            "step": self.step,
        })

    @classmethod
    def from_json(cls, payload: Any) -> ActivationRecovery | None:
        if payload is None:
            return None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("facts"), dict)
            or not isinstance(payload.get("operation"), dict)
            or not payload["operation"].get("request_id")
            or any(not isinstance(payload.get(key), str) or not payload[key]
                   for key in ("comment_request_id", "block_request_id", "step"))
        ):
            raise DispatcherError("invalid_activation_recovery", "invalid persisted release activation recovery")
        return cls(**copy.deepcopy(payload))


@dataclass
class DispatcherRecord:
    worker: str
    workspace: str
    handle: str
    head: str
    review_head: str
    attempt_id: str
    comment_baseline: int
    review_baseline: int
    state: str
    claimed_at: float
    activation_recovery: ActivationRecovery | None = None
    # Durable report round advances only when a new round opens, never on respawn.
    # The head each role was preferred on when the claim had to leave that preference behind
    # (secretary-1165), empty when it did not. The claim walks the canon's fallback chain when the
    # preferred head's resource is red or spent, and `head`/`review_head` above then name another
    # family. Kept here because the preference is a claim-time fact: re-reading `role_defaults` at
    # document-build time would answer a different question, and an operator who repoints a role
    # mid-attempt would turn a faithful record into a false one.
    preferred_head: str = ""
    preferred_review_head: str = ""
    report_generation: int = 0
    # One validated policy per role round, persisted before packet rendering and launch.
    worker_local_run_snapshot: dict[str, Any] = field(default_factory=dict)
    review_local_run_snapshot: dict[str, Any] = field(default_factory=dict)
    # Frozen when the dispatcher accepts a worker report, before any source
    # handoff is consulted.  Every later terminal effect reads this one typed
    # classification, so losing a report handoff cannot redefine the path.
    outcome_terminal_path: OutcomeTerminalPath = OutcomeTerminalPath.NO_ACCEPTED_REPORT
    # The observer decision that opened the round `report_generation` names, empty when no decision
    # opened it (secretary-1064). Frozen here with the generation, in the same write, because the
    # worker of the round must be handed the adjudication its round was opened on: reading "the
    # latest decision comment" at document-build time answers a different question, and a decision
    # recorded while the round runs would silently replace the instruction. Assigned, like the
    # generation, whenever a red transition opens a round, so a gate-red round carries no stale
    # decision from the review round before it.
    report_decision: str = ""
    # Canonical source frozen with the rework generation; empty in released records.
    report_decision_id: str = ""
    # The revision-bound protocol prerequisite names that opened this worker round. The dispatcher
    # writes them only after resolving the observer declaration through the ownership registry.
    report_protocol_prerequisites: tuple[str, ...] = ()
    # Mechanical validation gate (secretary-633): "" until the gate is green for the current code
    # state, then "green". Reset to "" on every fresh entry to validate so a reworked card re-runs
    # the gate instead of coasting on a stale pass, except for an observer-directed report-only
    # research continuation whose accepted receipt still names its unchanged candidate. gate_pending_since
    # stamps when a github CI rollup first went non-terminal, driving the pending watchdog.
    gate_state: str = ""
    gate_pending_since: float = 0.0
    # SHA-bound result of the last green mechanical gate.  It is an evidence receipt, not a
    # cache key: release still re-runs the gate immediately before merge.
    gate_attestation: PersistedGateReceipt = field(default_factory=PersistedGateReceipt)
    # Comment effects are frozen independently of the latest observation receipt. Keyed by
    # full receipt semantics and delivery context, with original bytes saved before the write.
    gate_attestation_effects: dict[str, Any] = field(default_factory=dict)
    gate_attestation_failure: dict[str, Any] = field(default_factory=dict)
    # Consecutive times the gate backend failed to answer at all (secretary-1164), and the last
    # such failure. A transport failure decides nothing about the card, so it is counted here and
    # retried on the next tick; only the exhausted count blocks the card, naming the transport.
    # Both reset the moment any answer — green, red or pending — comes back.
    gate_transport_failures: int = 0
    gate_transport_error: str = ""
    # The rerun is a second backend operation after an answered red result.  It has the same
    # transport ceiling, but keeps its own consecutive count so that rereading the red result does
    # not erase an unanswered rerun POST on the following tick.
    gate_rerun_transport_failures: int = 0
    gate_rerun_transport_error: str = ""
    # Recovery of a classified CI-service red is bounded separately from transport retries.  It is
    # anchored to the SHA and exact Actions run the gate reran, so a rework starts clean while an
    # unchanged checkout cannot spin on the old terminal check-run.
    gate_infrastructure_reruns_sha: str = ""
    gate_infrastructure_reruns: int = 0
    gate_infrastructure_rerun_run_id: str = ""
    gate_infrastructure_rerun_reason: str = ""
    # Gate-authored PR identity lives outside editable PR text; absence forbids refresh.
    gate_pr_authorship: PersistedGatePrAuthorship = field(default_factory=PersistedGatePrAuthorship)
    # The card branch and object id the gate last published (secretary-1540).  A held worker
    # rebases, so publication is a rewrite of the ref the dispatcher itself wrote; this durable
    # observation is the lease that rewrite is fenced against, and a remote sitting anywhere else
    # is a foreign push the gate refuses instead of clobbering.
    gate_published_ref: PersistedGatePublishedRef = field(default_factory=PersistedGatePublishedRef)
    # Last checkout rejected by a mechanical gate or red review in this attempt.  The class and
    # reason come from the gate's structured result, before any card comment is made.  A same-SHA
    # report after an infrastructure red may retry that gate; every other same-SHA report is still
    # the stale-result safeguard and returns to rework once before escalating.
    rejected_sha: str = ""
    rejected_failure_class: str = "substantive"
    rejected_failure_reason: str = ""
    rejected_done_reports: int = 0
    # When the dispatcher last put a question to the worker head that the head has not answered:
    # the instant a done report was bounced back to rework (secretary-1543). It is a fact about
    # the board conversation, not an observation of the head, and the vitality reducer takes it as
    # a declared input -- with an ended turn and no progress since, it is an explicit stall signal
    # instead of something the outer ceiling notices hours later. Cleared when a report is
    # accepted, and when a replacement head that never saw the rejection takes over.
    worker_answer_owed_since: float = 0.0
    # Transient, never serialised (secretary-1692): the factual line a wait-watchdog respawn hands
    # the successor about the command its predecessor was stopped in, set by ``_respawn_wait`` for
    # the one bring-up it calls and cleared right after. Keeping it off the durable record means
    # no later rework, review or restart can ever render a stale one.
    respawn_interrupted_command: str = field(default="", compare=False)
    # Reviewer leaf is stable across handle aliases; its commit fences verdicts to its checkout.
    review_handle: str = ""
    review_leaf: str = ""
    review_commit: str = ""
    # Set by the release identity check for the gate audit in this tick only.
    review_reconciliation: dict[str, str | int] | None = field(default=None, compare=False)
    # Re-review packet: the accepted predecessor checkout and full verdict body.
    # These survive rework so the next independent reviewer can inspect the delta.
    previous_reviewed_sha: str = ""
    previous_blockers: str = ""
    previous_review_id: str = ""
    # The worker pane has the same handle-alias problem as the reviewer pane.  Keep its leafId
    # too, so an inventory alias cannot turn a live worker into a missing-terminal respawn.
    worker_leaf: str = ""
    # Heartbeats preserve head identity across lost pane handles; clear only on confirmed stop.
    worker_pid_file: str = ""
    review_pid_file: str = ""
    # A Ready record keeps its workspace so the next claim can reuse the checkout. Once
    # reconciliation has stopped that workspace, remember the result separately from the head
    # identities so later ticks do not stop the same checkout again.
    workspace_settled: bool = False
    # Wait watchdogs (secretary-654): when the current wait for a worker report / review
    # verdict started, and how many times that wait has already respawned its head. Both
    # reset whenever the card enters a fresh wait of that kind.
    worker_waiting_since: float = 0.0
    worker_respawns: int = 0
    # Most recent output from the tracked head pane.  This is deliberately pane-scoped: output
    # from an unrelated shell in the same worktree must not keep a broken head alive.
    worker_started_at: float = 0.0
    worker_progress_at: float = 0.0
    # Since when the head has been ready for input with nothing delivered for the round being
    # waited on (secretary-1063), 0.0 when it is working or its readiness cannot be read. A head
    # that finished its turn and went back to its prompt holds a live pid, so this is the only
    # signal that separates it from one that is still thinking.
    worker_idle_since: float = 0.0
    # The idle watchdog only takes a destructive action after two separate dispatcher ticks
    # observe the same aged idle episode.  It replaces the former second, microsecond-adjacent
    # probe: a fresh tick gives a resumed turn a real chance to report busy.
    worker_idle_confirmations: int = 0
    # The one report prompt this round may spend on a confirmed-idle worker (secretary-1172),
    # before the watchdog stops or replaces it. Durable and keyed on the report generation, so the
    # bound survives a restart and belongs to the round rather than to a tick.
    worker_report_nudge: WorkerReportNudge = field(default_factory=WorkerReportNudge)
    # The PO, owner and observer comments this card's live worker was pointed at mid-round
    # (secretary-1768), by the key `worker_comments.comment_key` gives each: its audit event id.
    # Written before the pointer is sent, so a tick repeat or a restarted dispatcher never sends the
    # same comment twice. Only grows; a new round's TASK.md carries every comment anyway.
    worker_comment_deliveries: tuple[str, ...] = ()
    # Durable worker ownership while validation has the checkout. This is deliberately one typed
    # state value rather than four optional fields whose combinations callers would have to infer.
    worker_continuation: WorkerContinuation = field(default_factory=WorkerContinuation)
    # Provider progress during a retained red continuation.  This stays beside the continuation
    # rather than inside its transient delivery stage so the terminal outcome remains auditable
    # after a confirmed stop hands the card to its one replacement head.
    worker_continuation_liveness: WorkerContinuationLiveness = field(
        default_factory=WorkerContinuationLiveness
    )
    # Shadow vitality episodes are telemetry only; `None` means no episode was recorded.
    worker_vitality_episode: VitalityEpisode | None = None
    review_vitality_episode: VitalityEpisode | None = None
    review_waiting_since: float = 0.0
    review_respawns: int = 0
    review_started_at: float = 0.0
    review_progress_at: float = 0.0
    review_idle_since: float = 0.0
    review_idle_confirmations: int = 0
    # Pause (secretary-731): when a freeze stopped this card's worker / reviewer head, 0.0 when it
    # did not. A head with an empty handle is otherwise indistinguishable from one that died, so
    # these are what let the tick log and pause-status say "stopped on purpose". Cleared on resume,
    # by the relaunch or by the decision not to relaunch.
    paused_worker_at: float = 0.0
    paused_reviewer_at: float = 0.0
    # Routing telemetry (secretary-716). attempt_round counts the card's worker rounds: claim opens
    # round 1, every rework bounce (red verdict, red gate) opens the next one. worker_run/review_run
    # are the launch snapshots of the heads currently serving that round, kept here so the verdict
    # record reports the configuration the heads actually started with rather than re-reading a
    # `heads.toml` that may have been edited since. Canon is the journal; this is the live copy.
    attempt_round: int = 0
    # The worker head's own run, as the three head operations keep it (secretary-1412): an identity
    # that survives Orca aliasing its pane handle, the lifecycle it has reached, and — once a stop
    # has begun — who initiated that stop. `worker_run` beside it is the routing snapshot of the
    # configuration the head launched with; this is the state of the head itself, and it is durable
    # for the same reason the pane identity is: the process that spawned a head is not necessarily
    # the process that ends it, and a restarted dispatcher must still be able to say who was
    # ending this one.
    worker_head_run: PersistedHeadRun = field(default_factory=PersistedHeadRun)
    # The reviewer's own run, kept for exactly the same reasons (secretary-1414). The reviewer is
    # the head this dispatcher stops most often and from the most places — a red verdict, a stalled
    # reviewer's respawn, a pipeline freeze, launch recovery, reconciliation — and until it had a
    # run of its own, none of those left a record of who was ending it.
    review_head_run: PersistedHeadRun = field(default_factory=PersistedHeadRun)
    worker_run: PersistedRoutingHeadSnapshot = field(default_factory=PersistedRoutingHeadSnapshot)
    review_run: PersistedRoutingHeadSnapshot = field(default_factory=PersistedRoutingHeadSnapshot)
    # Aborted reviewer bring-ups (issue:aa9a8ae4): consecutive ticks whose reviewer launch came up
    # but could not confirm the worker was frozen, so it handed the pane back as
    # `review-launch-aborted` and kept its intent. This never blocks the card on
    # its own — the head may still be running — so without a bound it repeats silently. Past the
    # stuck ceiling one operator escalation is emitted. Reset the moment a reviewer does take the
    # checkout, so the count covers one stuck episode rather than the card's whole history.
    review_launch_aborts: int = 0
    # Reviewer infrastructure failures over a green candidate (secretary-1401): consecutive ticks
    # whose reviewer bring-up failed outright — a split that would not open, an inventory the
    # runtime would not answer — with no head left behind and nothing said about the candidate. A
    # reviewer that cannot be started is a failure of the review stage, not a verdict on the code,
    # so the card keeps this record: the gate receipt, the candidate SHA, the report round and the
    # held worker session all stay exactly as the green gate left them, and the next tick launches
    # the reviewer again against that same evidence. Only the ceiling blocks the card, for an
    # operator, and `review_infra_error` is what the last attempt failed on. Reset the moment a
    # reviewer does take the checkout, so the count covers one outage rather than the card's life.
    review_infra_failures: int = 0
    review_infra_error: str = ""
    # A reviewer whose first turn ended on a provider error and whose fallback chain had no
    # launchable head (secretary-1799): the card stays in Validate with no reviewer, and this is the
    # visible reason it waits. While it is set, `start_review` walks the chain again on every tick
    # and launches the reviewer on the first head that can run, charging no infrastructure retry.
    # Cleared by that launch.
    review_provider_hold: str = ""
    # Reviewer prompt deliveries this card lost, and the bounded evidence of the last one, from
    # the same delivery boundary the observer's wakes go through. Unlike the counter above these
    # are not reset by a reviewer that later takes the checkout: a card whose first reviewer never
    # received its prompt must still read that way afterwards, which is the whole point of keeping
    # delivery evidence rather than delivery state. Payload size and hash only, never prompt text.
    review_delivery_failures: int = 0
    review_delivery_evidence: PersistedDeliveryEvidence = field(default_factory=PersistedDeliveryEvidence)
    # Same bounded evidence for worker launch, rework and one-turn continuation delivery.  It is
    # retained across recovery so an attempted body/submit pair is never mistaken for an absent
    # prompt when the next tick chooses whether a head may be replaced.
    worker_delivery_failures: int = 0
    worker_delivery_evidence: PersistedDeliveryEvidence = field(default_factory=PersistedDeliveryEvidence)
    # Launch intent is persisted before host creation and cleared after its answer.
    launch_intent: PersistedLaunchIntent = field(default_factory=PersistedLaunchIntent)
    # A card standing in an active execution state with no worker identity and no launch debt
    # (secretary-1544).  Written before the recovery decides, so a tick that cannot finish the
    # decision still leaves the degradation on the record instead of an empty handle that reads
    # as work in progress.  Cleared by the replacement launch that ends the episode.
    worker_headless: PersistedHeadlessRecoveryEpisode = field(
        default_factory=PersistedHeadlessRecoveryEpisode
    )
    # A decision/operation card's hand-over to its sprint's PO session; empty for a headed card.
    po_submission: PoSubmission = field(default_factory=PoSubmission)

    def __setattr__(self, name: str, value: Any) -> None:
        # All producers, including legacy host/dispatcher code that still assigns JSON dictionaries,
        # cross this normalization point. The durable mapping surface remains compatible while the
        # in-memory lifecycle and routing values gain canonical typed views.
        if name in {"worker_head_run", "review_head_run"}:
            value = PersistedHeadRun.from_value(value)
        elif name in {"worker_run", "review_run"}:
            value = PersistedRoutingHeadSnapshot.from_value(value)
        elif name == "gate_attestation":
            value = PersistedGateReceipt.from_value(value)
        elif name == "gate_pr_authorship":
            value = PersistedGatePrAuthorship.from_value(value)
        elif name == "gate_published_ref":
            value = PersistedGatePublishedRef.from_value(value)
        elif name in {"worker_delivery_evidence", "review_delivery_evidence"}:
            value = PersistedDeliveryEvidence.from_value(value)
        elif name == "launch_intent":
            value = PersistedLaunchIntent.from_value(value)
        elif name == "worker_headless":
            value = PersistedHeadlessRecoveryEpisode.from_value(value)
        super().__setattr__(name, value)

    def owns_head(self, role: str | None = None) -> bool:
        """Whether this record still carries an identity that must be settled before replacement."""
        worker = bool(self.handle or self.worker_leaf or self.worker_pid_file)
        review = bool(self.review_handle or self.review_leaf or self.review_pid_file)
        if role == "worker":
            return worker
        if role == "review":
            return review
        return worker or review

    def needs_settling(self) -> bool:
        """Whether reconciliation still owes this record a confirmed stop."""
        return self.owns_head() or bool(self.workspace and not self.workspace_settled)

    def to_json(self) -> dict[str, Any]:
        return {
            "claimed_at": self.claimed_at,
            "activation_recovery": self.activation_recovery.to_json() if self.activation_recovery else None,
            "comment_baseline": self.comment_baseline,
            "gate_pending_since": self.gate_pending_since,
            "gate_state": self.gate_state,
            "gate_attestation": self.gate_attestation.to_json(),
            "gate_attestation_effects": copy.deepcopy(self.gate_attestation_effects),
            "gate_attestation_failure": dict(self.gate_attestation_failure),
            "gate_transport_failures": self.gate_transport_failures,
            "gate_transport_error": self.gate_transport_error,
            "gate_rerun_transport_failures": self.gate_rerun_transport_failures,
            "gate_rerun_transport_error": self.gate_rerun_transport_error,
            "gate_infrastructure_reruns_sha": self.gate_infrastructure_reruns_sha,
            "gate_infrastructure_reruns": self.gate_infrastructure_reruns,
            "gate_infrastructure_rerun_run_id": self.gate_infrastructure_rerun_run_id,
            "gate_infrastructure_rerun_reason": self.gate_infrastructure_rerun_reason,
            "gate_pr_authorship": self.gate_pr_authorship.to_json(),
            "gate_published_ref": self.gate_published_ref.to_json(),
            "handle": self.handle,
            "head": self.head,
            "preferred_head": self.preferred_head,
            "preferred_review_head": self.preferred_review_head,
            "attempt_id": self.attempt_id,
            "attempt_round": self.attempt_round,
            "paused_reviewer_at": self.paused_reviewer_at,
            "paused_worker_at": self.paused_worker_at,
            "report_generation": self.report_generation,
            "worker_local_run_snapshot": dict(self.worker_local_run_snapshot),
            "review_local_run_snapshot": dict(self.review_local_run_snapshot),
            "report_decision": self.report_decision,
            "report_decision_id": self.report_decision_id,
            "report_protocol_prerequisites": list(self.report_protocol_prerequisites),
            "outcome_terminal_path": self.outcome_terminal_path.value,
            "review_baseline": self.review_baseline,
            "review_commit": self.review_commit,
            "previous_reviewed_sha": self.previous_reviewed_sha,
            "previous_blockers": self.previous_blockers,
            "previous_review_id": self.previous_review_id,
            "review_handle": self.review_handle,
            "review_head": self.review_head,
            "review_leaf": self.review_leaf,
            "review_idle_since": self.review_idle_since,
            "review_idle_confirmations": self.review_idle_confirmations,
            "review_progress_at": self.review_progress_at,
            "review_respawns": self.review_respawns,
            "review_started_at": self.review_started_at,
            "review_waiting_since": self.review_waiting_since,
            "rejected_done_reports": self.rejected_done_reports,
            "worker_answer_owed_since": self.worker_answer_owed_since,
            "rejected_failure_class": self.rejected_failure_class,
            "rejected_failure_reason": self.rejected_failure_reason,
            "rejected_sha": self.rejected_sha,
            "state": self.state,
            "worker": self.worker,
            "worker_leaf": self.worker_leaf,
            "worker_pid_file": self.worker_pid_file,
            "review_pid_file": self.review_pid_file,
            "worker_idle_since": self.worker_idle_since,
            "worker_idle_confirmations": self.worker_idle_confirmations,
            "worker_report_nudge": self.worker_report_nudge.to_json(),
            "worker_comment_deliveries": list(self.worker_comment_deliveries),
            "worker_progress_at": self.worker_progress_at,
            "worker_continuation": self.worker_continuation.to_json(),
            "worker_continuation_liveness": self.worker_continuation_liveness.to_json(),
            "worker_vitality_episode": (
                self.worker_vitality_episode.to_json() if self.worker_vitality_episode is not None else None
            ),
            "review_vitality_episode": (
                self.review_vitality_episode.to_json() if self.review_vitality_episode is not None else None
            ),
            "worker_respawns": self.worker_respawns,
            "worker_started_at": self.worker_started_at,
            "worker_head_run": self.worker_head_run.to_json(),
            "review_head_run": self.review_head_run.to_json(),
            "worker_run": self.worker_run.to_json(),
            "review_run": self.review_run.to_json(),
            "review_launch_aborts": self.review_launch_aborts,
            "review_infra_failures": self.review_infra_failures,
            "review_infra_error": self.review_infra_error,
            # Only while a reviewer waits for a provider, so every other record keeps its shape.
            **({"review_provider_hold": self.review_provider_hold} if self.review_provider_hold else {}),
            "review_delivery_failures": self.review_delivery_failures,
            "review_delivery_evidence": self.review_delivery_evidence.to_json(),
            "worker_delivery_failures": self.worker_delivery_failures,
            "worker_delivery_evidence": self.worker_delivery_evidence.to_json(),
            "worker_headless": self.worker_headless.to_json(),
            "launch_intent": self.launch_intent.to_json(),
            "worker_waiting_since": self.worker_waiting_since,
            "workspace": self.workspace,
            "workspace_settled": self.workspace_settled,
            # Only on a PO-executed card's record, so every other record keeps its released shape.
            **({"po_submission": self.po_submission.to_json()} if self.po_submission else {}),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> DispatcherRecord:
        # Refuse obsolete flat continuation fields; interpreting them as absent is unsafe.
        legacy = [
            field_name
            for field_name in (
                "worker_retained_at",
                "worker_resume_delivery",
                "worker_resume_phase",
                "worker_resume_sent_at",
            )
            if field_name in payload
        ]
        if legacy:
            raise DispatcherError(
                "unsupported_legacy_record",
                "unsupported legacy dispatcher record: flat continuation fields "
                f"{', '.join(legacy)}; this release stores the retention under "
                "'worker_continuation'. Let the recorded worker finish or clear the record "
                "before upgrading.",
            )
        state = str(payload.get("state") or "claimed")
        return cls(
            worker=str(payload.get("worker") or ""),
            workspace=str(payload.get("workspace") or ""),
            handle=str(payload.get("handle") or ""),
            head=str(payload.get("head") or ""),
            review_head=str(payload.get("review_head") or ""),
            preferred_head=str(payload.get("preferred_head") or ""),
            preferred_review_head=str(payload.get("preferred_review_head") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            attempt_round=int(payload.get("attempt_round") or 0),
            worker_head_run=PersistedHeadRun.from_value(payload.get("worker_head_run")),
            review_head_run=PersistedHeadRun.from_value(payload.get("review_head_run")),
            worker_run=PersistedRoutingHeadSnapshot.from_value(payload.get("worker_run")),
            review_run=PersistedRoutingHeadSnapshot.from_value(payload.get("review_run")),
            launch_intent=PersistedLaunchIntent.from_value(payload.get("launch_intent")),
            comment_baseline=int(payload.get("comment_baseline") or 0),
            review_baseline=int(payload.get("review_baseline") or 0),
            # A record written before the generation existed carries its round key in
            # `review_baseline`, which is the number the worker in that checkout was handed. Taking
            # it over is what keeps the first generation this dispatcher opens above every id the
            # previous one issued for the round still running.
            report_generation=int(payload.get("report_generation") or payload.get("review_baseline") or 0),
            worker_local_run_snapshot=(
                dict(payload["worker_local_run_snapshot"])
                if isinstance(payload.get("worker_local_run_snapshot"), dict)
                else {}
            ),
            review_local_run_snapshot=(
                dict(payload["review_local_run_snapshot"])
                if isinstance(payload.get("review_local_run_snapshot"), dict)
                else {}
            ),
            report_decision=str(payload.get("report_decision") or ""),
            report_decision_id=str(payload.get("report_decision_id") or ""),
            report_protocol_prerequisites=tuple(
                item
                for item in payload.get("report_protocol_prerequisites", ())
                if isinstance(item, str) and item
            ),
            outcome_terminal_path=outcome_terminal_path(payload.get("outcome_terminal_path"), state=state),
            state=state,
            claimed_at=float(payload.get("claimed_at") or time.time()),
            activation_recovery=ActivationRecovery.from_json(payload.get("activation_recovery")),
            gate_state=str(payload.get("gate_state") or ""),
            gate_pending_since=float(payload.get("gate_pending_since") or 0.0),
            gate_attestation=PersistedGateReceipt.from_value(payload.get("gate_attestation")),
            gate_attestation_effects=copy.deepcopy(payload.get("gate_attestation_effects") or {}),
            gate_attestation_failure=dict(payload.get("gate_attestation_failure") or {}),
            gate_transport_failures=int(payload.get("gate_transport_failures") or 0),
            gate_transport_error=str(payload.get("gate_transport_error") or ""),
            gate_rerun_transport_failures=int(payload.get("gate_rerun_transport_failures") or 0),
            gate_rerun_transport_error=str(payload.get("gate_rerun_transport_error") or ""),
            gate_infrastructure_reruns_sha=str(payload.get("gate_infrastructure_reruns_sha") or ""),
            gate_infrastructure_reruns=int(payload.get("gate_infrastructure_reruns") or 0),
            gate_infrastructure_rerun_run_id=str(payload.get("gate_infrastructure_rerun_run_id") or ""),
            gate_infrastructure_rerun_reason=str(payload.get("gate_infrastructure_rerun_reason") or ""),
            gate_pr_authorship=PersistedGatePrAuthorship.from_value(payload.get("gate_pr_authorship")),
            gate_published_ref=PersistedGatePublishedRef.from_value(payload.get("gate_published_ref")),
            rejected_sha=str(payload.get("rejected_sha") or ""),
            rejected_failure_class=str(payload.get("rejected_failure_class") or "substantive"),
            rejected_failure_reason=str(payload.get("rejected_failure_reason") or ""),
            rejected_done_reports=int(payload.get("rejected_done_reports") or 0),
            worker_answer_owed_since=float(payload.get("worker_answer_owed_since") or 0.0),
            review_handle=str(payload.get("review_handle") or ""),
            review_leaf=str(payload.get("review_leaf") or ""),
            review_commit=str(payload.get("review_commit") or ""),
            previous_reviewed_sha=str(payload.get("previous_reviewed_sha") or ""),
            previous_blockers=str(payload.get("previous_blockers") or ""),
            previous_review_id=str(payload.get("previous_review_id") or ""),
            worker_leaf=str(payload.get("worker_leaf") or ""),
            worker_pid_file=str(payload.get("worker_pid_file") or ""),
            review_pid_file=str(payload.get("review_pid_file") or ""),
            review_launch_aborts=int(payload.get("review_launch_aborts") or 0),
            review_infra_failures=int(payload.get("review_infra_failures") or 0),
            review_infra_error=str(payload.get("review_infra_error") or ""),
            review_provider_hold=str(payload.get("review_provider_hold") or ""),
            review_delivery_failures=int(payload.get("review_delivery_failures") or 0),
            review_delivery_evidence=PersistedDeliveryEvidence.from_value(
                payload.get("review_delivery_evidence")
            ),
            worker_delivery_failures=int(payload.get("worker_delivery_failures") or 0),
            worker_delivery_evidence=PersistedDeliveryEvidence.from_value(
                payload.get("worker_delivery_evidence")
            ),
            worker_headless=PersistedHeadlessRecoveryEpisode.from_value(payload.get("worker_headless")),
            worker_waiting_since=float(payload.get("worker_waiting_since") or 0.0),
            worker_respawns=int(payload.get("worker_respawns") or 0),
            worker_started_at=float(payload.get("worker_started_at") or 0.0),
            worker_progress_at=float(payload.get("worker_progress_at") or 0.0),
            worker_idle_since=float(payload.get("worker_idle_since") or 0.0),
            worker_idle_confirmations=int(payload.get("worker_idle_confirmations") or 0),
            # Absent on every record written before the prompt existed, which is exactly a round
            # that has not spent one: the empty value opens the same single prompt for it.
            worker_report_nudge=WorkerReportNudge.from_json(payload.get("worker_report_nudge")),
            worker_comment_deliveries=tuple(
                item
                for item in payload.get("worker_comment_deliveries") or ()
                if isinstance(item, str) and item
            ),
            worker_continuation=WorkerContinuation.from_json(payload.get("worker_continuation")),
            worker_continuation_liveness=WorkerContinuationLiveness.from_json(
                payload.get("worker_continuation_liveness")
            ),
            # Absence is "no episode yet", not an empty one: a record from before the field
            # existed, or a role whose head has never been observed, carries no claim. A present
            # but damaged payload raises - a corrupt shadow verdict must stop the load rather
            # than be silently dropped, or the record would look observed when it was not.
            worker_vitality_episode=(
                _vitality_episode_from_json(payload["worker_vitality_episode"])
                if payload.get("worker_vitality_episode") is not None
                else None
            ),
            review_vitality_episode=(
                _vitality_episode_from_json(payload["review_vitality_episode"])
                if payload.get("review_vitality_episode") is not None
                else None
            ),
            review_waiting_since=float(payload.get("review_waiting_since") or 0.0),
            review_respawns=int(payload.get("review_respawns") or 0),
            review_started_at=float(payload.get("review_started_at") or 0.0),
            review_progress_at=float(payload.get("review_progress_at") or 0.0),
            review_idle_since=float(payload.get("review_idle_since") or 0.0),
            review_idle_confirmations=int(payload.get("review_idle_confirmations") or 0),
            paused_worker_at=float(payload.get("paused_worker_at") or 0.0),
            paused_reviewer_at=float(payload.get("paused_reviewer_at") or 0.0),
            workspace_settled=bool(payload.get("workspace_settled", False)),
            po_submission=PoSubmission.from_json(payload.get("po_submission")),
        )


def _vitality_episode_from_json(payload: Any) -> VitalityEpisode:
    """Load one persisted vitality episode, importing its module lazily.

    See the import note at the top of this module: the episode vocabulary sits above
    ``dispatcher_watchdog`` in the dependency order, so the record reads it at call time instead
    of at load time.
    """
    from ummanu.dispatch.head_vitality_episode import VitalityEpisode

    return VitalityEpisode.from_json(payload)


def now_rfc3339() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_attempt_id() -> str:
    return f"attempt-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:12]}"


def record_attempt(
    payload: dict[str, Any],
    attempt_id: str,
    reference: str,
    actor: str,
    owner: str,
) -> None:
    attempts = payload.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        payload["attempts"] = attempts
    if any(isinstance(attempt, dict) and attempt.get("attempt_id") == attempt_id for attempt in attempts):
        return
    attempts.append(
        {
            "attempt_id": attempt_id,
            "pilot_ref": reference,
            "owner": owner,
            "started_at": now_rfc3339(),
            "started_by": actor,
        }
    )


def attempt_request_id(attempt_id: str, action: str, reference: str, suffix: str = "") -> str:
    parts = ["dispatcher", request_token(attempt_id or "attempt-missing"), action, reference]
    if suffix:
        parts.append(suffix)
    return "-".join(request_token(part) for part in parts)


def request_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return token or "empty"


def claim_mismatch(
    task: dict[str, Any],
    worker: str,
    resolved_head: str,
    resolved_review_head: str,
) -> list[str]:
    mismatches = []
    if task.get("state") != "in_progress":
        mismatches.append("state")
    if task.get("claim", {}).get("worker") != worker:
        mismatches.append("worker")
    routing = task.get("routing", {})
    if routing.get("resolved_worker_head") != resolved_head:
        mismatches.append("resolved_head")
    if routing.get("resolved_review_head") != resolved_review_head:
        mismatches.append("resolved_review_head")
    return mismatches


def claim_actual(task: dict[str, Any]) -> dict[str, Any]:
    routing = task.get("routing", {})
    return {
        "state": task.get("state"),
        "worker": task.get("claim", {}).get("worker"),
        "resolved_head": routing.get("resolved_worker_head"),
        "resolved_review_head": routing.get("resolved_review_head"),
    }


def record_divergence(
    payload: dict[str, Any],
    attempt_id: str,
    reference: str,
    step: str,
    reason: str,
    *,
    expected: dict[str, Any],
    actual: dict[str, Any],
    details: list[str],
) -> dict[str, Any]:
    divergences = payload.setdefault("controlled_divergences", [])
    if not isinstance(divergences, list):
        divergences = []
        payload["controlled_divergences"] = divergences
    divergence = {
        "id": f"div_{uuid.uuid4().hex[:16]}",
        "at": now_rfc3339(),
        "attempt_id": attempt_id,
        "pilot_ref": reference,
        "step": step,
        "reason": reason,
        "expected": expected,
        "actual": actual,
        "details": details,
        # Opening rule: every divergence starts open. Closing rule lives with the
        # production tick (see `_reconcile_production` in dispatch/production.py):
        # a divergence closes once its card leaves the active dispatcher cycle
        # (in_progress/validate), whatever state it lands in. A divergence with no
        # "status" is a pre-existing record from before this field existed and is
        # treated as open.
        "status": "open",
    }
    divergences.append(divergence)
    return divergence


def divergence_is_open(divergence: dict[str, Any]) -> bool:
    return divergence.get("status") != "closed"


def close_divergence(divergence: dict[str, Any], reason: str) -> None:
    divergence["status"] = "closed"
    divergence["closed_at"] = now_rfc3339()
    divergence["closed_reason"] = reason
