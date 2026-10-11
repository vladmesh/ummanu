"""Consumed fields of TaskService's typed marker events, without a board/runtime."""

from __future__ import annotations

import hashlib
from typing import Any


def marker_event(
    marker: str,
    body: str,
    request: str,
    *,
    ref: str,
    description: str,
    revision: str,
    occurrence: int = 1,
    **fields: Any,
) -> dict[str, Any]:
    action = marker.split(":", 1)[0]
    data = {
        "marker": marker,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "marker_occurrence": occurrence,
        "specification_revision": revision,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        **fields,
    }
    if action == "decision":
        data["decision"] = marker.split(":", 1)[1]
        data.setdefault("protocol_prerequisites", [])
    else:
        data["status"] = marker.split(":", 1)[1]
    return {
        "event_id": request,
        "request_id": request,
        "ref": ref,
        "record_type": "board.protocol_event",
        "kind": {"review": "card.verdict", "decision": "card.decided", "report": "card.reported"}[action],
        "data": data,
    }
