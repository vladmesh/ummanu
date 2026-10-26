"""Immutable, backend-neutral values used by the board protocol.

The classes in this module are deliberately values rather than views of a
board row.  Adapters are responsible for translating their backend rows
before one of these objects crosses the protocol boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, TypeAlias

from ummanu.board.attempt_outcome import (
    ATTEMPT_OUTCOME_BLOCKED_REASONS as ATTEMPT_OUTCOME_BLOCKED_REASONS,
    ATTEMPT_OUTCOME_COMPLETENESS as ATTEMPT_OUTCOME_COMPLETENESS,
    ATTEMPT_OUTCOME_DISPOSITIONS as ATTEMPT_OUTCOME_DISPOSITIONS,
    ATTEMPT_OUTCOME_VERDICTS as ATTEMPT_OUTCOME_VERDICTS,
    validate_attempt_outcome_data,
)
from ummanu.board.marker_payload import REPORT_STATUSES, check_report_fields
from ummanu.board.roles import Role
from ummanu.board.sql_rows import parse_rfc3339, rfc3339
from ummanu.board.terminal_taxonomy import (
    BLOCKED_REASONS as BLOCKED_REASONS,
    TERMINAL_DISPOSITIONS as TERMINAL_DISPOSITIONS,
    read_terminal_taxonomy,
)

EntityRef: TypeAlias = str


class EntityKind(StrEnum):
    PRODUCT = "product"
    ISSUE = "issue"
    SPRINT = "sprint"
    CARD = "card"


class EventKind(StrEnum):
    """Every event kind the normalized board protocol may persist."""

    ENTITY_CREATED = "entity.created"
    ENTITY_UPDATED = "entity.updated"
    PRODUCT_ARCHIVED = "product.archived"
    ISSUE_CLOSED = "issue.closed"
    ISSUE_REOPENED = "issue.reopened"
    SPRINT_CLOSED = "sprint.closed"
    SPRINT_STOPPED = "sprint.stopped"
    SPRINT_REOPENED = "sprint.reopened"
    CARD_READY = "card.readied"
    CARD_STARTED = "card.started"
    CARD_SUBMITTED = "card.submitted"
    CARD_ASSESSED = "card.assessed"
    CARD_REWORKED = "card.reworked"
    CARD_RELEASED = "card.released"
    CARD_BLOCKED = "card.blocked"
    CARD_UNBLOCKED = "card.unblocked"
    CARD_RETURNED = "card.returned"
    CARD_MOVED = "card.moved"
    # Control-plane marker occurrences are Card facts too.  Their marker
    # comments are a rendering of the data carried here, not a second source
    # of the report, verdict, or observer decision.
    CARD_REPORTED = "card.reported"
    CARD_VERDICTED = "card.verdict"
    CARD_DECIDED = "card.decided"
    # A refused rework is a durable Card fact: it names the registry boundary that prevented an
    # observer instruction becoming authoritative, without inventing a worker outcome.
    CARD_DECISION_REFUSED = "card.decision_refused"
    # What one completed worker or review phase cost, read from the provider's own structured
    # records at the moment the phase ended.  It is a Card fact with no backend mutation: the
    # journal is the only place a finished phase's token counts are durable at all.
    ATTEMPT_USAGE = "attempt.usage"
    # A completed attempt's final lifecycle disposition.  This is deliberately
    # an observation, never an edge the lifecycle reader may consume.
    ATTEMPT_OUTCOME = "attempt.outcome"


class AttemptUsageOutcome(StrEnum):
    """How the collection of one phase's provider usage ended.

    Exactly one value says the counts are real. Every other value is a named degradation, so a
    reader never has to tell "this phase cost nothing" apart from "nobody could read what it cost".
    """

    COLLECTED = "collected"
    # The provider read succeeded, but its derived session total is below an immutable earlier
    # phase boundary. Recording a zero interval would disguise contradictory accounting evidence.
    ARITHMETIC_CONTRADICTION = "arithmetic_contradiction"
    # The head ran on an adapter that publishes no structured usage records at all.
    ADAPTER_UNSUPPORTED = "adapter_unsupported"
    # The run never bound a provider session identity, so no journal belongs to it.
    SESSION_UNAVAILABLE = "session_unavailable"
    # The session is identified but its structured record source was never bound to this run.
    SOURCE_UNAVAILABLE = "source_unavailable"
    # The bound source exists and could not be read: permissions, a removed file, an I/O error.
    SOURCE_UNREADABLE = "source_unreadable"
    # The source was read and its structured usage records were not usable: a record that declared
    # itself a usage record and carried a schema this adapter does not publish, or a journal in
    # which nothing parsed at all. Distinct from USAGE_ABSENT, which is a journal that parsed.
    RECORDS_MALFORMED = "records_malformed"
    # The source parsed and carries no usage record for this phase.
    USAGE_ABSENT = "usage_absent"


# The five dimensions a phase is accounted in. A provider that reports none of a dimension leaves
# it null: an absent dimension is never written down as a zero, because zero is a real count.
TOKEN_DIMENSIONS = ("input", "cache_input", "cache_read_input", "output", "reasoning")

# The three accounts one occurrence carries, each in the same five dimensions: what this phase
# owns, the running session total it ended at, and the durable boundary it started from.
ATTEMPT_USAGE_ACCOUNTS = ("tokens", "session_totals", "phase_baseline")

ATTEMPT_USAGE_ROLES = ("worker", "reviewer")
ATTEMPT_USAGE_PHASES = ("worker", "review")


class ProductState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class IssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class IssueKind(StrEnum):
    BUG = "bug"
    FEATURE = "feature"
    QUESTION = "question"
    IMPROVEMENT = "improvement"


class IssuePriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class IssueCloseReason(StrEnum):
    RESOLVED = "resolved"
    INVALID = "invalid"
    DUPLICATE = "duplicate"
    WONT_DO = "wont_do"


class SprintState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    STOPPED = "stopped"


class CardState(StrEnum):
    ISSUES = "issues"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    VALIDATE = "validate"
    CI = "ci"
    REVIEW = "review"
    ASSESSMENT = "assessment"
    POST_MERGE_CI = "post_merge_ci"
    BLOCKED = "blocked"
    DONE = "done"


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class Product:
    ref: EntityRef
    title: str
    state: ProductState = ProductState.ACTIVE
    projects: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        _non_empty(self.ref, "product ref")
        _non_empty(self.title, "product title")

    @property
    def kind(self) -> EntityKind:
        return EntityKind.PRODUCT


@dataclass(frozen=True, slots=True, init=False)
class Issue:
    ref: EntityRef
    title: str
    product_ref: EntityRef
    state: IssueState
    priority: IssuePriority | None
    issue_kind: IssueKind | None
    description: str
    close_reason: IssueCloseReason | None

    def __init__(
        self,
        ref: EntityRef,
        title: str,
        product_ref: EntityRef,
        state: IssueState = IssueState.OPEN,
        priority: IssuePriority | str | None = None,
        issue_kind: IssueKind | str | None = None,
        description: str = "",
        close_reason: IssueCloseReason | str | None = None,
    ) -> None:
        # A staged board create historically exposes ``pending/pending`` before its metadata
        # finish step.  Keep accepting that recovery-only wire shape, but normalize it to absence
        # rather than polluting either closed vocabulary with a value that can never be persisted.
        if priority == "pending" or issue_kind == "pending":
            if priority != "pending" or issue_kind != "pending":
                raise ValueError("incomplete Issue vocabulary must omit both priority and kind")
            priority = None
            issue_kind = None
        if priority == "":
            priority = None
        if issue_kind == "":
            issue_kind = None
        if close_reason == "":
            close_reason = None
        try:
            normalized_priority = None if priority is None else IssuePriority(priority)
            normalized_kind = None if issue_kind is None else IssueKind(issue_kind)
            normalized_close_reason = None if close_reason is None else IssueCloseReason(close_reason)
        except ValueError as exc:
            raise ValueError(f"invalid Issue vocabulary: {exc}") from None
        object.__setattr__(self, "ref", ref)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "product_ref", product_ref)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "priority", normalized_priority)
        object.__setattr__(self, "issue_kind", normalized_kind)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "close_reason", normalized_close_reason)
        self.__post_init__()

    def __post_init__(self) -> None:
        _non_empty(self.ref, "issue ref")
        _non_empty(self.title, "issue title")
        _non_empty(self.product_ref, "issue product ref")

    @property
    def kind(self) -> EntityKind:
        return EntityKind.ISSUE


@dataclass(frozen=True, slots=True)
class Sprint:
    ref: EntityRef
    goal: str
    state: SprintState = SprintState.OPEN
    product_ref: EntityRef | None = None
    issue_refs: tuple[EntityRef, ...] = ()
    card_refs: tuple[EntityRef, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.ref, "sprint ref")
        if not isinstance(self.goal, str):
            raise ValueError("sprint goal must be a string")  # noqa: TRY004 - model validation refuses with ValueError

    @property
    def kind(self) -> EntityKind:
        return EntityKind.SPRINT


@dataclass(frozen=True, slots=True)
class Card:
    ref: EntityRef
    title: str
    state: CardState
    sprint_ref: EntityRef | None = None
    issue_refs: tuple[EntityRef, ...] = ()
    product_ref: EntityRef | None = None
    description: str = ""

    def __post_init__(self) -> None:
        _non_empty(self.ref, "card ref")
        _non_empty(self.title, "card title")

    @property
    def kind(self) -> EntityKind:
        return EntityKind.CARD


BoardEntity: TypeAlias = Product | Issue | Sprint | Card


@dataclass(frozen=True, slots=True, init=False)
class Actor:
    """The protocol identity that made a board mutation."""

    role: Role
    id: str
    head_run_ref: str | None = None

    def __init__(self, role: Role | str, id: str, head_run_ref: str | None = None) -> None:
        try:
            normalized_role = Role(role)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid actor role: {role!r}") from exc
        object.__setattr__(self, "role", normalized_role)
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "head_run_ref", head_run_ref)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            raise ValueError("actor role must be a Role")  # noqa: TRY004 - model validation refuses with ValueError
        _non_empty(self.id, "actor id")
        if self.head_run_ref is not None:
            _non_empty(self.head_run_ref, "actor head-run ref")


@dataclass(frozen=True, slots=True)
class RelatedRefs:
    """Cross-entity links carried by a mutation and its event."""

    refs: tuple[EntityRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.refs, tuple):
            raise ValueError("related refs must be a tuple")  # noqa: TRY004 - model validation refuses with ValueError
        unique: list[EntityRef] = []
        for ref in self.refs:
            _non_empty(ref, "related ref")
            if ref not in unique:
                unique.append(ref)
        object.__setattr__(self, "refs", tuple(unique))


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable, schema-versioned board protocol occurrence.

    The journal representation is deliberately a compact top-level document so
    generic audit readers can still locate its request id and subject ref.
    ``subject`` remains explicit in the durable shape, rather than requiring a
    future reader to infer it from an event-kind prefix.
    """

    RECORD_TYPE: ClassVar[str] = "board.protocol_event"
    SCHEMA_VERSION: ClassVar[int] = 2

    event_id: str
    kind: EventKind
    entity_kind: EntityKind
    ref: EntityRef
    actor: Actor
    reason: str
    occurred_at: datetime
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    # Lifecycle data is optional so the v2 canon remains able to read the
    # additive events introduced before Card transitions migrated.  A Card
    # transition, however, carries both values: recovery must be able to prove
    # the precise backend effect instead of inferring it from a broad event
    # kind such as ``card.moved``.
    source_state: str | None = None
    target_state: str | None = None
    # Additive operation evidence.  Product/Issue creates have follow-up
    # metadata writes, so recovery needs the normalized intended value rather
    # than a second, private journal to complete or prove the occurrence.
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty(self.event_id, "event id")
        if not isinstance(self.kind, EventKind):
            raise ValueError("event kind must be an EventKind")  # noqa: TRY004 - model validation refuses with ValueError
        if not isinstance(self.entity_kind, EntityKind):
            raise ValueError("event entity kind must be an EntityKind")  # noqa: TRY004 - model validation refuses with ValueError
        _non_empty(self.ref, "event ref")
        _non_empty(self.reason, "event reason")
        if not isinstance(self.actor, Actor):
            raise ValueError("event actor must be an Actor")  # noqa: TRY004 - model validation refuses with ValueError
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise ValueError("event occurred_at must be timezone-aware")
        if not isinstance(self.related_refs, RelatedRefs):
            raise ValueError("event related refs must be RelatedRefs")  # noqa: TRY004 - model validation refuses with ValueError
        if (self.source_state is None) != (self.target_state is None):
            raise ValueError("event transition requires both source and target states")
        if self.source_state is not None:
            _non_empty(self.source_state, "event transition source")
            _non_empty(self.target_state or "", "event transition target")
        if not isinstance(self.data, dict):
            raise ValueError("event data must be an object")  # noqa: TRY004 - model validation refuses with ValueError
        _validate_control_marker_event(self.kind, self.entity_kind, self.reason, self.data)
        _validate_attempt_usage_event(self.kind, self.entity_kind, self.data)
        _validate_attempt_outcome_event(self.kind, self.entity_kind, self.data)
        _validate_terminal_taxonomy_event(self.entity_kind, self.target_state, self.data)
        # The journal spelling is UTC.  Keep the value canonical too, so an
        # event read back from its own record compares equal to the value that
        # was written, even when its caller supplied another aware timezone.
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))

    def to_record(self, request_id: str) -> dict[str, Any]:
        """Serialize a canonical, deterministic audit record."""
        _non_empty(request_id, "event request id")
        actor: dict[str, str] = {"role": self.actor.role, "id": self.actor.id}
        if self.actor.head_run_ref is not None:
            actor["head_run_ref"] = self.actor.head_run_ref
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "record_type": self.RECORD_TYPE,
            "request_id": request_id,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "subject": {"kind": self.entity_kind.value, "ref": self.ref},
            # Keep the old generic query surface useful without making it the
            # protocol representation of the subject.
            "ref": self.ref,
            "occurred_at": _format_time(self.occurred_at),
            "actor": actor,
            "reason": self.reason,
            "related_refs": list(self.related_refs.refs),
            "data": self.data,
        }
        if self.source_state is not None:
            record["transition"] = {"source": self.source_state, "target": self.target_state}
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Event:
        """Read and validate one protocol record, rejecting generic audit rows."""
        if not isinstance(record, dict):
            raise ValueError("board event record must be an object")  # noqa: TRY004 - model validation refuses with ValueError
        if record.get("record_type") != cls.RECORD_TYPE:
            raise ValueError("record is not a board protocol event")
        if record.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("unsupported board event schema version")
        subject = record.get("subject")
        actor = record.get("actor")
        related = record.get("related_refs")
        lifecycle = record.get("transition")
        data = record.get("data", {})
        if not isinstance(subject, dict):
            raise ValueError("board event subject must be an object")  # noqa: TRY004 - model validation refuses with ValueError
        if not isinstance(actor, dict):
            raise ValueError("board event actor must be an object")  # noqa: TRY004 - model validation refuses with ValueError
        if not isinstance(related, list) or not all(isinstance(ref, str) for ref in related):
            raise ValueError("board event related_refs must be a string list")
        if len(set(related)) != len(related):
            raise ValueError("board event related_refs must be deduplicated")
        if not isinstance(data, dict):
            raise ValueError("board event data must be an object")  # noqa: TRY004 - model validation refuses with ValueError
        if lifecycle is not None and (
            not isinstance(lifecycle, dict)
            or not isinstance(lifecycle.get("source"), str)
            or not isinstance(lifecycle.get("target"), str)
        ):
            raise ValueError("board event transition must have string source and target")
        try:
            event = cls(
                event_id=_string_field(record, "event_id"),
                kind=EventKind(_string_field(record, "kind")),
                entity_kind=EntityKind(_string_field(subject, "kind")),
                ref=_string_field(subject, "ref"),
                actor=Actor(
                    _string_field(actor, "role"),
                    _string_field(actor, "id"),
                    _optional_string_field(actor, "head_run_ref"),
                ),
                reason=_string_field(record, "reason"),
                occurred_at=_parse_time(_string_field(record, "occurred_at")),
                related_refs=RelatedRefs(tuple(related)),
                source_state=lifecycle.get("source") if isinstance(lifecycle, dict) else None,
                target_state=lifecycle.get("target") if isinstance(lifecycle, dict) else None,
                data=data,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid board event record: {exc}") from None
        legacy_ref = record.get("ref")
        if legacy_ref is not None and legacy_ref != event.ref:
            raise ValueError("board event ref disagrees with subject ref")
        _non_empty(_string_field(record, "request_id"), "event request id")
        return event


def _string_field(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise ValueError(f"board event {name} must be a string")  # noqa: TRY004 - model validation refuses with ValueError
    return value


def _optional_string_field(document: dict[str, Any], name: str) -> str | None:
    value = document.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"board event {name} must be a string")  # noqa: TRY004 - model validation refuses with ValueError
    return value


def _validate_control_marker_event(
    kind: EventKind,
    entity_kind: EntityKind,
    reason: str,
    data: dict[str, Any],
) -> None:
    """Keep the three declared marker kinds complete at the typed boundary."""
    if kind is EventKind.CARD_DECISION_REFUSED:
        if entity_kind is not EntityKind.CARD:
            raise ValueError("Card decision refusal requires a Card subject")
        required = {
            "decision",
            "code",
            "artifact",
            "artifact_owner",
            "requested_role",
            "specification_revision",
            "protocol_prerequisites",
        }
        if set(data) != required:
            raise ValueError("Card decision refusal has an unsupported payload")
        if data.get("decision") != "rework" or data.get("code") != "artifact_ownership_violation":
            raise ValueError("Card decision refusal has an unsupported reason")
        for field in ("artifact", "artifact_owner", "requested_role"):
            value = data.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Card decision refusal needs {field}")
        revision = data.get("specification_revision")
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise ValueError("Card decision refusal needs a specification revision or null")
        prerequisites = data.get("protocol_prerequisites")
        if not isinstance(prerequisites, list) or not prerequisites or any(
            not isinstance(value, str) or not value for value in prerequisites
        ):
            raise ValueError("Card decision refusal needs declared protocol prerequisites")
        return
    marker_kinds = {
        EventKind.CARD_REPORTED,
        EventKind.CARD_VERDICTED,
        EventKind.CARD_DECIDED,
    }
    if kind not in marker_kinds:
        return
    if entity_kind is not EntityKind.CARD:
        raise ValueError("control-plane marker events require a Card subject")
    marker = data.get("marker")
    body = data.get("body")
    if not isinstance(marker, str) or not marker or not isinstance(body, str) or not body.strip():
        raise ValueError("control-plane marker events require a non-empty marker and body")
    if reason != body:
        raise ValueError("control-plane marker event reason must match its body")
    if kind is EventKind.CARD_REPORTED:
        status = data.get("status")
        if status not in REPORT_STATUSES or marker != f"report:{status}":
            raise ValueError("Card report event has an unsupported marker payload")
        check_report_fields(status, data.get("classification"), data.get("partial_commit"))
        return
    if kind is EventKind.CARD_VERDICTED:
        status = data.get("status")
        if status not in {"green", "red"} or marker != f"review:{status}":
            raise ValueError("Card verdict event has an unsupported marker payload")
        return
    decision = data.get("decision")
    if decision not in {"accept", "rework", "reslice"} or marker != f"decision:{decision}":
        raise ValueError("Card decision event has an unsupported marker payload")
    # The field was added after `card.decided` was released. Historical records declare no
    # protocol prerequisite, while every newly written value remains shape-checked below.
    prerequisites = data.get("protocol_prerequisites", [])
    if not isinstance(prerequisites, list) or any(not isinstance(value, str) or not value for value in prerequisites):
        raise ValueError("Card decision event has invalid protocol prerequisites")
    if len(set(prerequisites)) != len(prerequisites):
        raise ValueError("Card decision event has duplicate protocol prerequisites")


def check_attempt_usage_resolved(model: Any, models: Any, effort: Any) -> None:
    """The resolved-model fields of an ``attempt.usage`` occurrence, one rule for both boundaries."""
    shaped = (
        isinstance(model, str)
        and isinstance(effort, str)
        and isinstance(models, (list, tuple))
        and all(isinstance(item, str) and item.strip() for item in models)
    )
    if not shaped:
        raise ValueError(
            "attempt usage resolved_model and resolved_effort are strings and resolved_models "
            "a list of non-empty model ids"
        )
    if len(set(models)) != len(models):
        raise ValueError("attempt usage resolved_models names each model once")
    if model != (models[-1] if models else ""):
        raise ValueError("attempt usage resolved_model is the last of resolved_models")


def _validate_attempt_usage_event(
    kind: EventKind,
    entity_kind: EntityKind,
    data: dict[str, Any],
) -> None:
    """Keep an ``attempt.usage`` occurrence self-contained at the typed boundary.

    The point of this event is that a reader never has to reopen a provider session file, so the
    identity it binds — which round, which role, which head configuration, which provider session —
    is a precondition of writing it rather than a convention its writers happen to follow.
    """
    if kind is not EventKind.ATTEMPT_USAGE:
        return
    if entity_kind is not EntityKind.CARD:
        raise ValueError("attempt usage events require a Card subject")
    attempt = data.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("attempt usage events require a positive attempt number")
    generation = data.get("report_generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("attempt usage events require a positive report generation")
    for name in ("attempt_id", "adapter"):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"attempt usage events require a non-empty {name}")
    if data.get("role") not in ATTEMPT_USAGE_ROLES:
        raise ValueError("attempt usage role must be one of " + ", ".join(ATTEMPT_USAGE_ROLES))
    if data.get("phase") not in ATTEMPT_USAGE_PHASES:
        raise ValueError("attempt usage phase must be one of " + ", ".join(ATTEMPT_USAGE_PHASES))
    model = data.get("model")
    model_source = data.get("model_source")
    # A model may legitimately be empty — an unpinned profile lets the CLI resolve one — but only
    # under a source that says so, which is exactly the routing journal's own rule.
    if not isinstance(model, str) or not isinstance(model_source, str) or not model_source:
        raise ValueError("attempt usage events carry a model string and where it was resolved")
    # Added after release: an occurrence written before them carries none of the three.
    if any(name in data for name in ("resolved_model", "resolved_models", "resolved_effort")):
        check_attempt_usage_resolved(
            data.get("resolved_model"), data.get("resolved_models"), data.get("resolved_effort")
        )
    session_id = data.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise ValueError("attempt usage session id must be a non-empty string or null")
    if session_id is None and not str(data.get("session_id_reason") or "").strip():
        # A typed absence is a fact; a blank one is a reader guessing.
        raise ValueError("an absent attempt usage session id must record why it is absent")
    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in set(AttemptUsageOutcome):
        raise ValueError("attempt usage events require a declared collection outcome")
    collected = outcome == AttemptUsageOutcome.COLLECTED
    # The interval this phase owns, the session total it ended at, and the boundary it started
    # from. The last two are what makes the interval checkable and what the next phase on the same
    # provider session subtracts, so they are as much a part of the occurrence as the interval.
    for account in ATTEMPT_USAGE_ACCOUNTS:
        counts = data.get(account)
        if not isinstance(counts, dict) or set(counts) != set(TOKEN_DIMENSIONS):
            raise ValueError(f"attempt usage {account} carry exactly the declared token dimensions")
        for name, value in counts.items():
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"attempt usage {account} dimension {name} must be a non-negative integer or null"
                )
        if not collected and any(value is not None for value in counts.values()):
            raise ValueError("a degraded attempt usage outcome reports no token totals")
        # `reasoning` is a contained subset of the inclusive `output`, in whichever accounts happen
        # to know both. It is a property of each account on its own, so it holds for a historical
        # all-three-or-none occurrence and for an independently attributed one alike.
        comparable = counts["output"] is not None and counts["reasoning"] is not None
        if comparable and counts["reasoning"] > counts["output"]:
            raise ValueError(f"attempt usage {account} reasoning must be contained in its output")
    if collected:
        tokens = data["tokens"]
        totals = data["session_totals"]
        baseline = data["phase_baseline"]
        if all(value is None for value in totals.values()):
            raise ValueError("a collected attempt usage outcome reports at least one session total")
        # The three accounts are nullable per dimension and independently of each other, because a
        # provider dimension can be absent from a predecessor and present here, or the reverse. What
        # is *attributed* is still checkable: a dimension this phase owns names the boundary it was
        # measured from and the total it was measured to. A dimension it does not own says so in both
        # `tokens` and `phase_baseline` and never as a zero, while `session_totals` keeps whatever the
        # provider did report so the next phase on this session has a boundary to subtract from.
        for name in TOKEN_DIMENSIONS:
            owned, total, start = tokens[name], totals[name], baseline[name]
            if owned is None and start is None:
                continue
            if owned is None or start is None:
                raise ValueError(
                    f"attempt usage dimension {name} attributes its interval and its baseline together"
                )
            if total is None:
                raise ValueError(
                    f"attempt usage dimension {name} attributes an interval with no session total"
                )
            if total < start:
                raise ValueError(f"attempt usage dimension {name} has a session total below its baseline")
            expected = total - start
            if owned != expected:
                raise ValueError(f"attempt usage dimension {name} does not match its session-total interval")


