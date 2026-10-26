"""Per-card head lifecycle event: a worker or reviewer head of one card stopped, and its successor.

Contract: docs/PROTOCOLS.md "Head changes". A journal-only `head_changed` record that moves no card
state. `before` is the stopped run, `after` the successor actually launched, or null when the role
was left without one; the record's own `occurred_at` is the time of the change.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

HEAD_CHANGED_KIND = "head_changed"

CAUSE_STALL = "stall"
CAUSE_HEAD_EXIT = "head_exit"
CAUSE_PROVIDER_FAILOVER = "provider_failover"
CAUSE_OPERATOR_STOP = "operator_stop"
CAUSES = (CAUSE_STALL, CAUSE_HEAD_EXIT, CAUSE_PROVIDER_FAILOVER, CAUSE_OPERATOR_STOP)

ROLES = ("worker", "reviewer")

CHANGE_REPLACED = "replaced"
CHANGE_STOPPED = "stopped"

_SIDE_FIELDS = ("run_id", "profile", "adapter", "model", "effort")


def head_change_payload(
    *,
    role: str,
    cause: str,
    reason: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any] | None,
    requested: str = "",
) -> dict[str, Any]:
    """The normalized payload of one change; raises `ValueError` for a shape the contract lacks."""
    if role not in ROLES:
        raise ValueError(f"a head change names a card role ({', '.join(ROLES)}), not {role!r}")
    if cause not in CAUSES:
        raise ValueError(f"a head change cause is one of {', '.join(CAUSES)}, not {cause!r}")
    if not str(reason or "").strip():
        raise ValueError("a head change says why it happened")
    stopped = _side(before, "before")
    successor = None if after is None else _side(after, "after")
    if successor is not None and successor["run_id"] == stopped["run_id"]:
        raise ValueError("a replacement names a successor run other than the stopped one")
    return {
        "role": role,
        "change": CHANGE_STOPPED if successor is None else CHANGE_REPLACED,
        "cause": cause,
        "reason": str(reason).strip(),
        "before": stopped,
        "after": successor,
        "requested": str(requested or ""),
    }


def head_changes(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The card's committed head changes in journal order, each with the time it was recorded."""
    return [
        {
            "at": str(event.get("occurred_at") or ""),
            "request_id": str(event.get("request_id") or ""),
            **payload,
        }
        for event in events
        if event.get("kind") == HEAD_CHANGED_KIND and isinstance(payload := event.get("payload"), dict)
    ]


def _side(value: Any, name: str) -> dict[str, str]:
    fields = value if isinstance(value, Mapping) else {}
    side = {field: str(fields.get(field) or "") for field in _SIDE_FIELDS}
    if not side["run_id"] or not side["profile"]:
        raise ValueError(f"a head change's {name} is a head run naming its run id and its profile")
    return side
