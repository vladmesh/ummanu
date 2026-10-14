"""Request-id paths, document I/O and retry matching; the stores own records and locks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ummanu.webproto.store_io import RunStoreError, write_document


class RequestMismatch(RunStoreError):
    """A request id already owns another operation or different inputs."""


class RequestIndex:
    """A directory of request documents, with no preparation or locking of its own.

    A missing file returns None. Existing JSON, including null, returns a path/payload pair
    for the domain store to convert before matching. Labels and nouns retain domain diagnostics;
    indentation retains the two released document formats.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def read(self, request_id: str, *, label: str) -> tuple[Path, Any] | None:
        path = self.path(request_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the {label} at {path} could not be read: {exc}") from None
        return path, payload

    def write(self, request_id: str, payload: dict[str, Any], *, indent: int | None = None) -> None:
        write_document(self.path(request_id), json.dumps(payload, sort_keys=True, indent=indent))

    @staticmethod
    def match(
        request_id: str,
        owned_operation: str,
        owned_fingerprint: str,
        *,
        operation: str,
        fingerprint: str,
        noun: str,
    ) -> None:
        """Optional constraints apply only to nonempty stored fields, operation first."""
        if operation and owned_operation and owned_operation != operation:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a {owned_operation} {noun}; a request id "
                f"is the idempotency key of one operation and cannot be reused for {operation}"
            )
        if fingerprint and owned_fingerprint and owned_fingerprint != fingerprint:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a {owned_operation or operation} {noun} "
                "made with different inputs; a repeat is a retry of the same request, not a new one"
            )
