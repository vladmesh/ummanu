"""A read-only protocol probe, not a Sprint storage implementation."""

from __future__ import annotations

import copy
from typing import Any


class ReadOnlySprintClient:
    def __init__(self, *, board: Any = None, rows: list[dict] | None = None) -> None:
        self.board = board
        self.rows = rows or []
        self.metadata: dict[int, Any] = {}
        self.comments: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.batches: list[list[tuple[str, dict]]] = []
        self.writes: list[str] = []

    def call(self, method: str, **params: Any) -> Any:
        self.calls.append((method, params))
        if method == "getProjectByName":
            return copy.deepcopy(self.board)
        if method == "getAllTasks":
            return copy.deepcopy(self.rows)
        if method == "getTaskByReference":
            return next((copy.deepcopy(row) for row in self.rows if row["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return copy.deepcopy(self.metadata.get(params["task_id"], {}))
        if method == "getAllComments":
            return copy.deepcopy(self.comments)
        self.writes.append(method)
        raise AssertionError(f"read attempted forbidden RPC: {method}")

    def call_batch(self, calls):
        calls = list(calls)
        self.batches.append(calls)
        return [self.call(method, **params) for method, params in calls]

    def _query(self, *args, **kwargs):
        raise AssertionError("unexpected SQL read in protocol probe")

    def _execute(self, *args, **kwargs):
        self.writes.append("SQL")
        raise AssertionError("read attempted SQL mutation")