def _validate_attempt_outcome_event(
    kind: EventKind,
    entity_kind: EntityKind,
    data: dict[str, Any],
) -> None:
    """Retain the Event kind/subject boundary before checking shared outcome facts."""
    if kind is not EventKind.ATTEMPT_OUTCOME:
        return
    if entity_kind is not EntityKind.CARD:
        raise ValueError("attempt outcome events require a Card subject")
    validate_attempt_outcome_data(data)


def _validate_terminal_taxonomy_event(
    entity_kind: EntityKind, target_state: str | None, data: dict[str, Any]
) -> None:
    """Keep optional forward taxonomy data at the ordinary typed-event boundary."""
    if "terminal_taxonomy" not in data:
        return
    if entity_kind is not EntityKind.CARD or target_state is None:
        raise ValueError("terminal taxonomy requires a Card transition")
    raw = data["terminal_taxonomy"]
    if not isinstance(raw, dict):
        raise ValueError("terminal taxonomy must be an object")  # noqa: TRY004 - model validation refuses with ValueError
    read_terminal_taxonomy({"terminal_taxonomy": raw}, disposition=str(raw.get("disposition") or ""))


def _format_time(value: datetime) -> str:
    """An event's `occurred_at`, kept to the microsecond: events of one second order by it (CON-08)."""
    return rfc3339(value, microseconds=True)


def _parse_time(value: str) -> datetime:
    parsed = parse_rfc3339(value)
    if parsed is None:
        raise ValueError("board event occurred_at must be RFC3339")
    if parsed.tzinfo is None:
        raise ValueError("board event occurred_at must be timezone-aware")
    return parsed.astimezone(UTC)
