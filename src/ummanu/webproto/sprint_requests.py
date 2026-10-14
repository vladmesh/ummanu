"""What a request id owns when the thing it asked for is a sprint.

The same claim-before-act idempotency as `RunStore`, but the record holds only what this layer
cannot get elsewhere: the claimed request and the reference of the sprint it produced. The sprint
itself is read from the board. The reference is a shortcut, not the guard against a second sprint:
the same request id is passed to `SprintWriter.create`, whose staged transaction resumes a
half-written row. Failures use the layer's existing `RunStoreError` / `RequestMismatch`, and writes
go through :func:`ummanu.webproto.store_io.write_document`, so the boundary translates them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ummanu._fsutil import file_lock
from ummanu.webproto._values import _float, _text
from ummanu.webproto.request_index import RequestIndex
from ummanu.webproto.store_io import RunStoreError

#: The one operation a record can be claimed under. Stored, so an id reused for another operation is
#: refused rather than answered with this one's sprint.
SPRINT_CREATE_OPERATION = "sprint_create"

#: Where the request index lives in the installation's data plane, beside the product runs.
SPRINT_REQUESTS_RELATIVE = Path("webproto") / "sprint-requests"


@dataclass(frozen=True, slots=True)
class SprintRequest:
    """One claimed request, and the sprint it produced once it produced one."""

    request_id: str
    operation: str
    fingerprint: str
    #: The sprint this request created. Empty means claimed with the outcome not established here,
    #: never that nothing was created.
    reference: str = ""
    claimed_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "operation": self.operation,
            "fingerprint": self.fingerprint,
            "reference": self.reference,
            "claimed_at": self.claimed_at,
        }

    @classmethod
    def from_json(cls, payload: Any) -> SprintRequest:
        if not isinstance(payload, dict):
            raise RunStoreError("a sprint request record is an object, and this is not one")
        return cls(
            request_id=_text(payload.get("request_id")),
            operation=_text(payload.get("operation")),
            fingerprint=_text(payload.get("fingerprint")),
            reference=_text(payload.get("reference")),
            claimed_at=_float(payload.get("claimed_at")),
        )


class SprintRequestStore:
    """Every sprint request of one installation, keyed by the request id that made it.

    Ids are digested so a caller's id never becomes a path; the lock is held across
    read-decide-write so a request id cannot be claimed twice.
    """

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.root = Path(os.fspath(data_dir)) / SPRINT_REQUESTS_RELATIVE
        self.lock_path = self.root / ".sprint-requests.lock"
        self.index = RequestIndex(self.root)

    # -- paths -----------------------------------------------------------------------------

    def _prepare(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunStoreError(f"the sprint request store at {self.root} could not be opened: {exc}") from None

    # -- reads -----------------------------------------------------------------------------

    def by_request(
        self, request_id: str, *, operation: str = "", fingerprint: str = ""
    ) -> SprintRequest | None:
        """The request this id already owns, if it owns one and this is the same request.

        A repeat with a different operation or fingerprint is a :class:`~ummanu.webproto.request_index.RequestMismatch`.
        """
        document = self.index.read(request_id, label="sprint request index")
        if document is None:
            return None
        record = SprintRequest.from_json(document[1])
        self.index.match(
            request_id, record.operation, record.fingerprint,
            operation=operation, fingerprint=fingerprint, noun="request",
        )
        return record

    # -- writes ----------------------------------------------------------------------------

    def claim(
        self, request_id: str, *, operation: str, fingerprint: str, now: float = 0.0
    ) -> tuple[SprintRequest, bool]:
        """The request this id owns, claiming it under the lock when it owns none yet.

        The boolean says whether this call claimed it; `False` means a repeat found the first record.
        """
        self._prepare()
        with file_lock(self.lock_path):
            existing = self.by_request(request_id, operation=operation, fingerprint=fingerprint)
            if existing is not None:
                return existing, False
            record = SprintRequest(
                request_id=request_id,
                operation=operation,
                fingerprint=fingerprint,
                claimed_at=now,
            )
            self._write(record)
            return record, True

    def record_reference(self, request_id: str, reference: str) -> SprintRequest:
        """Name the sprint this request produced, once; a recorded reference is never replaced."""
        self._prepare()
        with file_lock(self.lock_path):
            record = self.by_request(request_id)
            if record is None:
                raise RunStoreError(f"there is no claimed sprint request {request_id!r} to complete")
            if record.reference:
                return record
            completed = replace(record, reference=reference)
            self._write(completed)
            return completed

    def _write(self, record: SprintRequest) -> None:
        self.index.write(record.request_id, record.to_json(), indent=2)
