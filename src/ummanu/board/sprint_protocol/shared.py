"""SQL operation boundary and stable lifecycle event/request identifiers."""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable
from typing import Any

from ummanu.kernel.errors import TaskError

SPRINT_REOPENED = "reopened"
SPRINT_CLOSED = "closed"
#: A committed event under the derived request id proves the knowledge closeout was written.
SPRINT_CLOSEOUT = "closeout_written"


def _sql_atomic(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Put one complete Sprint protocol operation in the SQL client's transaction."""

    @functools.wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request_id = kwargs.get("request_id")
        try:
            # `_depth` is this thread's own, so the check needs no lock around it.
            if self.client._depth:
                return method(self, *args, **kwargs)
            with self.client.transaction():
                # Admission, request claims and reservations share this transaction-scoped lock.
                self.client._execute("SELECT pg_advisory_xact_lock(%s)", (1_600,))
                return method(self, *args, **kwargs)
        except TaskError as exc:
            if isinstance(request_id, str):
                try:
                    self.transactions.drop(request_id)
                except TaskError:
                    pass
            if exc.code == "audit_pending":
                raise TaskError("backend_error", "PostgreSQL Sprint transaction rolled back", 1) from None
            raise
        except Exception as exc:  # noqa: BLE001 - the rollback is the public fact.
            if isinstance(request_id, str):
                try:
                    self.transactions.drop(request_id)
                except TaskError:
                    pass
            raise TaskError("backend_error", f"PostgreSQL Sprint transaction rolled back: {exc}", 1) from None

    return wrapped


def _close_archive_request_id(request_id: str, reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"{request_id}:sprint-close-archive:{digest}"


def _close_step_request_id(request_id: str, step: str, reference: str) -> str:
    """One derived id per step of a close, so a retry replays it instead of repeating it."""
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"{request_id}:sprint-close-{step}:{digest}"
