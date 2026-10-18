"""The normalized board import's write order: the one place it is defined.

Every round of the 1476 recovery drill stopped on one more foreign key the import wrote out of
order -- an Issue before its Product (ummanu-27), then an issue comment before the `requests` row it
claims (`issue_comment_claims_its_request`, ummanu-45). Each was fixed where it surfaced, so the
next one waited for a drill. This table is what closes the class: it names the import's write
phases in the order the import runs them and the `ummanu.board.schema` tables each phase writes, and
`tests/test_import_write_order.py` builds the foreign-key graph from the schema's own metadata and
fails when a parent table's first phase comes after a phase that writes its child. A table added
to the schema has to be placed here, or named in `NOT_IMPORTED`, before that test passes again.

How the import follows it:

* ``history`` runs first (`restore._restore_board_history`). It writes every exported committed
  request and its typed occurrence, and nothing it writes references a board row, so every row a
  later phase writes that claims an exported request -- an issue or product comment stamped
  ``[request-id:...]`` -- finds its `requests` row already there.
* ``product``, ``issue`` and ``card`` are the three normalized record kinds. The card restore
  (`task_restore.restore_cards_batched`) runs them in two sweeps, one creating rows and one
  initializing them, and each sweep is sorted by `record_rank`, the position of the record's phase
  here. That is the Product-before-Issue order ummanu-27 needed, taken from this table.
* Every later phase is a step of `restore._import_normalized_board` in this order.

Two kinds of write are deliberately not phases of their own:

* A writer's **own claim**. Each audited write stages its request in `requests` immediately before
  its effect, in the same call (`SqlTaskAudit.stage`, then the board write), and only that effect
  claims it -- a sprint comment, a restored sprint budget event. That order is the writer protocol's,
  not an order between phases, so `requests` is placed once, at ``history``, the phase whose rows
  other phases claim.
* An **ensured parent**. A writer that names a project inserts its `projects` row first, in the same
  call (`INSERT ... ON CONFLICT DO NOTHING`), so `projects` is listed in each phase that ensures it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ImportPhase:
    """One write phase of the board import, and the schema tables its writes touch."""

    name: str
    tables: tuple[str, ...]
    #: The normalized `record_type` this phase creates, for the three record phases.
    record_type: str = ""


IMPORT_PHASES: tuple[ImportPhase, ...] = (
    ImportPhase("history", ("requests", "board_events")),
    ImportPhase("product", ("products", "product_projects", "projects"), record_type="product"),
    ImportPhase("issue", ("issues",), record_type="issue"),
    ImportPhase(
        "card",
        ("tasks", "projects", "task_issues", "task_dependencies", "task_supersessions", "task_retry_heads"),
        record_type="task",
    ),
    ImportPhase("card_comments", ("task_comments", "issue_comments", "product_comments")),
    ImportPhase("closure", ("tasks", "issues", "products")),
    ImportPhase("order", ("tasks",)),
    ImportPhase(
        "sprints",
        (
            "sprints",
            "projects",
            "repositories",
            "sprint_repositories",
            "sprint_issues",
            "sprint_projects",
            "sprint_resumes",
            "sprint_budget_events",
            "sprint_e2e_charges",
        ),
    ),
    ImportPhase("sprint_comments", ("sprint_comments",)),
    ImportPhase("origin_returns", ("origin_returns",)),
)

#: The normalized board import does not wire the separate PO SQL capture/restore primitive.
#: Owner events and sprint close decisions are outside board export content.
NOT_IMPORTED: frozenset[str] = frozenset(
    {
        "owner_events",
        "po_feed",
        "po_requests",
        "po_sessions",
        "po_turns",
        "sprint_decisions",
    }
)


def phase_rank(name: str) -> int:
    """The position of the phase `name` in the import order."""
    for rank, phase in enumerate(IMPORT_PHASES):
        if phase.name == name:
            return rank
    raise KeyError(f"no board import phase is named {name!r}")


def record_rank(record_type: str) -> int:
    """The position of the phase that writes a normalized record of `record_type`.

    An absent or unknown kind is a task card, as the restore reads it everywhere else.
    """
    for rank, phase in enumerate(IMPORT_PHASES):
        if phase.record_type and phase.record_type == (record_type or "task"):
            return rank
    return phase_rank("card")


@dataclass(frozen=True)
class ForeignKeyEdge:
    """One foreign key of the schema, child table to parent table."""

    child: str
    parent: str
    name: str
    #: `DEFERRABLE INITIALLY DEFERRED`: checked at commit, so any order inside the import's one
    #: transaction satisfies it.
    deferred: bool


def foreign_key_edges(metadata: Any) -> list[ForeignKeyEdge]:
    """Every foreign key of `metadata`, named or not, as child-to-parent edges."""
    edges: list[ForeignKeyEdge] = []
    for table in metadata.sorted_tables:
        for constraint in table.foreign_key_constraints:
            edges.append(
                ForeignKeyEdge(
                    child=table.name,
                    parent=constraint.referred_table.name,
                    name=constraint.name or f"{table.name}({', '.join(constraint.column_keys)})",
                    deferred=bool(constraint.deferrable)
                    and str(constraint.initially or "").upper() == "DEFERRED",
                )
            )
    return edges


def order_violations(
    metadata: Any,
    phases: Iterable[ImportPhase] = IMPORT_PHASES,
    not_imported: frozenset[str] = NOT_IMPORTED,
) -> list[str]:
    """What the import order gets wrong against `metadata`'s tables and foreign keys.

    A table is either written by a phase or named as not imported, never both and never neither. For
    every immediate foreign key between two written tables, the parent is written no later than any
    phase that writes the child: same phase is the writer's own statement order. A deferred key is
    satisfied by the transaction, whatever the order.
    """
    phases = tuple(phases)
    first: dict[str, int] = {}
    writers: dict[str, list[int]] = {}
    for rank, phase in enumerate(phases):
        for table in phase.tables:
            first.setdefault(table, rank)
            writers.setdefault(table, []).append(rank)
    problems: list[str] = []
    tables = set(metadata.tables)
    for table in sorted(tables - set(first) - not_imported):
        problems.append(f"{table}: not placed in the import order and not named as not imported")
    for table in sorted(set(first) & not_imported):
        problems.append(f"{table}: both placed in the import order and named as not imported")
    for table in sorted((set(first) | not_imported) - tables):
        problems.append(f"{table}: named by the import order but not a schema table")
    for edge in foreign_key_edges(metadata):
        if edge.deferred or edge.child not in first or edge.parent not in first:
            continue
        for rank in writers[edge.child]:
            if first[edge.parent] > rank:
                problems.append(
                    f"{edge.name}: {edge.child} is written in phase {phases[rank].name!r} before its "
                    f"parent {edge.parent} is first written in phase {phases[first[edge.parent]].name!r}"
                )
    return problems


__all__ = [
    "IMPORT_PHASES",
    "NOT_IMPORTED",
    "ForeignKeyEdge",
    "ImportPhase",
    "foreign_key_edges",
    "order_violations",
    "phase_rank",
    "record_rank",
]
