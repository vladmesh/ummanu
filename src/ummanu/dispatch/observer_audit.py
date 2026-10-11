"""Observer audit staging, commitment and event/cursor lookup helpers.

Keep request IDs, serialized events, and commit/repair ordering unchanged.
Observer lifecycle and persistence remain in observer.py.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from ummanu.dispatch.state import now_rfc3339, request_token


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _event_age_seconds(occurred_at: str) -> float:
    occurred = _timestamp(occurred_at)
    if occurred is None:
        return 0.0
    return max(0.0, time.time() - occurred.timestamp())


def _event_id(event: dict[str, Any] | None) -> str:
    if not isinstance(event, dict):
        return ""
    return str(event.get("event_id") or event.get("request_id") or "")


#: How many times the observer's narrowed audit read is retaken while cards are being linked.
_OBSERVER_SLICE_ATTEMPTS = 3


def _cursor_refs(audit: Any, event_ids: set[str]) -> set[str]:
    """The refs of the committed records these cursor ids name, each found by its own key.

    A cursor carries an event's id, which is its request id or its typed `event_id`; the first is
    the primary key and the second is `event_id_owner`'s index. An id neither finds adds nothing,
    and the caller's existing refusal of an unavailable cursor stands.
    """
    refs: set[str] = set()
    for event_id in event_ids:
        record = audit.committed_event(event_id)
        if record is None:
            owner = audit.event_id_owner(event_id)
            record = audit.committed_event(owner) if owner else None
        if isinstance(record, dict) and str(record.get("ref") or ""):
            refs.add(str(record["ref"]))
    return refs


def _linked_refs(sprint: dict[str, Any]) -> set[str]:
    cards = sprint.get("cards") if isinstance(sprint.get("cards"), list) else []
    return {
        str(card.get("ref") or "")
        for card in cards
        if isinstance(card, dict) and str(card.get("ref") or "")
    }


def _event_index(events: list[dict[str, Any]], event_id: str) -> int:
    if not event_id:
        return -1
    for index, event in enumerate(events):
        if _event_id(event) == event_id:
            return index
    return -1

def observer_request_id(action: str, reference: str, generation: str, launches: int) -> str:
    """One id per (record, action, launch counter): the same tick retried is the same request."""
    return "-".join(("dispatcher-observer", action, request_token(reference), generation, str(launches)))


def stage_event(
    runtime: Any,
    kind: str,
    reference: str,
    request_id: str,
    payload: dict[str, Any],
    *,
    outcome: str = "success",
) -> dict[str, Any] | None:
    """Put one lifecycle event on durable disk before the host call it describes.

    Returns the staged event for `commit_event`, or None when there is nothing to commit. Raises
    OSError when the pending copy cannot be written, which the caller answers by not touching the
    host.
    """
    audit = getattr(runtime, "audit", None)
    if audit is None or audit.committed_event(request_id) is not None:
        return None
    event = {
        "event_id": "evt_" + uuid.uuid4().hex,
        "schema_version": 1,
        "occurred_at": now_rfc3339(),
        "actor": {"role": "dispatcher", "id": str(getattr(runtime, "owner", "") or "dispatcher")},
        "kind": kind,
        "outcome": outcome,
        "task_id": "",
        "ref": reference,
        "backend": {"kind": "dispatcher", "task_id": None, "revision": "n/a"},
        "request_id": request_id,
        "payload": payload,
    }
    audit.stage(request_id, event)
    return event

def commit_staged_event(runtime: Any, request_id: str) -> bool:
    """Append an event that was staged by a tick which did not live to commit it.

    False when the log has neither a committed nor a pending copy, or refuses the append: the
    outcome then says `audit: pending` rather than claiming a line that is not in the log.
    """
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return True
    try:
        if audit.committed_event(request_id) is not None:
            return True
        event = audit.pending_event(request_id)
        if event is None:
            return False
        audit.append(request_id, event)
    except Exception:  # noqa: BLE001 - observer bookkeeping never fails the tick
        return False
    return True


def commit_event(runtime: Any, event: dict[str, Any] | None) -> bool:
    """Move a staged event into the log. False means the pending copy is what carries it now.

    A refused commit is never raised at the caller: the effect the event describes has already
    happened, and the stale-staged settlement (`SqlTaskAudit.settle_stale_staged`) commits the
    pending copy on a later tick.
    """
    audit = getattr(runtime, "audit", None)
    if audit is None or event is None:
        return True
    request_id = str(event["request_id"])
    try:
        # Re-staged first: the pending copy then carries the post-effect payload too, so a repair
        # pass writes the same line this commit would have.
        audit.stage(request_id, event)
        audit.append(request_id, event)
    except Exception:  # noqa: BLE001 - observer bookkeeping never fails the tick
        return False
    return True


def discard_event(runtime: Any, request_id: str) -> None:
    """Drop a staged event whose effect never happened, so no repair pass invents it."""
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    try:
        audit.discard(request_id)
    except OSError:
        pass


def record_event(runtime: Any, kind: str, reference: str, request_id: str, payload: dict[str, Any]) -> bool:
    """Stage and commit an event with no host call to protect. False when neither landed."""
    try:
        event = stage_event(runtime, kind, reference, request_id, payload)
    except OSError:
        return False
    return commit_event(runtime, event)


def _with_audit(outcome: dict[str, Any], audited: bool) -> dict[str, Any]:
    """Say in the outcome when the event is only staged, so the tick does not read as clean."""
    if not audited:
        outcome["audit"] = "pending"
        outcome["status"] = "degraded"
    return outcome
