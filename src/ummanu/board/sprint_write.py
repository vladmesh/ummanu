"""Typed Sprint write-side values at the legacy document boundary.

SprintReader and the durable transaction/audit stores intentionally keep their released dict/JSON
shapes.  The writer should not carry those bags through domain logic, though: this module parses the
closed Sprint fields once into immutable values and renders only when a persistence or public boundary
requires the historical document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ummanu.board.e2e_budget import DEFAULT_E2E_BUDGET
from ummanu.board.local_run import LocalRunException, parse_local_run_exceptions
from ummanu.board.models import SprintState
from ummanu.board.owner_decisions import parse_decisions
from ummanu.board.roles import Role
from ummanu.board.sprint_admission import SprintAdmission
from ummanu.board.sprint_read import SprintBudget


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _observer(value: Any) -> dict[str, Any] | None:
    return {str(key): item for key, item in value.items()} if isinstance(value, Mapping) else None


@dataclass(frozen=True, slots=True)
class SprintWriteSnapshot:
    """The Sprint fields a mutation is allowed to reason about.

    This is deliberately smaller than the public Sprint document. Comments, cards and presentation
    summaries stay on the read/public side; write rules consume the typed state/resources they need.
    """

    entity_id: str
    ref: str
    state: SprintState
    repositories: tuple[str, ...]
    product: str
    issues: tuple[str, ...]
    reservations: tuple[str, ...]
    budget: SprintBudget
    current_task: str | None
    observer: dict[str, Any] | None
    updated_at: str

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, Any],
        *,
        thresholds: Mapping[str, int] | None = None,
    ) -> SprintWriteSnapshot:
        raw_state = str(document.get("status") or SprintState.OPEN.value)
        try:
            state = SprintState(raw_state)
        except ValueError:
            # SprintReader has always normalized unknown legacy status spellings to open.
            state = SprintState.OPEN
        audit = document.get("audit")
        updated_at = str(audit.get("updated_at") or "") if isinstance(audit, Mapping) else ""
        current_task = str(document.get("current_task") or "") or None
        return cls(
            entity_id=str(document.get("id") or ""),
            ref=str(document.get("ref") or ""),
            state=state,
            repositories=_strings(document.get("repositories")),
            product=str(document.get("product") or "").strip(),
            issues=_strings(document.get("issues")),
            reservations=_strings(document.get("reservations")),
            budget=SprintBudget.from_legacy(document.get("budget"), thresholds=thresholds),
            current_task=current_task,
            observer=_observer(document.get("observer")),
            updated_at=updated_at,
        )

    def admission(self) -> SprintAdmission:
        return SprintAdmission(
            ref=self.ref,
            product=self.product,
            reservations=self.reservations,
            repositories=self.repositories,
            state=self.state,
        )


@dataclass(frozen=True, slots=True)
class SprintCreateIntent:
    """Normalized create/restore-create request before it crosses into the transaction store."""

    role: Role
    actor: str
    goal: str
    definition_of_done: str
    repositories: tuple[str, ...]
    product: str
    issues: tuple[str, ...]
    reservations: tuple[str, ...]
    reference: str
    state: SprintState
    observer: dict[str, Any] | None
    worker: str | None
    reviewer: str | None
    # The PO session that opens the sprint and the productions its operations may touch. Both are
    # inputs of the request, and both are left off the document when unset, so an intent staged
    # before they existed still replays as the same request.
    po_session: str | None = None
    allowed_productions: tuple[str, ...] = ()
    # The e2e run budget (secretary-1796). Left off the document at the default, so an intent staged
    # before it existed still replays as the same request.
    e2e_budget: int = DEFAULT_E2E_BUDGET
    # Omitted at the empty default to preserve released create request identity.
    local_run_exceptions: tuple[LocalRunException, ...] = ()
    owner_decisions: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SprintCreateIntent:
        return cls(
            role=Role(str(document.get("role") or "")),
            actor=str(document.get("actor") or ""),
            goal=str(document.get("goal") or ""),
            definition_of_done=str(document.get("definition_of_done") or ""),
            repositories=_strings(document.get("repositories")),
            product=str(document.get("product") or ""),
            issues=_strings(document.get("issues")),
            reservations=_strings(document.get("reservations")),
            reference=str(document.get("reference") or ""),
            state=SprintState(str(document.get("status") or SprintState.OPEN.value)),
            observer=_observer(document.get("observer")),
            worker=str(document.get("worker")) if document.get("worker") is not None else None,
            reviewer=str(document.get("reviewer")) if document.get("reviewer") is not None else None,
            po_session=str(document.get("po_session")) if document.get("po_session") else None,
            allowed_productions=_strings(document.get("allowed_productions")),
            e2e_budget=int(document.get("e2e_budget", DEFAULT_E2E_BUDGET)),
            owner_decisions=tuple(parse_decisions(document.get("owner_decisions", []))),
            local_run_exceptions=parse_local_run_exceptions(
                document.get("local_run_exceptions", []), projects=_strings(document.get("reservations"))
            ),
        )

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "role": self.role.value,
            "actor": self.actor,
            "goal": self.goal,
            "definition_of_done": self.definition_of_done,
            "repositories": list(self.repositories),
            "product": self.product,
            "issues": list(self.issues),
            "reservations": list(self.reservations),
            "reference": self.reference,
            "status": self.state.value,
            "observer": dict(self.observer) if self.observer is not None else None,
            "worker": self.worker,
            "reviewer": self.reviewer,
        }
        if self.owner_decisions:
            document["owner_decisions"] = list(self.owner_decisions)
        if self.po_session:
            document["po_session"] = self.po_session
        if self.allowed_productions:
            document["allowed_productions"] = list(self.allowed_productions)
        if self.e2e_budget != DEFAULT_E2E_BUDGET:
            document["e2e_budget"] = self.e2e_budget
        if self.local_run_exceptions:
            document["local_run_exceptions"] = [entry.to_document() for entry in self.local_run_exceptions]
        return document

    def admission(self, *, reference: str | None = None) -> SprintAdmission:
        return SprintAdmission(
            ref=self.reference if reference is None else reference,
            product=self.product,
            reservations=self.reservations,
            repositories=self.repositories,
            state=self.state,
        )


@dataclass(frozen=True, slots=True)
class SprintReopenIntent:
    """The replay identity of one reopen, independent of its persisted dict representation."""

    role: Role
    actor: str
    reference: str
    observer: dict[str, Any] | None

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SprintReopenIntent:
        return cls(
            role=Role(str(document.get("role") or "")),
            actor=str(document.get("actor") or ""),
            reference=str(document.get("reference") or ""),
            observer=_observer(document.get("observer")),
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "actor": self.actor,
            "reference": self.reference,
            "observer": dict(self.observer) if self.observer is not None else None,
        }


@dataclass(frozen=True, slots=True)
class SprintMutationReceipt:
    """Typed operation result until the public Sprint document is projected at return time."""

    action: str
    event_id: str

    def to_document(self, sprint: Mapping[str, Any]) -> dict[str, Any]:
        return {"action": self.action, "sprint": dict(sprint), "event_id": self.event_id}


__all__ = [
    "SprintCreateIntent",
    "SprintMutationReceipt",
    "SprintReopenIntent",
    "SprintWriteSnapshot",
]
