"""Typed payload boundary for the closed ``attempt.outcome`` board-event schema."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ummanu.board.terminal_taxonomy import (
    BLOCKED_REASONS,
    TERMINAL_DISPOSITIONS,
    BlockedReason,
    TerminalDisposition,
)


class AttemptOutcomeTerminalState(StrEnum):
    """Terminal board state observed for one sealed attempt."""

    DONE = "done"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"


class AttemptOutcomeVerdict(StrEnum):
    """Review/report verdict projected into the attempt outcome ledger."""

    GREEN = "green"
    RED = "red"
    BLOCKED = "blocked"
    MISSING = "missing"
    LEGACY = "legacy"


class AttemptOutcomeCompleteness(StrEnum):
    """Whether one role's provider-usage evidence is complete for the attempt."""

    COLLECTED = "collected"
    DEGRADED = "degraded"
    MISSING = "missing"
    LEGACY = "legacy"


ATTEMPT_OUTCOME_VERDICTS = tuple(value.value for value in AttemptOutcomeVerdict)
ATTEMPT_OUTCOME_DISPOSITIONS = TERMINAL_DISPOSITIONS
ATTEMPT_OUTCOME_BLOCKED_REASONS = BLOCKED_REASONS
ATTEMPT_OUTCOME_COMPLETENESS = tuple(value.value for value in AttemptOutcomeCompleteness)
_TERMINAL_STATES = frozenset(value.value for value in AttemptOutcomeTerminalState)
_VERSIONS = (1, 2)
_USAGE_ROLES = ("worker", "review")

_SOURCE_EVENT_FIELDS = (
    "report",
    "verdict",
    "decision",
    "effect",
    "worker_usage",
    "review_usage",
)
_LINEAGE_FIELDS = (
    "specification_revision",
    "report",
    "verdict",
    "decision",
    "effect",
    "worker_usage",
    "review_usage",
)
_BASE_FIELDS = {
    "version",
    "attempt_id",
    "attempt",
    "report_generation",
    "sprint_ref",
    "specification_revision",
    "terminal_state",
    "verdict",
    "disposition",
    "blocked_reason",
    "source_event_ids",
    "usage_completeness",
}


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_non_empty(value: str | None, field_name: str) -> None:
    if value is not None and not _is_non_empty_string(value):
        raise ValueError(f"attempt outcome {field_name} must be a non-empty string or null")


def _validate_field_set(data: Mapping[str, Any], fields: Iterable[str], message: str) -> None:
    if set(data) != set(fields):
        raise ValueError(message)


def _validate_schema(data: Mapping[str, Any]) -> int:
    version = data.get("version")
    if version not in _VERSIONS:
        raise ValueError("unsupported attempt outcome version")
    fields = _BASE_FIELDS | {"lineage_required"} if version == 2 else _BASE_FIELDS
    _validate_field_set(data, fields, f"attempt outcome v{version} has an unsupported field set")
    return version


def _validate_scalars(
    attempt_id: str,
    attempt: int,
    report_generation: int,
    sprint_ref: str | None,
    specification_revision: str | None,
) -> None:
    if not _is_non_empty_string(attempt_id):
        raise ValueError("attempt outcome requires a non-empty attempt_id")
    for name, value in (("attempt", attempt), ("report_generation", report_generation)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"attempt outcome requires a positive {name}")
    _optional_non_empty(sprint_ref, "sprint_ref")
    _optional_non_empty(specification_revision, "specification_revision")


def _validate_choice(value: Any, choices: Iterable[str], message: str) -> None:
    if value not in choices:
        raise ValueError(message)


def _validate_disposition(disposition: str, blocked_reason: str | None) -> None:
    _validate_choice(disposition, ATTEMPT_OUTCOME_DISPOSITIONS, "attempt outcome disposition is unsupported")
    if blocked_reason is not None:
        _validate_choice(
            blocked_reason, ATTEMPT_OUTCOME_BLOCKED_REASONS, "attempt outcome blocked reason is unsupported"
        )
    if (disposition == "blocked") != (blocked_reason is not None):
        raise ValueError("attempt outcome blocked reason is present exactly for blocked disposition")


def _validate_usage_link(state: str, usage_ref: str | None, role: str) -> None:
    if state in {"collected", "degraded"} and usage_ref is None:
        raise ValueError(f"attempt outcome {role} completeness requires its usage event ref")
    if state in {"missing", "legacy"} and usage_ref is not None:
        raise ValueError(f"attempt outcome {role} missingness carries no usage event ref")


def _validate_lineage_values(values: Iterable[bool]) -> None:
    if not all(isinstance(value, bool) for value in values):
        raise ValueError("attempt outcome lineage requiredness must be boolean")


