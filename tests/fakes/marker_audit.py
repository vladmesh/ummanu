"""In-memory audit support for writer marker tests."""

from __future__ import annotations

import contextlib
import copy

from tests.fakes.board import MemoryAudit
from ummanu.board.audit_contract import require_claim


class MarkerAudit(MemoryAudit):
    """The generic and typed audit contracts over the same memory owner."""

    def __init__(self, root):
        super().__init__()
        self.board_dir = root / "board"

    def stage(self, request_id, event):
        self._pending[request_id] = copy.deepcopy(event)

    def events(self, reference="", kind=None, **kwargs):
        return [event for event in super().events(reference) if kind is None or event["kind"] == kind]

    def pending_events(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(list(self._pending.values()))

    def pending_marker_owner(self, reference, content, *, request_id):
        return None

    def marker_comment_lock(self, reference):
        return contextlib.nullcontext()

    @staticmethod
    def require_claim(existing, **fields):
        require_claim(existing, **fields)
