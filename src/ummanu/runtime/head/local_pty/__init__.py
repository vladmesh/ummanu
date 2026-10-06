"""Process substrate for the `local-pty` head backend (`runtime.local_pty_head` builds the verbs).

A head process the product owns: on its own pty in its own session, held by a supervisor that
outlives the dispatcher tick, addressable over a Unix socket, narrating into a versioned
append-only journal. Verb mapping: `deliver` = socket `input`, `observe` = bounded output plus
`status`, `attach` = bounded attach, `request_drain` = `drain`, `stop` = `stop`, `start` =
`client.spawn_head`.

Design invariants:

  * The supervisor holds the pty master, bounds kept output, refuses oversized input naming the
    limit, bounds concurrent attach, reaps the head and records `run.exited`. A detached
    `setsid` process could do none of that.
  * `spawn_head` starts an intermediate in a new session that forks the supervisor and exits, so
    the supervisor is never a dispatcher child; the head gets its own session with the pty as
    controlling terminal, out of reach of the dispatcher's process group.
  * A transient systemd scope (`MemoryMax`) is the resource boundary only; the supervisor keeps
    process ownership and the exit record. `memory_limit` needs SIGKILL plus a kernel OOM victim
    record naming this head. See docs/HEAD_SCOPES.md.
  * The pty is non-canonical, set before the head execs: canonical mode silently truncates a line
    at 4095 bytes, which would make the declared input limit a lie. Non-canonical mode answers a
    full buffer with `EAGAIN`, so any delivery up to the limit arrives whole.
  * A delivery is admitted, not awaited: `input` accepts or refuses within one loop tick, the loop
    writes as the terminal drains, and `input.accepted` counts only landed bytes. Progress is
    state (`status`, journal), so a slow head never makes the supervisor unanswerable.
  * Identity is the existing launch identity: the supervisor alone wraps the bare head command
    with `..command.with_pid_heartbeat`, so `head.pid` (at the caller's `pid_file`) is written by
    the head process and read unchanged by `ummanu.dispatch.watchdog`. `task` is spelled by
    `head.identity.task_binding`.
"""

from __future__ import annotations

from .client import (
    HeadHandle,
    LocalPtyError,
    LocalPtySpawnError,
    SupervisorClient,
    spawn_head,
)
from .journal import (
    DRAIN_REQUESTED,
    EVENT_KINDS,
    INPUT_ACCEPTED,
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_TAIL_BYTES,
    PROVIDER_PROGRESSED,
    RUN_EXITED,
    RUN_STARTED,
    RUN_STOPPING,
    TURN_FINISHED,
    TURN_STARTED,
    JournalError,
    JournalReadResult,
    JournalWriter,
    read_events,
    read_tail,
    tail_window,
)
from .protocol import (
    ATTACH_MAX_CLIENTS,
    CONNECTION_MAX_CLIENTS,
    DELIVERY_STATES,
    FRAME_MAX_BYTES,
    INPUT_DELIVERY_SECONDS,
    INPUT_MAX_BYTES,
    OUTPUT_BUFFER_BYTES,
    ProtocolError,
    socket_path_for,
)

__all__ = [
    "ATTACH_MAX_CLIENTS",
    "CONNECTION_MAX_CLIENTS",
    "DELIVERY_STATES",
    "DRAIN_REQUESTED",
    "EVENT_KINDS",
    "FRAME_MAX_BYTES",
    "INPUT_ACCEPTED",
    "INPUT_DELIVERY_SECONDS",
    "INPUT_MAX_BYTES",
    "JOURNAL_SCHEMA_VERSION",
    "JOURNAL_TAIL_BYTES",
    "OUTPUT_BUFFER_BYTES",
    "PROVIDER_PROGRESSED",
    "RUN_EXITED",
    "RUN_STARTED",
    "RUN_STOPPING",
    "TURN_FINISHED",
    "TURN_STARTED",
    "HeadHandle",
    "JournalError",
    "JournalReadResult",
    "JournalWriter",
    "LocalPtyError",
    "LocalPtySpawnError",
    "ProtocolError",
    "SupervisorClient",
    "read_events",
    "read_tail",
    "socket_path_for",
    "spawn_head",
    "tail_window",
]