def validate_attempt_outcome_data(data: dict[str, Any]) -> None:
    """Check raw Event facts in their released order, retaining dict-only nested shapes.

    Parsing typed values has a different order and error framing. Neither adapter
    normalizes raw values or serializes typed objects to cross the other boundary.
    """
    version = _validate_schema(data)
    _validate_scalars(
        data["attempt_id"],
        data["attempt"],
        data["report_generation"],
        data["sprint_ref"],
        data["specification_revision"],
    )
    _validate_choice(
        data["terminal_state"],
        _TERMINAL_STATES,
        "attempt outcome terminal state must be done, blocked or in_progress",
    )
    _validate_choice(data["verdict"], ATTEMPT_OUTCOME_VERDICTS, "attempt outcome verdict is unsupported")
    _validate_disposition(data["disposition"], data["blocked_reason"])
    refs = data["source_event_ids"]
    if not isinstance(refs, dict):
        raise ValueError("attempt outcome source_event_ids are incomplete")  # noqa: TRY004 - released boundary refuses with ValueError
    _validate_field_set(refs, _SOURCE_EVENT_FIELDS, "attempt outcome source_event_ids are incomplete")
    for name, value in refs.items():
        _optional_non_empty(value, f"source event {name}")
    completeness = data["usage_completeness"]
    if not isinstance(completeness, dict):
        raise ValueError("attempt outcome usage completeness is incomplete")  # noqa: TRY004 - released boundary refuses with ValueError
    _validate_field_set(completeness, _USAGE_ROLES, "attempt outcome usage completeness is incomplete")
    for role in _USAGE_ROLES:
        state = completeness[role]
        _validate_choice(
            state, ATTEMPT_OUTCOME_COMPLETENESS, f"attempt outcome {role} completeness is unsupported"
        )
        _validate_usage_link(state, refs[f"{role}_usage"], role)
    if version == 2:
        lineage = data["lineage_required"]
        if not isinstance(lineage, dict):
            raise ValueError("attempt outcome lineage requiredness is incomplete")
        _validate_field_set(lineage, _LINEAGE_FIELDS, "attempt outcome lineage requiredness is incomplete")
        _validate_lineage_values(lineage.values())


@dataclass(frozen=True, slots=True)
class AttemptOutcomeSourceEventIds:
    """Typed lineage references carried by one attempt outcome."""

    report: str | None
    verdict: str | None
    decision: str | None
    effect: str | None
    worker_usage: str | None
    review_usage: str | None

    def __post_init__(self) -> None:
        for name in _SOURCE_EVENT_FIELDS:
            _optional_non_empty(getattr(self, name), f"source event {name}")

    def to_data(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in _SOURCE_EVENT_FIELDS}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomeSourceEventIds:
        _validate_field_set(data, _SOURCE_EVENT_FIELDS, "attempt outcome source_event_ids are incomplete")
        return cls(**{name: data[name] for name in _SOURCE_EVENT_FIELDS})


