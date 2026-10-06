"""Typed failures of the read and operation layer.

A failure is an exception with a stable ``code`` (the task protocol's ``not_found``, ``validation``,
``backend_unavailable``, ...), never a status number; the transport maps codes onto its protocol.
A source that cannot answer part of a snapshot is a field of the result instead
(:mod:`ummanu.webproto.sources`), so the parts that did answer still render.
"""

from __future__ import annotations

from typing import Any


class ReadError(Exception):
    """A refused read or operation, with the protocol code its callers already know.

    Optional ``data`` is a JSON object with a `reason` token and, where one exists, the safe action
    the caller may take. A refusal with nothing to add carries no `data` key.
    """

    code = "read_error"

    def __init__(self, message: str, *, data: dict[str, Any] | None = None) -> None:
        self.message = message
        self.data = dict(data or {})
        super().__init__(message)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            payload["data"] = self.data
        return payload


class TaskNotFound(ReadError):
    """The board answered and holds no card under this reference."""

    code = "not_found"


class InvalidCursor(ReadError):
    """A cursor this layer did not issue, or one the journal can no longer place.

    Never silently restarted from the beginning: that would re-read or skip events.
    """

    code = "validation"


class InstallationUnavailable(ReadError):
    """The instance config itself does not validate, so nothing below it can be read."""

    code = "backend_unavailable"


# -- operations: same base, so one `ReadError` code table and one catch cover reads and writes ----


class ValidationRefused(ReadError):
    """The operation was asked for something it cannot do: a missing input, an unusable profile."""

    code = "validation"


class RunNotFound(ReadError):
    """No product run of this installation is named by this identifier."""

    code = "not_found"


class HeadRunNotFound(ReadError):
    """The card recorded no head run under this identifier: not its own, or no run at all."""

    code = "not_found"


class OwnerConflict(ReadError):
    """Somebody else owns this card or run; a second owner is not created.

    Not `validation`: the request is well formed, refused on current state, and admitted on retry
    once the other owner finishes. Decided in :mod:`ummanu.webproto.admission`.
    """

    code = "owner_conflict"


class RuntimeUnavailable(ReadError):
    """The product runtime could not raise, reach or record a head, and says which.

    Only a failure that leaves no run behind is an error; a run whose head died has the state
    `process_failed`.
    """

    code = "backend_unavailable"


# -- sprints ------------------------------------------------------------------------------------


class OperationPending(ReadError):
    """A part-done, durably repairable operation; repeat it with the same request id.

    The writer keeps the staged intent of a sprint create (row, fields, reference), so repeating
    this request resumes the existing row; a new request id would open a second sprint. Uses the
    `backend_unavailable` code; :attr:`~ReadError.data` carries the reason and action.
    """

    code = "backend_unavailable"


class IdentityRefused(ReadError):
    """A write refused on who is asking, carrying the writer's own code unchanged.

    Not `validation`: the request is well formed. `role_masquerade` means write as the observer;
    `observer_identity_unbound` / `observer_sprint_mismatch` mean a head writing outside the sprint
    it was launched for.
    """

    code = "forbidden"


class RoleMasquerade(IdentityRefused):
    """A PO write in the observer's name; the observer writes as `--role observer`."""

    code = "role_masquerade"


class ObserverIdentityUnbound(IdentityRefused):
    """A write of role `observer` from a head no launcher bound to a sprint."""

    code = "observer_identity_unbound"


class ObserverSprintMismatch(IdentityRefused):
    """A write of role `observer` about a sprint other than the one its head was launched for."""

    code = "observer_sprint_mismatch"


#: The writer codes an :class:`IdentityRefused` carries, each by its own class.
IDENTITY_REFUSALS: dict[str, type[IdentityRefused]] = {
    cls.code: cls for cls in (RoleMasquerade, ObserverIdentityUnbound, ObserverSprintMismatch)
}


class OwnerEventMissing(ReadError):
    """The board store answered and holds no owner event under this id."""

    code = "not_found"


# -- PO head ------------------------------------------------------------------------------------


class PoSessionNotFound(ReadError):
    """The board store answered and holds no PO session under this id."""

    code = "not_found"


class PoTurnInProgress(ReadError):
    """A turn is already running in this PO session; nothing was written."""

    code = "owner_conflict"


class PoRequestConflict(ReadError):
    """A /po request id already belongs to another operation or other inputs; nothing was written.

    Not `owner_conflict`: repeating it can never succeed, so a client must not wait and retry.
    """

    code = "request_conflict"


#: `data` of a /po refusal known to have written nothing. Only after such a refusal does a web form
#: get a fresh request id; any other refusal keeps it, so a resend is a replay
#: (`ummanu.web.app._keeps_request_id`).
NOTHING_WRITTEN: dict[str, Any] = {"nothing_written": True}


class PoOutcomeUnknown(ReadError):
    """A /po write reached the PO service and no answer came back, so it may have been carried out.

    The safe move is the same request again (same request id, answered as a replay; stop and close
    are idempotent), never a new request id; `data` says so.
    """

    code = "backend_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(message, data={"reason": "outcome_unknown", "action": "repeat_same_request"})


class PoSessionClosed(ReadError):
    """The owner closed this PO session; a message into it starts no turn and nothing was written.

    Unlike `owner_conflict` it never clears by waiting: a closed session is not reopened.
    """

    code = "session_closed"
