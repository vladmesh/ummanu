"""Reference numbering for Pipeline cards (`<project>-<n>`) and sprints (`sprint:<n>`).

A new reference is the first number above every number the family has used, counted over the
board's open and archived rows (forgetting archived rows re-issues references). Allocation is not
the uniqueness guarantee: callers ask the backend whether the exact reference is claimed before
writing it, and refuse when it is.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

# Propagated to every agent process by the role environment, so writers in different processes
# resolve one lock file.
DATA_DIR_ENV = "UMMANU_DATA_DIR"


class BoardRowsUnavailable(RuntimeError):
    """A row enumeration answered with something that is not a list of rows."""


def board_rows(call: Callable[..., Any], project_id: int) -> list[dict[str, Any]]:
    """Every row of one board, open and archived alike.

    The client splits rows into status 1 (open) and 0 (closed, incl. archived) with no
    complete-set status; both are read and the first copy of each task id is kept.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for status_id in (1, 0):
        answer = call("getAllTasks", project_id=project_id, status_id=status_id)
        if not isinstance(answer, list):
            raise BoardRowsUnavailable("the board returned an invalid task list")
        for row in answer:
            if not isinstance(row, dict):
                continue
            identifier = row.get("id")
            if identifier is not None:
                key = str(identifier)
                if key in seen:
                    continue
                seen.add(key)
            rows.append(row)
    return rows


def next_reference(rows: Iterable[Mapping[str, Any]], prefix: str) -> str:
    """The first reference of this family above every number the given rows already use."""
    pattern = re.compile(rf"{re.escape(prefix)}(\d+)$")
    used = (pattern.fullmatch(str(row.get("reference") or "")) for row in rows)
    return f"{prefix}{max((int(match.group(1)) for match in used if match), default=0) + 1}"


@contextlib.contextmanager
def reference_allocation_lock(data_dir: Path | str | None = None) -> Iterator[None]:
    """Serialize allocate, claim and write across every local writer of one board.

    The card client has no compare-and-swap on a reference, so every local writer of a board takes
    this one file lock around all three steps; otherwise two processes can both allocate, find the
    reference unclaimed and create a duplicate. The lock lives in the data directory (`data_dir`, or
    `UMMANU_DATA_DIR`).
    """
    root = (
        Path(data_dir) if data_dir else Path(os.environ.get(DATA_DIR_ENV) or Path.home() / "ummanu-data")
    )
    board = root / "board"
    board.mkdir(parents=True, exist_ok=True)
    with (board / ".create.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
