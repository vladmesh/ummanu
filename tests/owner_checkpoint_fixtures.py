"""Complete mixed bell history shared by checkpoint and normal SQL recovery fixtures."""

import json

from ummanu._fsutil import write_json, write_ndjson
from ummanu.board.owner_checkpoint import OWNER_FILE, owner_coverage
from ummanu.board.owner_events import KIND_CLASS


def bell_rows(count=12):
    kinds = tuple(KIND_CLASS)
    return [
        {
            "id": 2 * index + 3,
            "kind": kinds[index % len(kinds)],
            "class": KIND_CLASS[kinds[index % len(kinds)]],
            "subject_ref": (None, "retired-1", "po-session:old", "ummanu-1")[index % 4],
            "text": f"saved bell fact {index}",
            "created_at": "2026-09-01T01:00:00.000001Z",
            "read_at": "2026-09-01T02:00:00.000002Z" if index % 3 else None,
            "dedup_key": f"saved-bell-{index}",
        }
        for index in range(count)
    ]


def write_bell(board, rows):
    write_ndjson(board / OWNER_FILE, rows)
    path = board / "export.json"
    summary = json.loads(path.read_text()) if path.exists() else {"version": 1}
    summary["owner_events"] = owner_coverage(rows, (board / OWNER_FILE).read_bytes())
    write_json(path, summary)
