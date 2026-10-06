"""`TaskRef`: the durable document a head run is a run of, with its kind.

Kinds: `card` (a Pipeline card reference), `sprint` (a board sprint entity, e.g. for an observer
head) and `standing` (a role's standing instruction, for heads not serving one unit of work).
`document` is the absolute on-disk path of the task text when the caller wrote one (what `nudge`
names), and may be empty when the board entity is the document. This module neither reads the
document nor decides what a head is told.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# The three kinds of durable task document a head can be pointed at.
TASK_CARD = "card"
TASK_SPRINT = "sprint"
TASK_STANDING = "standing"
TASK_KINDS = (TASK_CARD, TASK_SPRINT, TASK_STANDING)


class TaskRefError(ValueError):
    """A pointer that names no task, or names one of a kind this product does not have."""


@dataclass(frozen=True)
class TaskRef:
    """What a head run is a run of; frozen because it is part of the run's identity."""

    kind: str
    ref: str
    document: str = ""

    def __post_init__(self) -> None:
        if self.kind not in TASK_KINDS:
            raise TaskRefError(f"a task pointer is one of {', '.join(TASK_KINDS)}, not {self.kind!r}")
        if not self.ref:
            raise TaskRefError(f"a {self.kind} task pointer names its task, and this one is empty")
        if self.document and not os.path.isabs(self.document):
            # A relative path would resolve differently in every pane it could be delivered to.
            raise TaskRefError(f"a task document is named by absolute path, and {self.document!r} is not one")

    @classmethod
    def card(cls, ref: str, *, document: str = "") -> TaskRef:
        """One Pipeline card, the kind of work the production dispatcher runs."""
        return cls(kind=TASK_CARD, ref=ref, document=document)

    @classmethod
    def sprint(cls, ref: str, *, document: str = "") -> TaskRef:
        """A sprint entity: what an observer head is pointed at, and no card at all."""
        return cls(kind=TASK_SPRINT, ref=ref, document=document)

    @classmethod
    def standing(cls, role: str, *, document: str = "") -> TaskRef:
        """A role's standing instruction, for a head whose task is its role rather than a unit."""
        return cls(kind=TASK_STANDING, ref=role, document=document)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "document": self.document}

    @classmethod
    def from_json(cls, payload: Any) -> TaskRef:
        if not isinstance(payload, dict):
            raise TaskRefError("a task pointer is read from an object, and this is not one")
        return cls(
            kind=str(payload.get("kind") or ""),
            ref=str(payload.get("ref") or ""),
            document=str(payload.get("document") or ""),
        )
