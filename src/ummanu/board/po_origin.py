"""PO delegation: the PO session a card came from, and where its result went (secretary-1792).

A card created by `task create --role po` inside a PO turn records its **origin**: the PO session of
that turn and the request id of the input the turn answers, both read from the turn's environment
(`UMMANU_PO_SESSION`, `UMMANU_PO_REQUEST`, set by `PoRunner.session_environment`). Nothing else
sets it: there is no flag, another role's create ignores the environment, and only create writes it.

Two typed fields of the extension bag (`extensions.extra`, docs/BOARD_STORE.md §8.2), JSON text, no
column:

- `po_origin`: `{session, request}`, written once by create and never again;
- `po_return`: the dispatcher's side, written only by the dispatcher (`TaskWriter.record_po_return`):
  `executor`, the session the dispatcher handed a decision/operation card to (shown, never a proof of
  who completed it: the completion records its own `po_session`); `successors`, per closed
  or missing session of the origin's line the session that succeeded it (route recorded before it is
  opened, id right after).

What the card owes its origin session, and what was delivered, is not here: it is the card's rows of
the origin-return outbox (`board/origin_outbox.py`), written in the transaction of each transition
into Done or Blocked. `task show` attaches them to the `origin` block as `returns`.

The origin's **line** is the origin session followed through `successors`: the session a result or
an out-of-sprint decision/operation card goes to now (:func:`line_head`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ummanu.board.extension_bag import bag_json

PO_ORIGIN = "po_origin"
PO_RETURN = "po_return"


def origin_text(session: str, request: str) -> str:
    return json.dumps({"session": session, "request": request}, sort_keys=True, separators=(",", ":"))


def po_origin(task: Mapping[str, Any]) -> dict[str, str] | None:
    """`{session, request}` of the card's origin, or None when it carries no well-formed one."""
    payload = bag_json(task, PO_ORIGIN)
    if not isinstance(payload, Mapping):
        return None
    session = str(payload.get("session") or "").strip()
    if not session:
        return None
    return {"session": session, "request": str(payload.get("request") or "").strip()}


def _records(payload: Any, required: tuple[str, ...]) -> dict[str, dict[str, str]]:
    if not isinstance(payload, Mapping):
        return {}
    return {
        str(key): {name: str(value) for name, value in record.items()}
        for key, record in payload.items()
        if isinstance(record, Mapping) and all(record.get(name) for name in required)
    }


@dataclass
class ReturnState:
    """The dispatcher's side of a delegated card, as `po_return` holds it."""

    executor: str = ""
    # closed or missing session -> {replaces, via, session}
    successors: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"executor": self.executor, "successors": self.successors}

    def text(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: Any) -> ReturnState:
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            executor=str(payload.get("executor") or ""),
            successors=_records(payload.get("successors"), ("replaces", "via")),
        )


def return_state(task: Mapping[str, Any]) -> ReturnState:
    return ReturnState.from_json(bag_json(task, PO_RETURN))


def line_head(origin_session: str, state: ReturnState) -> str:
    """The session of the origin's line that takes what is sent to the origin now."""
    session, seen = origin_session, {origin_session}
    while (following := (state.successors.get(session) or {}).get("session")) and following not in seen:
        session = following
        seen.add(session)
    return session


def in_line(session: str, origin_session: str, state: ReturnState) -> bool:
    """Whether `session` is the origin session or one of the successors that followed it."""
    current, seen = origin_session, set()
    while current and current not in seen:
        if current == session:
            return True
        seen.add(current)
        current = (state.successors.get(current) or {}).get("session") or ""
    return False


def origin_view(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `origin` block `task show` and `task list` carry, or None for a card with no origin.

    `returns` is filled by the reader from the card's outbox rows (`TaskReader`), empty here.
    """
    origin = po_origin(task)
    if origin is None:
        return None
    state = return_state(task)
    return {
        "po_session": origin["session"],
        "request_id": origin["request"] or None,
        "current_session": line_head(origin["session"], state),
        "executor": state.executor or None,
        "returns": [],
    }


__all__ = [
    "PO_ORIGIN",
    "PO_RETURN",
    "ReturnState",
    "in_line",
    "line_head",
    "origin_text",
    "origin_view",
    "po_origin",
    "return_state",
]
