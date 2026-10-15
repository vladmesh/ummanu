"""Slug/workspace-name helpers for the task pipeline — pure functions, no I/O, no Orca.

Workspaces already live under the project's own directory in the session manager
(`<workspaces root>/<project>/`), so repeating the project name in the workspace itself would
just echo something the path already says. `card_id` strips the reference (`<project>-<id>`, the board-CLI identity, left untouched)
down to the numeric tail a workspace or tab name is keyed off.
"""

from __future__ import annotations


def card_id(reference: str) -> str:
    """The numeric tail of a `<project>-<id>` reference, e.g. `"218"` from
    `"triggered-agents-218"`. The reference itself is left untouched everywhere else (board-CLI,
    comments, claim metadata) — this is only for naming workspaces/tabs."""
    return reference.rsplit("-", 1)[-1]
