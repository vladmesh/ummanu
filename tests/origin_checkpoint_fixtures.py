"""Portable obligations and their committed transition records."""

import hashlib
import json
from pathlib import Path

from ummanu._fsutil import write_json, write_ndjson
from ummanu.board.origin_checkpoint import OUTBOX_FILE


def obligations(count=3):
    rows, history = [], []
    for index in range(count):
        status = (None, "delivered", "skipped")[index % 3]
        row = {
            "id": index * 2 + 1,
            "task_ref": "ummanu-1",
            "event_id": f"transition-{index}",
            "request_id": f"audit-request-{index}",
            "target_state": "done" if index % 2 else "blocked",
            "created_at": "2026-09-01T01:00:00.000001Z",
            "delivered_at": "2026-09-01T02:00:00.000002Z" if status else None,
            "status": status,
            "notice": "saved notice" if status else None,
            "session": "successor-session" if status else None,
            "po_request_id": f"delivery-request-{index}" if status else None,
        }
        rows.append(row)
        history.append({
            "event_id": row["event_id"], "request_id": row["request_id"], "ref": row["task_ref"],
            "kind": "moved", "outcome": "success", "payload": {"from": "in_progress", "to": row["target_state"]},
        })
    return rows, history


def write_outbox(board: Path, rows):
    write_ndjson(board / OUTBOX_FILE, rows)
    summary_path = board / "export.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary.update(
        version=2, origin_return_count=len(rows),
        origin_return_sha256=hashlib.sha256((board / OUTBOX_FILE).read_bytes()).hexdigest(),
    )
    write_json(summary_path, summary)
