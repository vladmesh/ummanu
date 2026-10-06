"""Which agents are running, decided from process state and from nothing else.

This module is the reason criterion 3 of secretary-1561 exists. An operator dashboard that draws a
green dot next to a card because a terminal exists is worse than one that draws nothing: the pane
is a fact about a window, and windows outlive the processes drawn in them, get aliased, get
detached and get redrawn empty while the head behind them works
(`ummanu.dispatch.head_status`, issue:84c0ae4f796f994a7c1d).

So liveness here comes from one place: the launch-identity heartbeat the head's own shell writes
before it `exec`s, classified by :func:`head_run_process_status` against the durable ``HeadRun``
the dispatcher recorded for that role. Nothing in this file opens a terminal, lists a pane, asks a
session manager anything or imports a module that could -- which is checked by a test, not only
promised here.

Five values, and they are five because collapsing any two of them is what makes a dashboard lie:

``running``             a live process whose identity matches the recorded run
``finished``            the run's own lifecycle says its stop was confirmed, or its process ended
                        after a stop had been asked for. This is an ending, not a failure
``process_failed``      the run still expects a process behind it and the heartbeat says that
                        process is gone. Nobody asked it to stop; it is not there
``source_unavailable``  the heartbeat could not be read at all -- unreadable file, a process this
                        user may not inspect, a record written by a scheme this reader does not
                        know. Nothing is proven either way, and the reason says which
``unknown``             no evidence exists yet: the dispatcher holds an identity for this role but
                        no durable run, or the head has not published its heartbeat yet, or the
                        live process under that pid is somebody else's

PID reuse is not trusted anywhere in this: `head_process_status` compares the boot id and the
process start ticks recorded in the heartbeat, so a recycled pid reads as the dead head it belongs
to rather than as a live one.
"""

from __future__ import annotations

from typing import Any

from ummanu.dispatch.state import DispatcherRecord

# The control plane's own seam onto the heartbeat reader and its vocabulary: `dispatch.watchdog`
# re-exports every one of these names, so this layer reads process state through the same door the
# dispatcher does rather than opening a second one onto the runtime package.
from ummanu.dispatch.watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_IDENTITY_MISMATCH,
    HEARTBEAT_LIVE_MATCH,
    HEARTBEAT_NOT_YET_WRITTEN,
    HEARTBEAT_UNREADABLE,
    head_run_process_status,
    pid_file_path,
)

# The lifecycle vocabulary of the run itself, from the module that defines it. Spelling these two
# states as string literals here would be a second copy of a contract that already has one owner.
from ummanu.runtime.head import EXITED, FINISHING

RUNNING = "running"
FINISHED = "finished"
PROCESS_FAILED = "process_failed"
SOURCE_UNAVAILABLE = "source_unavailable"
UNKNOWN = "unknown"

#: Every value an agent's ``state`` may take, for a client that wants to enumerate them.
AGENT_STATES = (RUNNING, FINISHED, PROCESS_FAILED, SOURCE_UNAVAILABLE, UNKNOWN)

#: The two roles the dispatcher runs per card, and the record prefix each of them is kept under.
ROLES = (("worker", "worker"), ("review", "reviewer"))

#: Stated in every row. A client that renders a row must not have to know this module to read it.
LIVENESS_INVARIANT = (
    "liveness is the head's process state, read from its launch-identity heartbeat; a terminal, "
    "pane or window says nothing about whether an agent is running and is not consulted here"
)


def agent_rows(record: DispatcherRecord, ref: str) -> list[dict[str, Any]]:
    """One row per head identity the dispatcher holds for this card, worker first."""
    return [_row(record, ref, kind=kind, role=role) for kind, role in ROLES if record.owns_head(kind)]


def _row(record: DispatcherRecord, ref: str, *, kind: str, role: str) -> dict[str, Any]:
    run = record.review_head_run if kind == "review" else record.worker_head_run
    leaf = record.review_leaf if kind == "review" else record.worker_leaf
    pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
    pid_file = pid_file or pid_file_path(kind, ref)
    lifecycle = str(run.get("lifecycle") or "") if isinstance(run, dict) else ""
    run_id = str(run.get("run_id") or "") if isinstance(run, dict) else ""
    heartbeat = head_run_process_status(pid_file, run=run, role=role, task=ref, leaf=leaf)
    state, reason = _state(heartbeat, lifecycle=lifecycle, run_id=run_id)
    return {
        "ref": ref,
        "role": role,
        "state": state,
        "reason": reason,
        "head": (record.review_head if kind == "review" else record.head) or None,
        "run_id": run_id or None,
        "run_lifecycle": lifecycle or None,
        "attempt_id": record.attempt_id or None,
        "workspace": record.workspace or None,
        "card_state": record.state or None,
        "evidence": {
            "kind": "process_heartbeat",
            "heartbeat_state": str(heartbeat.get("state") or ""),
            "pid": heartbeat.get("pid") if isinstance(heartbeat.get("pid"), int) else None,
            "pid_file": pid_file,
            "detail": str(heartbeat.get("reason") or "") or None,
        },
        "invariant": LIVENESS_INVARIANT,
    }


def _state(heartbeat: dict[str, Any], *, lifecycle: str, run_id: str) -> tuple[str, str]:
    """Map one heartbeat classification onto the vocabulary, given what the run expected.

    The run's lifecycle is what tells an ending from a failure, and it is the only thing that can:
    an ended process looks identical from the outside whether somebody asked it to stop or it died.
    ``EXITED`` is a confirmed stop and ``FINISHING`` is a stop that was asked for, so a process
    missing under either of them is an agent that finished; a process missing under a run that
    still expects one is a process that failed.
    """
    state = str(heartbeat.get("state") or "")
    if lifecycle == EXITED:
        return FINISHED, "the dispatcher confirmed this head's stop"
    if state == HEARTBEAT_LIVE_MATCH:
        return RUNNING, "a live process matches this head's recorded run"
    if state == HEARTBEAT_DEAD:
        if lifecycle == FINISHING:
            return FINISHED, "this head's process ended after its stop was asked for"
        return (
            PROCESS_FAILED,
            "this head's run still expects a process and the heartbeat names none that is alive",
        )
    if state == HEARTBEAT_UNREADABLE:
        return (
            SOURCE_UNAVAILABLE,
            f"this head's heartbeat could not be read ({heartbeat.get('reason') or 'unreadable'})",
        )
    if state == HEARTBEAT_IDENTITY_MISMATCH:
        return UNKNOWN, (
            "the pid in this head's heartbeat belongs to another process, which proves nothing "
            "about this head"
        )
    if state == HEARTBEAT_NOT_YET_WRITTEN:
        if not run_id:
            return UNKNOWN, "the dispatcher holds a head identity for this role but no durable run"
        return UNKNOWN, "this head has not published a launch heartbeat yet"
    return UNKNOWN, "no process evidence was available for this head"
