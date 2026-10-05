"""Dispatcher routing points to the canonical observer decision without copying it."""

from typing import Any

from ummanu.tasks import assessment_resolution


def decision_pointer(runtime: Any, task: dict[str, Any], decision: str) -> str:
    _visit, event = assessment_resolution(runtime.audit.events(task["ref"]))
    identity = str(event.get("event_id") or "") if isinstance(event, dict) else ""
    pointer = identity or f"[decision:{decision}]"
    return (f"Observer decision: {decision}. See {pointer} on {task['ref']} "
            f"(`python3 -P -m ummanu task show --ref {task['ref']}`).")
