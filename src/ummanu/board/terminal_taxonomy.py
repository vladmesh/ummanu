"""Forward-only terminal taxonomy for dispatcher observations.

The lifecycle owns its effects.  This module only gives new terminal writers one
typed description which the outcome ledger and sprint projection can share.
Older events have no such description and are deliberately read as legacy
evidence rather than guessed from request ids or prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TerminalDisposition = Literal["release", "rework", "reslice", "blocked", "drop", "operator_stop"]
BlockedReason = Literal[
    "implementation", "review", "task_contract", "gate", "provider", "infrastructure", "operator", "other"
]
BudgetClass = Literal["blocked", "infrastructure_blocked"]

TERMINAL_DISPOSITIONS = (
    "release",
    "rework",
    "reslice",
    "blocked",
    "drop",
    "operator_stop",
)
BLOCKED_REASONS = (
    "implementation",
    "review",
    "task_contract",
    "gate",
    "provider",
    "infrastructure",
    "operator",
    "other",
)
# Version 1 was emitted by the first forward-only taxonomy release.  Version
# 2 makes the classification that budget recovery consumes an immutable part
# of a newly committed terminal record.  Readers retain v1 so a committed
# forward record still wins over the older action-token fallback.
TERMINAL_TAXONOMY_VERSION = 2
_TERMINAL_TAXONOMY_V1 = 1


class TerminalTaxonomyValidationError(ValueError):
    """A new terminal value did not cross the typed taxonomy boundary."""


@dataclass(frozen=True, slots=True)
class TerminalTaxonomy:
    """Normalized terminal axes plus the source evidence that produced them."""

    disposition: TerminalDisposition
    blocked_reason: BlockedReason | None
    source_evidence: str | None
    budget_class: BudgetClass | None
    provenance: Literal["forward", "legacy"]

    def to_record(self) -> dict[str, Any]:
        return {
            "version": TERMINAL_TAXONOMY_VERSION,
            "disposition": self.disposition,
            "blocked_reason": self.blocked_reason,
            "source_evidence": self.source_evidence,
            "budget_class": self.budget_class,
            "provenance": self.provenance,
        }


def normalize_terminal_taxonomy(*, disposition: str, blocked_reason: str | None = None) -> TerminalTaxonomy:
    """Validate one new terminal description without rewriting its evidence.

    ``wrong_task_definition`` is a forward worker-report token, not an outcome
    reason, so it maps to ``task_contract`` while retaining the original token.
    ``external_fact`` has no more precise taxonomy meaning and remains visible
    as its source evidence under ``other``.
    """
    if disposition not in TERMINAL_DISPOSITIONS:
        raise TerminalTaxonomyValidationError(f"unsupported terminal disposition {disposition!r}")
    if disposition != "blocked":
        if blocked_reason is not None:
            raise TerminalTaxonomyValidationError(
                "a terminal taxonomy blocked reason is present exactly for blocked disposition"
            )
        # A reslice is still a terminal Blocked transition for the released
        # sprint budget, even though its independent disposition is reslice.
        return TerminalTaxonomy(
            disposition,
            None,
            None,
            "blocked" if disposition == "reslice" else None,
            "forward",
        )
    if not isinstance(blocked_reason, str) or not blocked_reason.strip():
        raise TerminalTaxonomyValidationError("a blocked terminal taxonomy requires source evidence")
    source_evidence = blocked_reason.strip()
    normalized = {
        "wrong_task_definition": "task_contract",
        "external_fact": "other",
    }.get(source_evidence, source_evidence)
    if normalized not in BLOCKED_REASONS:
        raise TerminalTaxonomyValidationError(f"unsupported terminal blocked reason {source_evidence!r}")
    return TerminalTaxonomy(
        disposition,
        normalized,
        source_evidence,
        "infrastructure_blocked" if normalized == "infrastructure" else "blocked",
        "forward",
    )


def read_terminal_taxonomy(data: Any, *, disposition: str | None) -> TerminalTaxonomy:
    """Read forward taxonomy data, or explicitly identify a legacy absence.

    This reader is for observational consumers.  It never upgrades a legacy
    action token or missing value into a newer classification.
    """
    payload = data if isinstance(data, dict) else {}
    raw = payload.get("terminal_taxonomy")
    if raw is None:
        if disposition is None:
            raise TerminalTaxonomyValidationError(
                "a legacy terminal taxonomy needs its transition disposition"
            )
        if disposition not in TERMINAL_DISPOSITIONS:
            raise TerminalTaxonomyValidationError(f"unsupported terminal disposition {disposition!r}")
        return TerminalTaxonomy(
            disposition,
            "other" if disposition == "blocked" else None,
            None,
            None,
            "legacy",
        )
    if not isinstance(raw, dict):
        raise TerminalTaxonomyValidationError("terminal taxonomy has an unsupported field set")
    version = raw.get("version")
    if type(version) is not int or version not in {_TERMINAL_TAXONOMY_V1, TERMINAL_TAXONOMY_VERSION}:
        raise TerminalTaxonomyValidationError("terminal taxonomy has an unsupported version")
    fields = {
        "version",
        "disposition",
        "blocked_reason",
        "source_evidence",
        "provenance",
    }
    if version == TERMINAL_TAXONOMY_VERSION:
        fields.add("budget_class")
    if set(raw) != fields:
        raise TerminalTaxonomyValidationError("terminal taxonomy has an unsupported field set")
    if raw["provenance"] != "forward":
        raise TerminalTaxonomyValidationError("terminal taxonomy has an unsupported version or provenance")
    taxonomy = normalize_terminal_taxonomy(
        disposition=raw["disposition"], blocked_reason=raw["source_evidence"]
    )
    if (disposition is not None and taxonomy.disposition != disposition) or taxonomy.blocked_reason != raw[
        "blocked_reason"
    ]:
        raise TerminalTaxonomyValidationError("terminal taxonomy does not match its transition")
    if version == TERMINAL_TAXONOMY_VERSION and taxonomy.budget_class != raw["budget_class"]:
        raise TerminalTaxonomyValidationError("terminal taxonomy does not match its budget class")
    return taxonomy


def budget_event_type(taxonomy: TerminalTaxonomy) -> BudgetClass | None:
    """Classify a terminal observation without deciding any lifecycle work."""
    return taxonomy.budget_class
