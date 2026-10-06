"""The transport-independent layer: the one place a dashboard, a bot or a CLI reads and drives the system.

Every answer lives here once and knows nothing about who is asking; transports only render it.

* Reads (:class:`~ummanu.webproto.reads.ReadLayer`): `system_snapshot`, `task_snapshot`,
  `task_events` (a page plus an opaque cursor).
* Run operations (:class:`~ummanu.webproto.ops.OperationLayer`): `run_start`, `run_review`,
  `run_state`.
* Sprints (:mod:`ummanu.webproto.sprint_ops`, :mod:`ummanu.webproto.sprint_reads`): `sprint_create`,
  `sprint_options`, `sprint_state`, `sprint_list`.
* Pause (:mod:`ummanu.webproto.pause_ops`, :mod:`ummanu.webproto.pause_reads`): `pause_drain`,
  `pause_resume`, `pause_state`, `pause_scope`.
* Commands (:class:`~ummanu.webproto.command_reads.CommandReadLayer`): `command_history`,
  `command_request`.

Invariants (each has a test):

* Reads write nothing to the board, dispatcher state, journal or installation, and take no actor.
* No HTTP, sockets, rendering or templates in this surface. Failures are typed codes
  (:mod:`ummanu.webproto.errors`) and per-section availability (:mod:`ummanu.webproto.sources`),
  never status codes; :mod:`ummanu.webproto.boundary` maps any implementation failure to
  `backend_unavailable`.
* Sources fail apart: one dead source blanks its section, not the page.
* Liveness is process state; a pane, terminal or window is never evidence.
* Ummanu owns a run (workspace, process, logs, result); nothing on the start or result path speaks to
  Orca. A request id owns one run, and :mod:`ummanu.webproto.admission` is the single start gate.
  A run's events go onto the board's own journal.
* Sprint rules stay with `SprintWriter.create`; opening a sprint with an observer starts it (the
  production tick raises the head); an absent executor pin stays absent, never `""`.
* The pause is pipeline-wide on every answer, a drain stops no running head, and freeze is never
  reached implicitly (a mode change while paused is `owner_conflict`).
* Command reads open no second store, never repair what they report on, and never fold `unknown`
  into another answer.

`ummanu web-read`, `ummanu web-run` (:mod:`ummanu.webproto.commands`) and the pause commands are
callers of this layer, not part of it. See docs/PROTOCOLS.md, "Reading the pipeline" onward.
"""

from __future__ import annotations

from ummanu.webproto.agents import AGENT_STATES, LIVENESS_INVARIANT
from ummanu.webproto.boundary import IMPLEMENTATION_FAILURES, ProtocolBoundary
from ummanu.webproto.command_reads import OPERATION_IDENTITY, CommandReadLayer
from ummanu.webproto.cursor import Cursor
from ummanu.webproto.errors import (
    InstallationUnavailable,
    InvalidCursor,
    OperationPending,
    OwnerConflict,
    ReadError,
    RunNotFound,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from ummanu.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from ummanu.webproto.ops import OperationLayer
from ummanu.webproto.pause_ops import PauseOperationLayer
from ummanu.webproto.pause_reads import PauseReadLayer
from ummanu.webproto.reads import SCHEMA_VERSION, ReadLayer
from ummanu.webproto.runs import ProductRun, RunStore
from ummanu.webproto.sprint_ops import SprintOperationLayer
from ummanu.webproto.sprint_reads import SprintReadLayer
from ummanu.webproto.sprint_requests import SprintRequestStore

__all__ = [
    "AGENT_STATES",
    "DEFAULT_LIMIT",
    "IMPLEMENTATION_FAILURES",
    "LIVENESS_INVARIANT",
    "MAX_LIMIT",
    "OPERATION_IDENTITY",
    "SCHEMA_VERSION",
    "CommandReadLayer",
    "Cursor",
    "InstallationUnavailable",
    "InvalidCursor",
    "OperationLayer",
    "OperationPending",
    "OwnerConflict",
    "PauseOperationLayer",
    "PauseReadLayer",
    "ProductRun",
    "ProtocolBoundary",
    "ReadError",
    "ReadLayer",
    "RunNotFound",
    "RunStore",
    "RuntimeUnavailable",
    "SprintOperationLayer",
    "SprintReadLayer",
    "SprintRequestStore",
    "TaskNotFound",
    "ValidationRefused",
]
