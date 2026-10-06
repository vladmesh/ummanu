"""The two pause operations: `pause_drain` sets the pipeline-wide soft pause, `pause_resume` lifts it.

There is no freeze operation and no mode parameter: `pause_drain` passes the literal
:data:`~ummanu.webproto.pause_reads.DRAIN`, and a freeze is only `ummanu pause freeze`. Every rule
(tick lock, same-mode no-op, `pause_conflict`, head stop/relaunch, TTL) stays in
`ummanu.dispatch.pause_ops`; this layer adds no flag, lock, store or request index.

`action` is the dispatcher's own answer (`paused`, `noop`, `resumed`), never inferred from a flag
read outside the tick lock. When the command completed but rendering the status afterwards failed
(`PauseCommandCompleted`), the decision is still reported, with a warning. See docs/PROTOCOLS.md,
"The pause as protocol operations".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.dispatch.bootstrap import runtime_from_args
from ummanu.dispatch.pause_ops import PauseCommandCompleted
from ummanu.dispatch.pause_ops import pause as _pause
from ummanu.dispatch.pause_ops import resume as _resume
from ummanu.dispatch.types import DispatcherError, HostError
from ummanu.webproto import sources
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.errors import OwnerConflict, RuntimeUnavailable, ValidationRefused
from ummanu.webproto.pause_reads import (
    DRAIN,
    DRAIN_CONTRACT,
    FREEZE_CONTRACT,
    SCHEMA_VERSION,
    PauseReadLayer,
    extent,
)

#: The operation names.
PAUSE_DRAIN_OPERATION = "pause_drain"
PAUSE_RESUME_OPERATION = "pause_resume"

#: The codes each pause operation can refuse with; `tests/test_web_pause_protocol.py` drives each
#: one and checks the operations table in `docs/PROTOCOLS.md` against it. The layer-wide
#: `backend_unavailable` from :mod:`ummanu.webproto.boundary` is not listed per operation.
PAUSE_ERRORS: dict[str, tuple[str, ...]] = {
    PAUSE_DRAIN_OPERATION: ("validation", "owner_conflict", "backend_unavailable"),
    PAUSE_RESUME_OPERATION: ("validation", "backend_unavailable"),
    "pause_state": ("validation",),
    "pause_scope": ("validation",),
}

#: How a `DispatcherError` code maps to this layer's codes (a mapping, never a re-decision).
#: `pause_conflict` is `owner_conflict`: a well-formed request refused on the state of the world.
#: `invalid_instance`/`invalid_heads` are `validation` so callers keep the exit status 2 that
#: `runtime_from_args` gave for a config that does not validate.
_CODES: dict[str, Any] = {
    "validation": ValidationRefused,
    "usage": ValidationRefused,
    "pause_conflict": OwnerConflict,
    "invalid_instance": ValidationRefused,
    "invalid_heads": ValidationRefused,
}


class PauseOperationLayer(ProtocolBoundary):
    """One installation's pause operations; construction does no I/O.

    `runtime` lets a test or caller supply its own dispatcher runtime; it is not a mode.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        runtime: Any | None = None,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
        host_mode: str = "real",
        owner: str = "ummanu-dispatcher",
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._given_runtime = runtime
        self._board_client = board_client
        self._clock = clock
        self._host_mode = host_mode
        self._owner = owner

    # -- operations ---------------------------------------------------------------------------

    def pause_drain(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Put the pipeline into the soft pause, or report that it already was.

        No `mode` argument by contract: only a drain. A drain stops no running head; it stops
        Ready claims and background role dispatch.
        """
        now = self._clock()
        runtime = self._runtime()
        result = self._perform(lambda: _pause(runtime, mode=DRAIN, actor=actor, reason=reason))
        return self._document(PAUSE_DRAIN_OPERATION, result, actor=actor, now=now, restored=None)

    def pause_resume(self, *, actor: str) -> dict[str, Any]:
        """Clear the pause and report what `resume` put back (`restored`).

        If the command completed but its answer was lost (:meth:`_perform`), a freeze's lists are
        `null`, not empty: what was put back is unknown, not nothing.
        """
        now = self._clock()
        runtime = self._runtime()
        result = self._perform(lambda: _resume(runtime, actor=actor))
        return self._document(
            PAUSE_RESUME_OPERATION,
            result,
            actor=actor,
            now=now,
            restored=_restored(result),
        )

    def _runtime(self) -> Any:
        """The dispatcher runtime, built per call by the same factory as `ummanu dispatcher`."""
        if self._given_runtime is not None:
            return self._given_runtime
        return self._call(
            lambda: runtime_from_args(
                str(self.instance),
                str(self._data_dir) if self._data_dir is not None else None,
                host_mode=self._host_mode,
                owner=self._owner,
            )
        )

    # -- the pieces an operation is made of ----------------------------------------------------

    @staticmethod
    def _call(operation: Callable[[], Any]) -> Any:
        """Run one dispatcher call, mapping `DispatcherError` via :data:`_CODES`.

        `HostError` becomes `backend_unavailable`.
        """
        try:
            return operation()
        except PauseCommandCompleted:
            # A completed command, not a failure: `_perform` reports it.
            raise
        except DispatcherError as exc:
            raise _CODES.get(exc.code, RuntimeUnavailable)(exc.message) from None
        except HostError as exc:
            raise RuntimeUnavailable(f"the host could not answer this pause command: {exc}") from None

    def _perform(self, operation: Callable[[], Any]) -> dict[str, Any]:
        """Run one pause command; if it completed but the status render failed, report its decision.

        The dispatcher writes the flag, then renders status; a corrupt production record makes the
        render raise after the pause took. `PauseCommandCompleted` carries the decision made under
        the tick lock, reported here with a warning; nothing is read to establish it. Any other
        failure (including `validation` and `pause_conflict`, raised before any write) propagates.
        """
        try:
            return self._call(operation)
        except PauseCommandCompleted as completed:
            return {
                **completed.decision,
                # Lists a freeze's resume produced are unknown, not empty.
                "reported": False,
                "warnings": [
                    *completed.warnings,
                    (
                        f"the command completed -- {_did(completed.decision)} -- but the dispatcher "
                        f"could not render the pipeline state afterwards: {completed.cause}."
                        " What could be established is on `state`, where the source that did not "
                        "answer is marked unavailable"
                    ),
                ],
            }

    def _document(
        self,
        operation: str,
        result: dict[str, Any],
        *,
        actor: str,
        now: float,
        restored: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """The `pause_command` document: what the command did, plus the embedded `pause_state` read."""
        action = str(result.get("action") or "")
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "pause_command",
            "observed_at": sources.isoformat(now),
            "operation": operation,
            "actor": actor,
            # `noop` (a repeat that changed nothing) is distinct from a change; refusals never get here.
            "action": action,
            "changed": action not in {"noop", ""},
            "restored": restored,
            # Warnings from the pause itself, carried through unchanged.
            "warnings": list(result.get("warnings") or []),
            "extent": extent(),
            "modes": {"drain": DRAIN_CONTRACT, "freeze": FREEZE_CONTRACT},
            "state": self._reads().pause_state(),
        }

    def _reads(self) -> PauseReadLayer:
        """The read layer this operation answers through, built with this layer's own seams."""
        return PauseReadLayer(
            self.instance,
            data_dir=self._data_dir,
            board_client=self._board_client,
            clock=self._clock,
        )


def _did(decision: dict[str, Any]) -> str:
    """Phrase the decision made under the lock for the operator's warning."""
    action = str(decision.get("action") or "")
    if action == "paused":
        return "the pipeline-wide pause was set"
    if action == "resumed":
        mode = str(decision.get("resumed_mode") or "")
        return f"the {mode} was lifted" if mode else "the pause was lifted"
    if decision.get("step") == "resume":
        return "the pipeline was not paused, so nothing was lifted"
    return "the pipeline already held the mode asked for, so nothing was written"


def _restored(result: dict[str, Any]) -> dict[str, Any]:
    """What a resume put back, copied from `resume`'s own lists with a statement of the case.

    When the answer was lost (`reported` false), a freeze's lists are `null` (unknown); for a
    drain or an unpaused pipeline they stay `[]`, since a drain stops nothing.
    """
    mode = str(result.get("resumed_mode") or "") or None
    unread = not result.get("reported", True) and mode == "freeze"
    relaunched = None if unread else list(result.get("relaunched") or [])
    parked = None if unread else list(result.get("parked") or [])
    skipped = None if unread else list(result.get("skipped") or [])
    if unread:
        statement = (
            "the freeze was lifted, but the resume's own report could not be read back, so what it "
            "put back is not established here; the heads that are up now are on `state`"
        )
    elif mode is None:
        statement = "the pipeline was not paused, so this resume lifted nothing and put nothing back"
    elif mode == DRAIN:
        statement = (
            "the drain stopped no head, so there was nothing to put back; the tick claims Ready "
            "cards and dispatches background roles again"
        )
    else:
        statement = (
            f"the freeze was lifted: {len(relaunched)} head(s) were relaunched in their existing "
            f"workspaces, {len(parked)} were left to the next tick, and {len(skipped)} were not "
            "brought back"
        )
    return {
        "resumed_mode": mode,
        "relaunched": relaunched,
        "parked": parked,
        "skipped": skipped,
        "observers_resumed": None if unread else list(result.get("observers_resumed") or []),
        "statement": statement,
    }


__all__ = [
    "PAUSE_DRAIN_OPERATION",
    "PAUSE_ERRORS",
    "PAUSE_RESUME_OPERATION",
    "PauseOperationLayer",
]
