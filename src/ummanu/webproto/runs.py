"""What a product run is, and the durable store Ummanu keeps it in.

A run is one head this product raised for one card, plus everything that outlives the process:
workspace, run directory, pid file, log, result file and the backend's head record.

* Owned here, under `webproto/runs`, never in `dispatcher/production-state.json`: a product run is
  not a pipeline attempt (no claim, no card move, no attempt id). Dispatcher state is only read, by
  :mod:`ummanu.webproto.admission`.
* A request id owns one run of one operation from before the head exists: the record is written
  under the store lock before anything is spawned, so a repeated start returns the same run. A repeat
  with another operation or different inputs is a :class:`RequestMismatch`.
* No stored `state`: the record holds evidence and :mod:`ummanu.webproto.run_state` reads it.

See docs/PROTOCOLS.md, "Running the pipeline".
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ummanu._fsutil import file_lock
from ummanu.webproto.store_io import RunStoreError, write_document

#: Which side of the demonstration scenario a run is; there is no third.
WORKER = "worker"
REVIEWER = "reviewer"
RUN_ROLES = (WORKER, REVIEWER)

#: Where the whole product runtime keeps its own state inside the installation's data plane.
RUNS_RELATIVE = Path("webproto") / "runs"
WORKSPACES_RELATIVE = Path("webproto") / "workspaces"
HEADS_RELATIVE = Path("webproto") / "heads"

#: The file a head writes its result into, inside its run directory. The head is told the path; there
#: is no other place a result may appear.
RESULT_NAME = "result.json"

#: How long a run may take before the product ends its head, so a head that never finishes ends.
DEFAULT_DEADLINE_SECONDS = 60.0 * 60.0


#: The phases of one product run, in order:
#:
#: ``claimed``     a request id owns a run id and its paths; nothing provisioned or spawned;
#: ``raising``     write-ahead: the run directory exists and the record can address and stop a head
#:                 from disk alone; set immediately before a spawn, so a process may exist from here;
#: ``raised``      a head came up and the backend's record of it is bound to this run;
#: ``unresolved``  the run should be closed but could not be confirmed closed; a head may be alive.
#:                 Not terminal (see :mod:`ummanu.webproto.lifecycle`);
#: ``settled``     terminal, once and forever.
CLAIMED = "claimed"
RAISING = "raising"
RAISED = "raised"
UNRESOLVED = "unresolved"
SETTLED = "settled"

PHASES = (CLAIMED, RAISING, RAISED, UNRESOLVED, SETTLED)

#: The two operations a request id may own; a request id is the idempotency key of one operation.
START_OPERATION = "run_start"
REVIEW_OPERATION = "run_review"


# `RunStoreError` lives in :mod:`ummanu.webproto.store_io` and stays importable from here
# (`IMPLEMENTATION_FAILURES` and others import it from this module).


class RequestMismatch(RunStoreError):
    """This request id already owns a different operation, or the same one with different inputs.

    Idempotency covers a retry of the same request only; another command reusing the id is refused
    rather than aliased to the first run.
    """


def request_fingerprint(operation: str, request: dict[str, Any]) -> str:
    """The identity of one request (operation plus inputs), digested so no caller input becomes a path."""
    payload = json.dumps(
        {"operation": operation, "request": {key: _text(value) for key, value in request.items()}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProductRun:
    """One head this product raised for one card. Frozen and JSON: the reader is another process."""

    run_id: str
    request_id: str
    ref: str
    project: str
    role: str
    profile: str
    adapter: str = ""
    runtime: str = ""
    #: The worker run a review answers (a review exists only for a worker run with a result).
    #: Empty for a worker run.
    parent_run_id: str = ""
    workspace: str = ""
    run_dir: str = ""
    pid_file: str = ""
    #: The supervisor's versioned journal (`run.started`, deliveries, `run.exited` with exit status).
    journal_path: str = ""
    #: The supervisor's own stderr log, for a failure that never reached the journal.
    log_path: str = ""
    result_path: str = ""
    #: The pid the substrate reported at spawn. Diagnostic only: liveness comes from the heartbeat.
    head_pid: int = 0
    supervisor_pid: int = 0
    started_at: float = 0.0
    deadline_at: float = 0.0
    #: A head record sufficient to address and stop this run's head from disk alone. Written before
    #: the spawn (`raising`) and replaced by the backend's record once the head is up.
    head_run: dict[str, Any] = field(default_factory=dict)
    #: The one field that says whether a process may exist.
    phase: str = CLAIMED
    #: Whether a head was ever confirmed up. Never unset, so a settled run still knows it owes a
    #: `product_run.started` event.
    head_raised: bool = False
    #: Why this run's ownership could not be resolved, when it could not. Empty otherwise.
    unresolved_reason: str = ""
    #: Fact one: this run is over (its process is provably gone, or none was spawned). Written only
    #: by :meth:`settled_as`; stored separately from `settled_state` so neither is read off the other.
    ended: bool = False
    #: Set once, by whoever first observed this run reach a terminal process state.
    settled_at: float = 0.0
    #: Fact two: how this run ended, one of the read layer's five values. `source_unavailable` is a
    #: valid ending. Whether the run is over is `ended`.
    settled_state: str = ""
    settled_reason: str = ""
    #: The evidence the ending was read from, kept so a settled run does not change after a sweep
    #: and its `product_run.finished` event is rebuilt identically when republished.
    settled_exit: dict[str, Any] = field(default_factory=dict)
    settled_result: dict[str, Any] = field(default_factory=dict)

    @property
    def raised(self) -> bool:
        """Whether a head was ever confirmed up; not `bool(self.head_run)`, which is written pre-spawn."""
        return self.head_raised

    @property
    def addressable(self) -> bool:
        """Whether this record can address a head at all -- the write-ahead has been written."""
        return bool(self.head_run) and self.phase in (RAISING, RAISED, UNRESOLVED)

    @property
    def unresolved(self) -> bool:
        return self.phase == UNRESOLVED

    def at_phase(self, phase: str, **changes: Any) -> ProductRun:
        """The same run in another phase. The only way `phase` is ever written."""
        return replace(self, phase=phase, **changes)


    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "ref": self.ref,
            "project": self.project,
            "role": self.role,
            "profile": self.profile,
            "adapter": self.adapter,
            "runtime": self.runtime,
            "parent_run_id": self.parent_run_id,
            "workspace": self.workspace,
            "run_dir": self.run_dir,
            "pid_file": self.pid_file,
            "journal_path": self.journal_path,
            "log_path": self.log_path,
            "result_path": self.result_path,
            "head_pid": self.head_pid,
            "supervisor_pid": self.supervisor_pid,
            "started_at": self.started_at,
            "deadline_at": self.deadline_at,
            "head_run": dict(self.head_run),
            "phase": self.phase,
            "head_raised": self.head_raised,
            "unresolved_reason": self.unresolved_reason,
            "ended": self.ended,
            "settled_at": self.settled_at,
            "settled_state": self.settled_state,
            "settled_reason": self.settled_reason,
            "settled_exit": dict(self.settled_exit),
            "settled_result": dict(self.settled_result),
        }

    @classmethod
    def from_json(cls, payload: Any) -> ProductRun:
        if not isinstance(payload, dict):
            raise RunStoreError("a product run record is an object, and this is not one")
        head_run = payload.get("head_run") if isinstance(payload.get("head_run"), dict) else {}
        return cls(
            run_id=_text(payload.get("run_id")),
            request_id=_text(payload.get("request_id")),
            ref=_text(payload.get("ref")),
            project=_text(payload.get("project")),
            role=_text(payload.get("role")),
            profile=_text(payload.get("profile")),
            adapter=_text(payload.get("adapter")),
            runtime=_text(payload.get("runtime")),
            parent_run_id=_text(payload.get("parent_run_id")),
            workspace=_text(payload.get("workspace")),
            run_dir=_text(payload.get("run_dir")),
            pid_file=_text(payload.get("pid_file")),
            journal_path=_text(payload.get("journal_path")),
            log_path=_text(payload.get("log_path")),
            result_path=_text(payload.get("result_path")),
            head_pid=_int(payload.get("head_pid")),
            supervisor_pid=_int(payload.get("supervisor_pid")),
            started_at=_float(payload.get("started_at")),
            deadline_at=_float(payload.get("deadline_at")),
            head_run=head_run,
            phase=_phase(payload, head_run),
            head_raised=_flag(payload.get("head_raised"), bool(head_run)),
            unresolved_reason=_text(payload.get("unresolved_reason")),
            # A pre-split record carries only the value, written only by a settle: read it as over.
            ended=_flag(payload.get("ended"), bool(_text(payload.get("settled_state")))),
            settled_at=_float(payload.get("settled_at")),
            settled_state=_text(payload.get("settled_state")),
            settled_reason=_text(payload.get("settled_reason")),
            settled_exit=_mapping(payload.get("settled_exit")),
            settled_result=_mapping(payload.get("settled_result")),
        )

    def with_head(
        self,
        head_run: dict[str, Any],
        *,
        phase: str,
        head_pid: int = 0,
        supervisor_pid: int = 0,
        head_raised: bool | None = None,
    ) -> ProductRun:
        return replace(
            self,
            head_run=dict(head_run),
            phase=phase,
            head_pid=head_pid or self.head_pid,
            supervisor_pid=supervisor_pid or self.supervisor_pid,
            head_raised=self.head_raised if head_raised is None else head_raised,
        )

    def in_doubt(self, reason: str) -> ProductRun:
        """The same run, recorded as one whose ownership could not be resolved."""
        return replace(self, phase=UNRESOLVED, unresolved_reason=reason)

    def settled_as(
        self,
        state: str,
        reason: str,
        *,
        now: float,
        exit_status: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> ProductRun:
        """The same run, settled. Idempotent: an already-settled run keeps its first settlement."""
        if self.ended:
            return self
        return replace(
            self,
            phase=SETTLED,
            unresolved_reason="",
            ended=True,
            settled_state=state,
            settled_reason=reason,
            settled_at=now,
            settled_exit=dict(exit_status or {}),
            settled_result=dict(result or {}),
        )


class RunStore:
    """Every product run of one installation, keyed by run id and by the request that asked for it.

    `runs/<run_id>.json` is the record; `runs/requests/<digest>.json` maps a request id (digested, so
    it never becomes a path) to its run. One store-wide lock is held across read-decide-write, so
    "does this request id already own a run" is answered once.
    """

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.root = Path(os.fspath(data_dir)) / RUNS_RELATIVE
        self.requests = self.root / "requests"
        self.lock_path = self.root / ".runs.lock"

    # -- paths -----------------------------------------------------------------------------

    def _run_path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or run_id in (".", ".."):
            raise RunStoreError(f"a product run is named by a run id, not by {run_id!r}")
        return self.root / f"{run_id}.json"

    def _request_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.requests / f"{digest}.json"

    def _prepare(self) -> None:
        try:
            self.requests.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunStoreError(f"the product run store at {self.root} could not be opened: {exc}") from None

    # -- reads -----------------------------------------------------------------------------

    def get(self, run_id: str) -> ProductRun | None:
        """One run, or nothing. An unreadable record is a store failure, never a missing run."""
        path = self._run_path(run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the product run record at {path} could not be read: {exc}") from None
        return ProductRun.from_json(payload)

    def by_request(
        self, request_id: str, *, operation: str = "", fingerprint: str = ""
    ) -> ProductRun | None:
        """The run a request id owns, if it owns one and this is the same request.

        With `operation`/`fingerprint`, a disagreeing record raises :class:`RequestMismatch`. With
        neither, the record is read as it stands (for listings and repairs).
        """
        path = self._request_path(request_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the product run request index at {path} could not be read: {exc}") from None
        if not isinstance(payload, dict):
            raise RunStoreError(f"the product run request index at {path} is not an object")
        owned_operation = _text(payload.get("operation"))
        owned_fingerprint = _text(payload.get("fingerprint"))
        if operation and owned_operation and owned_operation != operation:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a {owned_operation} run; a request id is "
                f"the idempotency key of one operation and cannot be reused for {operation}"
            )
        if fingerprint and owned_fingerprint and owned_fingerprint != fingerprint:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a {owned_operation or operation} run made "
                "with different inputs; a repeat is a retry of the same request, not a new one"
            )
        run_id = _text(payload.get("run_id"))
        return self.get(run_id) if run_id else None

    def for_ref(self, ref: str) -> list[ProductRun]:
        """Every run of one card, oldest first. What a card page shows, and what admission reads."""
        runs: list[ProductRun] = []
        try:
            names = sorted(path for path in self.root.glob("*.json"))
        except OSError as exc:
            raise RunStoreError(f"the product run store at {self.root} could not be listed: {exc}") from None
        for path in names:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                raise RunStoreError(f"the product run record at {path} could not be read: {exc}") from None
            run = ProductRun.from_json(payload)
            if run.ref == ref:
                runs.append(run)
        return sorted(runs, key=lambda run: (run.started_at, run.run_id))

    # -- writes ----------------------------------------------------------------------------

    def claim(
        self,
        request_id: str,
        build: Any,
        *,
        operation: str = "",
        fingerprint: str = "",
    ) -> tuple[ProductRun, bool]:
        """The run this request id owns, creating it under the lock when it owns none yet.

        `build` gets a fresh run id and returns the record, so every path is durable before any
        process exists. The boolean is True only for the call that created the run; a repeat spawns
        nothing.
        """
        self._prepare()
        with file_lock(self.lock_path):
            existing = self.by_request(request_id, operation=operation, fingerprint=fingerprint)
            if existing is not None:
                return existing, False
            run = build(new_run_id())
            if not isinstance(run, ProductRun):
                raise RunStoreError("a product run record is built as a ProductRun")
            self._write(run)
            write_document(
                self._request_path(request_id),
                json.dumps(
                    {
                        "request_id": request_id,
                        "run_id": run.run_id,
                        "operation": operation,
                        "fingerprint": fingerprint,
                    },
                    sort_keys=True,
                ),
            )
            return run, True

    def save(self, run: ProductRun) -> ProductRun:
        """Replace one run's record. Taken under the same lock every other write is."""
        self._prepare()
        with file_lock(self.lock_path):
            self._write(run)
        return run

    def settle(
        self,
        run_id: str,
        state: str,
        reason: str,
        *,
        now: float,
        exit_status: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> tuple[ProductRun, bool]:
        """Record a run's terminal state exactly once. True means this caller owes the terminal event."""
        self._prepare()
        with file_lock(self.lock_path):
            current = self.get(run_id)
            if current is None:
                raise RunStoreError(f"there is no product run {run_id!r} to settle")
            if current.ended:
                return current, False
            settled = current.settled_as(
                state, reason, now=now, exit_status=exit_status, result=result
            )
            self._write(settled)
            return settled, True

    def _write(self, run: ProductRun) -> None:
        write_document(self._run_path(run.run_id), json.dumps(run.to_json(), sort_keys=True, indent=2))


def new_run_id() -> str:
    """A product run's identity. Prefixed so a run directory is never mistaken for a pipeline one."""
    return "pr-" + uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _phase(payload: dict[str, Any], head_run: dict[str, Any]) -> str:
    """This record's phase; a record from before phases reads as `settled`, `raised` or `claimed`."""
    declared = _text(payload.get("phase"))
    if declared in PHASES:
        return declared
    if _text(payload.get("settled_state")):
        return SETTLED
    return RAISED if head_run else CLAIMED


def _flag(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
