"""Complete PO SQL source records, including inert host bindings and accepted input metadata."""

import errno
import json
import os
from pathlib import Path
from unittest import mock

from ummanu._fsutil import write_json
from ummanu.board.po_checkpoint import PO_FILE, po_coverage
from ummanu.board.po_notes_checkpoint import NOTES_CHECKPOINT_FILE, write_notes_capture
from ummanu.po.store import send_fingerprint, session_fingerprint, sprint_session_fingerprint

MOMENT = "2026-09-01T01:00:00.000001Z"
FINISHED = "2026-09-01T02:00:00.000002Z"


def po_records(feed_count=10):
    sessions = [
        {"session_id": identity, "cli": "codex", "model": "chosen-model", "cwd": "/old/host/work",
         "created_at": MOMENT, "state": "closed" if identity == "closed" else "open",
         "cli_session_id": "native-provider-context", "closed_at": FINISHED if identity == "closed" else None,
         "closed_by": "owner" if identity == "closed" else None, "effort": "high", "title": "Saved title"}
        for identity in ("closed", "empty", "open")
    ]
    turns = [
        {"session_id": "closed" if index == 0 else "open", "seq": 1 if index == 0 else index,
         "started_at": MOMENT, "finished_at": None if state == "running" else FINISHED,
         "state": state, "stdout_path": "/old/host/stdout.log", "pid": 123,
         "process_identity": "old-boot:12345", "reason": None if state == "running" else "saved reason",
         "resolved_model": "reported-model" if index % 2 else None}
        for index, state in enumerate(("completed", "completed", "failed", "interrupted", "running"))
    ]
    feed = [
        {"entry_id": index * 2 + 1, "session_id": "open", "turn_seq": index % 4 + 1,
         "role": "owner" if index % 2 == 0 else "agent", "text": "repeated text\n" + "я" * 1024,
         "created_at": MOMENT,
         "metadata": {"source": "sprint", "summary": "full summary", "comment_position": index,
                      "nested": [True, {"text": "accepted input"}]} if index % 2 == 0 else None}
        for index in range(feed_count)
    ]
    requests = [
        {"request_id": "create", "operation": "po_session_create",
         "fingerprint": session_fingerprint("codex", "chosen-model", "high"),
         "session_id": "open", "seq": None, "created_at": MOMENT},
        {"request_id": "send", "operation": "po_send", "fingerprint": send_fingerprint("open", feed[0]["text"]),
         "session_id": "open", "seq": 1, "created_at": MOMENT},
        {"request_id": "sprint", "operation": "po_sprint_session",
         "fingerprint": sprint_session_fingerprint("sprint:10"),
         "session_id": "open", "seq": None, "created_at": MOMENT},
    ]
    return {"po_sessions": sessions, "po_turns": turns, "po_feed": feed, "po_requests": requests}


def write_po(board, records):
    write_json(board / PO_FILE, records)
    summary_path = board / "export.json"
    summary = json.loads(summary_path.read_text())
    summary["po_sql"] = po_coverage(records, (board / PO_FILE).read_bytes())
    write_json(summary_path, summary)


def write_notes(board, content):
    """Declare a PO notes capture beside a board export, as `export_board` does; None is a confirmed absence."""
    (board / NOTES_CHECKPOINT_FILE).unlink(missing_ok=True)
    summary_path = board / "export.json"
    summary = json.loads(summary_path.read_text())
    summary["po_notes"] = write_notes_capture(board, content)
    write_json(summary_path, summary)


def failing_workspace_sync(workspace_dir: Path, synced: list[bool], failing: list[bool]):
    """An `os.fsync` that records each sync of `workspace_dir` and fails it while `failing[0]`."""
    target = workspace_dir.stat()
    real = os.fsync

    def fsync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            synced.append(failing[0])
            if failing[0]:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
        real(descriptor)

    return mock.patch("ummanu.board.po_notes_checkpoint.os.fsync", side_effect=fsync)


def seed_po(client, records):
    from psycopg.types.json import Jsonb

    with client.transaction():
        for table, rows in records.items():
            for row in rows:
                values = [Jsonb(value) if field == "metadata" and value is not None else value
                          for field, value in row.items()]
                client._execute(
                    f"INSERT INTO {table} ({', '.join(row)}) OVERRIDING SYSTEM VALUE "
                    f"VALUES ({', '.join(['%s'] * len(row))})", tuple(values),
                )
