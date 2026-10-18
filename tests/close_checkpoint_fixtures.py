"""Mixed typed close facts with shared request identities and tied timestamps."""

from ummanu._fsutil import write_json, write_ndjson
from ummanu.board.close_checkpoint import close_coverage


def close_facts(reference="sprint:closed"):
    rows = []
    for index, (kind, subject, verdict, actual) in enumerate((
        ("issue", "one", "resolved", None),
        ("issue", "two", "already_closed", "invalid"),
        ("card", "ummanu-1", "drop", None),
        ("card", "ummanu-2", "already_moved", "done"),
    )):
        rows.append({
            "decision_id": index * 3 + 7, "sprint_ref": reference, "subject_kind": kind,
            "issue_id": subject if kind == "issue" else None,
            "task_ref": subject if kind == "card" else None,
            "verdict": verdict, "actual": actual, "reason": f"saved reason {index}",
            "request_id": "close-request", "decided_at": "2026-09-01T00:00:00.000012Z",
        })
    return {"close_reason": "saved close reason", "closeout_document": "knowledge/closeout.md", "decisions": rows}


def write_close(board, sprints, cards, history):
    write_ndjson(board / "sprints.ndjson", sprints)
    write_ndjson(board / "cards.ndjson", cards)
    write_ndjson(board / "audit.ndjson", history)
    write_json(board / "export.json", {"version": 2, "sprint_close": close_coverage(sprints, (board / "sprints.ndjson").read_bytes())})