@dataclass(frozen=True, slots=True)
class AttemptOutcomeUsageCompleteness:
    """Typed worker/reviewer provider-evidence completeness."""

    worker: AttemptOutcomeCompleteness
    review: AttemptOutcomeCompleteness

    def __post_init__(self) -> None:
        if not isinstance(self.worker, AttemptOutcomeCompleteness):
            raise ValueError("attempt outcome worker completeness is unsupported")  # noqa: TRY004 - released boundary refuses with ValueError
        if not isinstance(self.review, AttemptOutcomeCompleteness):
            raise ValueError("attempt outcome review completeness is unsupported")  # noqa: TRY004 - released boundary refuses with ValueError

    def to_data(self) -> dict[str, str]:
        return {"worker": self.worker.value, "review": self.review.value}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomeUsageCompleteness:
        _validate_field_set(data, _USAGE_ROLES, "attempt outcome usage completeness is incomplete")
        try:
            return cls(
                worker=AttemptOutcomeCompleteness(data["worker"]),
                review=AttemptOutcomeCompleteness(data["review"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"attempt outcome completeness is unsupported: {exc}") from None


@dataclass(frozen=True, slots=True)
class AttemptOutcomeLineageRequired:
    """Which v2 forward-lineage facts are obligations rather than optional evidence."""

    specification_revision: bool
    report: bool
    verdict: bool
    decision: bool
    effect: bool
    worker_usage: bool
    review_usage: bool

    def __post_init__(self) -> None:
        _validate_lineage_values(getattr(self, name) for name in _LINEAGE_FIELDS)

    def to_data(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in _LINEAGE_FIELDS}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomeLineageRequired:
        _validate_field_set(data, _LINEAGE_FIELDS, "attempt outcome lineage requiredness is incomplete")
        return cls(**{name: data[name] for name in _LINEAGE_FIELDS})


@dataclass(frozen=True, slots=True)
class AttemptOutcomePayload:
    """Immutable domain value for the released v1/v2 ``attempt.outcome`` event data."""

    version: int
    attempt_id: str
    attempt: int
    report_generation: int
    sprint_ref: str | None
    specification_revision: str | None
    terminal_state: AttemptOutcomeTerminalState
    verdict: AttemptOutcomeVerdict
    disposition: TerminalDisposition
    blocked_reason: BlockedReason | None
    source_event_ids: AttemptOutcomeSourceEventIds
    usage_completeness: AttemptOutcomeUsageCompleteness
    lineage_required: AttemptOutcomeLineageRequired | None = None

    def __post_init__(self) -> None:
        # Direct construction retains its hash-based membership and bool refusal.
        # Raw/schema parsing deliberately accepts True and equal float versions.
        if self.version not in set(_VERSIONS) or isinstance(self.version, bool):
            raise ValueError("unsupported attempt outcome version")
        _validate_scalars(
            self.attempt_id,
            self.attempt,
            self.report_generation,
            self.sprint_ref,
            self.specification_revision,
        )
        if not isinstance(self.terminal_state, AttemptOutcomeTerminalState):
            raise ValueError("attempt outcome terminal state must be done, blocked or in_progress")  # noqa: TRY004 - released boundary refuses with ValueError
        if not isinstance(self.verdict, AttemptOutcomeVerdict):
            raise ValueError("attempt outcome verdict is unsupported")  # noqa: TRY004 - released boundary refuses with ValueError
        _validate_disposition(self.disposition, self.blocked_reason)
        if not isinstance(self.source_event_ids, AttemptOutcomeSourceEventIds):
            raise ValueError("attempt outcome source_event_ids are incomplete")  # noqa: TRY004 - released boundary refuses with ValueError
        if not isinstance(self.usage_completeness, AttemptOutcomeUsageCompleteness):
            raise ValueError("attempt outcome usage completeness is incomplete")  # noqa: TRY004 - released boundary refuses with ValueError
        for role in _USAGE_ROLES:
            state = getattr(self.usage_completeness, role)
            usage_ref = getattr(self.source_event_ids, f"{role}_usage")
            _validate_usage_link(state, usage_ref, role)
        if self.version == 1 and self.lineage_required is not None:
            raise ValueError("attempt outcome v1 has an unsupported field set")
        if self.version == 2 and not isinstance(self.lineage_required, AttemptOutcomeLineageRequired):
            raise ValueError("attempt outcome lineage requiredness is incomplete")

    def natural_key(self, card_ref: str) -> tuple[str, str, int]:
        """Return the released ledger identity for this outcome."""
        if not _is_non_empty_string(card_ref):
            raise ValueError("attempt outcome card ref must be non-empty")
        return (card_ref, self.attempt_id, self.report_generation)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "attempt_id": self.attempt_id,
            "attempt": self.attempt,
            "report_generation": self.report_generation,
            "sprint_ref": self.sprint_ref,
            "specification_revision": self.specification_revision,
            "terminal_state": self.terminal_state.value,
            "verdict": self.verdict.value,
            "disposition": self.disposition,
            "blocked_reason": self.blocked_reason,
            "source_event_ids": self.source_event_ids.to_data(),
            "usage_completeness": self.usage_completeness.to_data(),
        }
        if self.version == 2:
            assert self.lineage_required is not None
            data["lineage_required"] = self.lineage_required.to_data()
        return data

    def lineage_missing(self) -> tuple[str, ...]:
        """Return missing required v2 lineage facts in released field order."""
        if self.version == 1:
            return ()
        assert self.lineage_required is not None
        missing: list[str] = []
        for name in _LINEAGE_FIELDS:
            if not getattr(self.lineage_required, name):
                continue
            value = (
                self.specification_revision
                if name == "specification_revision"
                else getattr(self.source_event_ids, name)
            )
            if value is None:
                missing.append(name)
        return tuple(missing)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AttemptOutcomePayload:
        version = _validate_schema(data)
        try:
            refs_raw = data["source_event_ids"]
            completeness_raw = data["usage_completeness"]
            if not isinstance(refs_raw, Mapping):
                raise ValueError("attempt outcome source_event_ids are incomplete")  # noqa: TRY004 - released boundary refuses with ValueError
            if not isinstance(completeness_raw, Mapping):
                raise ValueError("attempt outcome usage completeness is incomplete")  # noqa: TRY004 - released boundary refuses with ValueError
            lineage_required: AttemptOutcomeLineageRequired | None = None
            if version == 2:
                lineage_raw = data.get("lineage_required")
                if not isinstance(lineage_raw, Mapping):
                    raise ValueError("attempt outcome lineage requiredness is incomplete")
                lineage_required = AttemptOutcomeLineageRequired.from_data(lineage_raw)
            return cls(
                version=version,
                attempt_id=data["attempt_id"],
                attempt=data["attempt"],
                report_generation=data["report_generation"],
                sprint_ref=data["sprint_ref"],
                specification_revision=data["specification_revision"],
                terminal_state=AttemptOutcomeTerminalState(data["terminal_state"]),
                verdict=AttemptOutcomeVerdict(data["verdict"]),
                disposition=data["disposition"],
                blocked_reason=data["blocked_reason"],
                source_event_ids=AttemptOutcomeSourceEventIds.from_data(refs_raw),
                usage_completeness=AttemptOutcomeUsageCompleteness.from_data(completeness_raw),
                lineage_required=lineage_required,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid attempt outcome payload: {exc}") from None
