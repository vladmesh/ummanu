"""What the dispatcher's heads in one live workspace are: alive, absent or unproven, and why.

An operator who opens a card's workspace and sees no head reads it as "the worker never started"
and intervenes by hand -- drops the claim, kills the workspace, restarts the card -- and destroys
live work. The measurement behind ``issue:84c0ae4f796f994a7c1d`` (2026-08-24, card
secretary-1450) was the counter-example: an Orca pane nothing drew, and the head behind it working.
So this command answers "is the head alive?" from the sources that observe the head, and says
which of them proved it and which could not answer.

Every head the dispatcher raises runs under a `local-pty` supervisor, and its row reads what that
backend keeps: the pid heartbeat, the supervisor's own answer, its lock and its journal. No pane
is read for any row (A20 step 5, secretary-1723). A legacy record -- a run on `orca-legacy`, or a
head identity with no durable run -- is shown as one (`legacy_record`, by the product's one
predicate `is_legacy_record`) and read through its pid heartbeat alone; it is never inventoried
through Orca.

``head_vitality``'s invariant -- pane and terminal readings are advisory and never by themselves
evidence of death -- is still printed on every row, so an operator reading one needs no module
knowledge to read it correctly.

Read-only, in the strong sense: it starts nothing, stops nothing, repairs nothing and writes
neither the dispatcher's state nor the head's, and a head whose channel cannot answer is reported
unproven rather than probed harder.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.dispatch.head_vitality import (
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    VitalitySnapshot,
    snapshots_from_status,
)
from ummanu.dispatch.head_vitality_episode import previous_snapshot_cursors, recovery_outlook
from ummanu.dispatch.host import _durable_head_run
from ummanu.dispatch.observer import load_observers, observer_head_status
from ummanu.dispatch.review import command_terminal_status
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tui import provider_progress_for_persisted_run
from ummanu.dispatch.types import HostError
from ummanu.dispatch.watchdog import head_run_process_status, pid_file_path
from ummanu.runtime.head import HeadRun, HeadRunError
from ummanu.runtime.head.identity import HEARTBEAT_DEAD, HEARTBEAT_LIVE_MATCH, head_process_status
from ummanu.runtime.head_runtime_backends import build_head_runtime, head_runtime_name, is_legacy_record
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from ummanu.runtime.local_pty_head import head_run_journal_read, head_run_supervisor_lease

# What this command may say about a head. Three words, deliberately: the two facts a snapshot can
# prove, and the honest third that keeps an unanswerable channel from being rounded to either.
HEAD_ALIVE = "alive"
HEAD_ABSENT = "absent"
HEAD_UNPROVEN = "unproven"

# Printed on every answer, next to every head. An operator reading a row must not have to know the
# module invariant to read the row correctly.
PANE_ADVISORY_INVARIANT = (
    "pane readings are advisory: a pane with no runtime pane, a disconnected pane, a pane no "
    "inventory names and an unreadable pane channel are all facts about the window, and none of "
    "them is evidence that a head is absent"
)

# A source the observation carried no reading from at all. Distinct from `unavailable`, which is a
# channel that was asked and could not answer, because reporting the two as one word would make an
# unsampled source look like a broken one.
NOT_OBSERVED = "not_observed"

#: What a legacy row says about itself, in place of anything a pane could have said.
LEGACY_NOTICE = (
    "It is a legacy record: shown, never launched or delivered to, and no pane inventory is read for it."
)

_ROLES = (("worker", "worker"), ("review", "reviewer"))


@dataclass
class HeadStatusHost:
    """Everything `command_terminal_status` asks a host for, answered without a single write.

    The provider cursor comes from the run already persisted on the record: the dispatcher's own
    reader may bind a Claude source and commit that binding back onto the record, which is a write,
    and an observer reports "this channel did not answer" instead of performing one.
    """

    mode: str = "real"

    def provider_progress(self, _task: dict[str, Any], record: DispatcherRecord, kind: str) -> dict[str, str]:
        run = record.review_head_run if kind == "review" else record.worker_head_run
        return provider_progress_for_persisted_run(run)


def head_status(runtime: Any, *, workspace: str, now: float | None = None) -> dict[str, Any]:
    """Answer, for every head the dispatcher holds in ``workspace``, whether it is alive and why.

    One row per head identity the dispatcher actually carries for that workspace, so a workspace
    the dispatcher holds nothing in answers with no rows rather than with a guess. Nothing here
    consults a wait ceiling, a threshold or a recovery ladder: the row states what the sources
    said, which of them could not answer, and which one proved what.
    """
    observed_at = time.time() if now is None else float(now)
    target = _normalised(workspace)
    if not target:
        return {
            "status": "degraded",
            "step": "head-status",
            "reason": "a workspace path is required",
            "workspace": "",
            "heads": [],
            "invariant": PANE_ADVISORY_INVARIANT,
        }
    mode = str(getattr(runtime.host, "mode", "real") or "real")
    if mode == "noop":
        # A noop host answers "live" to every question by construction. Reporting that as a head
        # would be exactly the fabricated observation the vitality vocabulary refuses to make.
        return {
            "status": "degraded",
            "step": "head-status",
            "workspace": target,
            "heads": [],
            "reason": "this dispatcher host is in noop mode and observes no live workspace",
            "invariant": PANE_ADVISORY_INVARIANT,
        }
    payload = runtime.production_state.load()
    records = runtime.production_state.records(payload)
    # Each row's backend is the one its durable run names, never the current profile's: a head
    # raised on one backend is read through that backend, whatever the registry says now.
    held = [
        (ref, record, kind, role, _durable_head_run(_recorded_run(record, kind)))
        for ref, record in sorted(records.items())
        if _normalised(record.workspace) == target
        for kind, role in _ROLES
        if record.owns_head(kind)
    ]
    observers = _observer_runs(payload, target)
    host = HeadStatusHost()
    heads = [
        _supervised_card_row(runtime, ref, record, run, kind=kind, role=role, observed_at=observed_at)
        if run is not None and head_runtime_name(run) == LOCAL_PTY_RUNTIME
        else _legacy_row(host, ref, record, run, kind=kind, role=role, observed_at=observed_at)
        for ref, record, kind, role, run in held
    ]
    heads.extend(_supervised_observer_rows(runtime, observers, observed_at))
    return {
        "status": "ok",
        "step": "head-status",
        "workspace": target,
        "heads": heads,
        "invariant": PANE_ADVISORY_INVARIANT,
        "summary": (
            [head["summary"] for head in heads]
            if heads
            else ["the dispatcher holds no head in this workspace"]
        ),
    }


def _recorded_run(record: DispatcherRecord, kind: str) -> dict[str, Any]:
    return record.review_head_run if kind == "review" else record.worker_head_run


def _legacy_row(
    host: Any,
    ref: str,
    record: DispatcherRecord,
    run: HeadRun | None,
    *,
    kind: str,
    role: str,
    observed_at: float,
) -> dict[str, Any]:
    """One legacy head: what its pid heartbeat proved, with no pane read for it."""
    run_payload = record.review_head_run if kind == "review" else record.worker_head_run
    run_id = str((run_payload or {}).get("run_id") or "")
    row: dict[str, Any] = {
        "ref": ref,
        "role": role,
        # A head that lived in an Orca pane, or one with no durable run. A legacy record is shown,
        # never launched or delivered to; `legacy_record` says which rows are one by the product's
        # one predicate (a run with no runtime, or `orca-legacy`).
        "runtime": ORCA_LEGACY_RUNTIME,
        "legacy_record": is_legacy_record(run),
        "run_id": run_id or None,
        "card_state": record.state,
        "invariant": PANE_ADVISORY_INVARIANT,
    }
    if not run_id:
        # Criterion of the whole vocabulary: a snapshot is bound to a run. With no durable HeadRun
        # there is nothing to bind one to, and borrowing another run's evidence is the exact lie
        # the binding exists to prevent -- so the row says so and proves nothing.
        row.update(
            head=HEAD_UNPROVEN,
            process=ProcessState.UNKNOWN.value,
            proved_by=None,
            evidence=[_not_observed(source) for source in _REPORTED_SOURCES],
            unavailable_sources=[],
            episode=None,
            reason=(
                "the dispatcher holds a head identity for this role but no durable HeadRun, so no "
                "vitality snapshot may be bound to it"
            ),
        )
        row["summary"] = _summary(row)
        return row
    status, refusal = _terminal_status(host, ref, record, kind)
    episode = record.review_vitality_episode if kind == "review" else record.worker_vitality_episode
    bound_episode = episode if episode is not None and episode.run_id == run_id else None
    snapshots = snapshots_from_status(
        status,
        run_id=run_id,
        **previous_snapshot_cursors(episode, run_id),
        observed_at=observed_at,
    )
    by_source = {snapshot.source: snapshot for snapshot in snapshots}
    head, process, proved_by = _verdict(by_source)
    row.update(
        head=head,
        process=process.value,
        proved_by=proved_by.value if proved_by is not None else None,
        evidence=[_evidence(source, by_source.get(source)) for source in _REPORTED_SOURCES],
        unavailable_sources=sorted(
            source.value
            for source, snapshot in by_source.items()
            if snapshot.availability is SourceAvailability.UNAVAILABLE
        ),
        episode=_episode_row(bound_episode, record, kind=kind, observed_at=observed_at),
        reason=refusal,
    )
    if episode is not None and bound_episode is None:
        # Someone else's conclusion about someone else's run. Naming the refusal is what keeps a
        # stale episode from being read as this head's silence.
        row["episode_note"] = "the persisted vitality episode names another run and was not used"
    row["summary"] = _summary(row)
    return row


def _episode_row(
    episode: Any,
    record: DispatcherRecord,
    *,
    kind: str,
    observed_at: float,
) -> dict[str, Any] | None:
    """The persisted conclusion, plus what an operator needs when a head goes quiet.

    A head frozen behind a dark progress source used to be readable only as
    ``healthy_quiet`` plus a basis token, which is precisely the state
    ``issue:7bff833fef6d9d9b404d`` sat in for 65 minutes. So the row answers the four questions
    that state raises: which progress source is missing or dark and since when, how long the head
    has been quiet, what the last meaningful progress was (the episode's own advancement, and the
    card's pane-output stamp beside it), and when the next recovery rung falls due.

    Derived, never re-observed: every number comes from the persisted episode and the record this
    command already read, through the reducer's own arithmetic (``recovery_outlook``).
    """
    if episode is None:
        return None
    outlook = recovery_outlook(episode, observed_at)
    return {
        "run_id": episode.run_id,
        "verdict": episode.verdict.value,
        "basis": list(episode.basis),
        "updated_at": episode.updated_at,
        "reason": episode.reason,
        "quiet_seconds": outlook["quiet_seconds"],
        # Both halves of "which source is missing": one that answered and stopped is dark with a
        # stamp, one this observation never carried is simply absent from the map.
        "dark_progress_sources": outlook["dark_sources"],
        "missing_progress_sources": [
            source.value
            for source in _REPORTED_SOURCES
            if source is SnapshotSource.PROVIDER_CURSOR
            and source.value not in episode.evidence_cursors
            and source.value not in episode.unavailable_since
        ],
        "last_progress": {
            "at": episode.last_progress_at,
            "source": episode.last_progress_source or "",
            # The workspace half: the card's own pane-output stamp, which is not vitality
            # evidence and is reported beside the episode rather than folded into it.
            "pane_output_at": float(getattr(record, f"{kind}_progress_at", 0.0) or 0.0),
            "waiting_since": float(getattr(record, f"{kind}_waiting_since", 0.0) or 0.0),
        },
        "next_recovery_deadline": outlook["next_deadline"],
        "deadline_note": outlook["note"],
        "answer_owed_since": (float(record.worker_answer_owed_since or 0.0) if kind == "worker" else 0.0),
        "turn_ended_at": episode.turn_ended_at,
        # The child command a respawn would name (secretary-1692), and how long the head's
        # children have been holding it healthy while the head itself was silent.
        "child_activity": {
            "command": episode.last_child_command,
            "output": episode.last_child_output,
            "read_at": episode.last_child_at,
            "progress_at": episode.child_progress_at,
            "holding_since": episode.child_activity_since,
        },
    }


_REPORTED_SOURCES = (
    SnapshotSource.PID_HEARTBEAT,
    SnapshotSource.PROVIDER_CURSOR,
)


def _terminal_status(host: Any, ref: str, record: DispatcherRecord, kind: str) -> tuple[dict[str, Any], str]:
    """The observation the wait tick makes, falling back to the pid heartbeat alone.

    `command_terminal_status` refuses a record with no workspace, and an operator looking at such
    a record is exactly the person who must not be told the head is gone. So the refusal is
    recorded as a fact about that channel and the same pid probe is made directly.
    """
    try:
        return command_terminal_status(host, {"ref": ref}, record, kind=kind), ""
    except HostError as exc:
        run = record.review_head_run if kind == "review" else record.worker_head_run
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        pid_status = head_run_process_status(
            pid_file_path(kind, ref),
            run=run,
            role=kind,
            task=f"card:{ref}",
            leaf=leaf,
        )
        return {"pid_status": dict(pid_status)}, (
            f"the status channel could not be read ({str(exc)[:200]}), so this head was observed "
            "through its pid heartbeat alone"
        )


def _verdict(
    by_source: dict[SnapshotSource, VitalitySnapshot],
) -> tuple[str, ProcessState, SnapshotSource | None]:
    """Alive, absent or unproven -- from the snapshots alone, and never from the pane.

    The pid heartbeat is the only source that may say a head is gone, because it is the only one
    that observes the process. A bound provider cursor that moved proves the opposite direction
    only: work advanced, so something is running it. Everything else leaves the answer unproven,
    which is a statement about this observation and not about the head.
    """
    pid = by_source.get(SnapshotSource.PID_HEARTBEAT)
    if pid is not None and pid.availability is SourceAvailability.AVAILABLE:
        if pid.process in (ProcessState.RUNNING, ProcessState.SUSPENDED):
            return HEAD_ALIVE, pid.process, SnapshotSource.PID_HEARTBEAT
        if pid.process is ProcessState.DEAD:
            return HEAD_ABSENT, ProcessState.DEAD, SnapshotSource.PID_HEARTBEAT
    provider = by_source.get(SnapshotSource.PROVIDER_CURSOR)
    if (
        provider is not None
        and provider.availability is SourceAvailability.AVAILABLE
        and provider.progress is ProgressState.ADVANCING
    ):
        return HEAD_ALIVE, ProcessState.UNKNOWN, SnapshotSource.PROVIDER_CURSOR
    return HEAD_UNPROVEN, ProcessState.UNKNOWN, None


def _evidence(source: SnapshotSource, snapshot: VitalitySnapshot | None) -> dict[str, Any]:
    """What one channel said, in the snapshot's own words."""
    if snapshot is None:
        return _not_observed(source)
    return {
        "source": source.value,
        "availability": snapshot.availability.value,
        "answered": snapshot.availability is SourceAvailability.AVAILABLE,
        "advisory": snapshot.advisory,
        "process": snapshot.process.value,
        "turn": snapshot.turn.value,
        "progress": snapshot.progress.value,
        "reason": snapshot.reason,
    }


def _not_observed(source: SnapshotSource) -> dict[str, Any]:
    return {
        "source": source.value,
        "availability": NOT_OBSERVED,
        "answered": False,
        "advisory": source is SnapshotSource.PANE_ADVISORY,
        "process": ProcessState.UNKNOWN.value,
        "turn": "unknown",
        "progress": ProgressState.UNKNOWN.value,
        "reason": "this observation carried no reading from this channel",
    }


def _summary(row: dict[str, Any]) -> str:
    """One sentence an operator can act on without interpreting anything.

    The verdict first, then the quiet half when there is one, then what the row is: a legacy
    record, about which nothing is read from a pane.
    """
    head = row["head"]
    run = row["run_id"] or "no run id"
    who = f"{row['role']} head of {row['ref']} ({ORCA_LEGACY_RUNTIME} run {run})"
    if head == HEAD_ALIVE:
        proof = (
            f"{row['proved_by']} says its process is {row['process']}"
            if row["proved_by"] == SnapshotSource.PID_HEARTBEAT.value
            else f"{row['proved_by']} says its work advanced"
        )
        verdict = f"{who} is ALIVE: {proof}."
    elif head == HEAD_ABSENT:
        verdict = f"{who} is ABSENT: {row['proved_by']} says the process behind its launch identity is gone."
    else:
        dark = ", ".join(row["unavailable_sources"]) or "none of the sources answered"
        verdict = (
            f"{who} is UNPROVEN: no source proved it either way (unavailable: {dark}). "
            "This is a statement about the observation, not about the head."
        )
    return f"{verdict} {_episode_sentence(row)}{LEGACY_NOTICE}"


def _episode_sentence(row: dict[str, Any]) -> str:
    """The quiet half, said only when there is something an operator has to act on.

    A live head whose progress channel is dark reads as ``alive`` on the head axis, and until
    secretary-1543 that is all the summary said -- the
    one line an operator reads named neither the missing channel nor the deadline. It does now,
    and only then: a head with no dark source and no pending rung adds no words.
    """
    episode = row.get("episode") or {}
    dark = episode.get("dark_progress_sources") or []
    deadline = episode.get("next_recovery_deadline") or {}
    if not dark and not deadline:
        return ""
    parts = []
    if dark:
        parts.append(
            "; ".join(f"{entry['source']} has been dark for {int(entry['dark_seconds'])}s" for entry in dark)
        )
    if deadline:
        parts.append(
            f"it becomes {deadline['verdict']} in {int(deadline['in_seconds'])}s unless something advances"
        )
    elif episode.get("deadline_note"):
        parts.append(str(episode["deadline_note"]))
    return f"Vitality {episode.get('verdict', 'unknown')}: " + ", and ".join(parts) + ". "


def _normalised(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path or ""))) if path else ""


#: How many of a supervised head's last journal records a row carries.
JOURNAL_TAIL_RECORDS = 8

# The sources a supervised row reads, beside the pid heartbeat every row shares.
SUPERVISOR_SOURCE = "supervisor"
LEASE_SOURCE = "supervisor_lock"
JOURNAL_SOURCE = "journal"

JOURNAL_DEGRADED = "degraded"

LEASE_HELD = "held"
LEASE_FREE = "free"


def _observer_runs(payload: dict[str, Any], workspace: str) -> list[tuple[str, Any, HeadRun | None]]:
    """Every sprint observer recorded in this workspace, with the run it names or `None`."""
    observers = []
    for ref, record in sorted(load_observers(payload).items()):
        if _normalised(record.workspace) != workspace:
            continue
        try:
            run: HeadRun | None = HeadRun.from_json(record.head_run)
        except (HeadRunError, TypeError, ValueError):
            run = None
        observers.append((ref, record, run))
    return observers


def _supervised_observer_rows(
    runtime: Any, observers: list[tuple[str, Any, HeadRun | None]], observed_at: float
) -> list[dict[str, Any]]:
    """A row for each sprint observer in this workspace that a local-pty supervisor holds."""
    rows = []
    for ref, record, run in observers:
        if run is None or head_runtime_name(run) != LOCAL_PTY_RUNTIME:
            continue
        row = _supervised_row(
            runtime,
            run,
            lambda record=record: observer_head_status(record),
            ref=ref,
            role="observer",
            observed_at=observed_at,
        )
        row.update(kind="observer", profile=record.head, observer_state=record.state)
        rows.append(row)
    return rows


def _supervised_card_row(
    runtime: Any,
    ref: str,
    record: DispatcherRecord,
    run: HeadRun,
    *,
    kind: str,
    role: str,
    observed_at: float,
) -> dict[str, Any]:
    """A card's worker or reviewer that a local-pty supervisor holds, read as the observer is."""
    review = kind == "review"
    pid_file = (record.review_pid_file if review else record.worker_pid_file) or run.pid_file

    def process() -> dict[str, Any]:
        return head_run_process_status(
            pid_file or pid_file_path(kind, ref),
            run=record.review_head_run if review else record.worker_head_run,
            role=kind,
            task=f"card:{ref}",
            leaf=record.review_leaf if review else record.worker_leaf,
        )

    row = _supervised_row(runtime, run, process, ref=ref, role=role, observed_at=observed_at)
    row.update(kind=kind, profile=record.review_head if review else record.head, card_state=record.state)
    return row


def _supervised_row(
    runtime: Any,
    run: HeadRun,
    process: Callable[[], dict[str, Any]],
    *,
    ref: str,
    role: str,
    observed_at: float,
) -> dict[str, Any]:
    """One head a local-pty supervisor holds, read from what that backend keeps instead of a pane.

    Four sources, each reported as answering or not: the launch identity (`process` reads the pid
    heartbeat the caller classifies for this role), the supervisor's own `status` answer,
    the supervisor lock and who holds it, and the last records of the head's journal. Read-only
    like the rest of this module: `observe` asks the supervisor one question and everything else
    is read from disk; nothing is delivered, drained or stopped. As with a pane, only the pid
    heartbeat may say the head is gone -- a supervisor that did not answer, a lock nobody holds and
    an unreadable journal are facts about those channels.

    Every source is read through `_source`, so nothing any of them holds can make head-status fail:
    a read or an interpretation that raises is that source not answering. What the verdict and the
    summary read of a source is only `answered`, `state` and `reason`, the keys every shape carries;
    anything else they look at is read with a default.
    """
    root = Path(runtime.data_dir) / "heads"
    run_dir = root / run.run_id
    heartbeat = _source(SnapshotSource.PID_HEARTBEAT.value, lambda: _heartbeat(process()), pid=None)
    supervisor = _source(SUPERVISOR_SOURCE, lambda: _supervisor_answer(root, run), status="")
    lease = _source(LEASE_SOURCE, lambda: _supervisor_lease(run_dir), holder_pid=None)
    journal = _source(JOURNAL_SOURCE, lambda: _journal_tail(run_dir), tail=[])
    head, proved_by = _supervised_verdict(heartbeat, supervisor)
    row: dict[str, Any] = {
        "ref": ref,
        "role": role,
        "runtime": LOCAL_PTY_RUNTIME,
        "legacy_record": False,
        "run_id": run.run_id,
        "head": head,
        "proved_by": proved_by,
        "process": {"state": heartbeat["state"], "pid": heartbeat.get("pid")},
        "heartbeat": heartbeat,
        "supervisor": supervisor,
        "lease": lease,
        "journal": journal,
        "unavailable_sources": [
            name
            for name, source in (
                (SnapshotSource.PID_HEARTBEAT.value, heartbeat),
                (SUPERVISOR_SOURCE, supervisor),
                (LEASE_SOURCE, lease),
                (JOURNAL_SOURCE, journal),
            )
            if not source["answered"]
        ],
        "invariant": PANE_ADVISORY_INVARIANT,
    }
    row["summary"] = _supervised_summary(row, observed_at)
    return row


def _source(name: str, read: Callable[[], dict[str, Any]], **empty: Any) -> dict[str, Any]:
    """One supervised source's answer, or the uniform shape of a source that did not answer.

    The one place a supervised row guards what it reads. Heartbeat files, the supervisor's reply,
    the lock files and the journal are all outside this process's control, so any exception their
    read or interpretation raises is this source not answering -- never a failed command and never
    a verdict. `empty` is the source's own empty evidence (a journal's `tail`, say), so a consumer
    of the payload finds the same keys either way.
    """
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - every supervised source is untrusted input
        return {
            **empty,
            "answered": False,
            "state": SourceAvailability.UNAVAILABLE.value,
            "reason": f"{name} could not be read or interpreted ({type(exc).__name__}: {str(exc)[:200]})",
        }


def _heartbeat(process: dict[str, Any]) -> dict[str, Any]:
    """The pid heartbeat as a supervised source, from the classification of this role's pid file."""
    if not isinstance(process, dict):
        raise TypeError(f"the heartbeat classification is a {type(process).__name__}, not a mapping")
    known, state, pid, reason = (process.get(key) for key in ("known", "state", "pid", "reason"))
    for key, value, types in (
        ("known", known, (bool, type(None))),
        ("state", state, (str, type(None))),
        ("reason", reason, (str, type(None))),
    ):
        if not isinstance(value, types):
            raise TypeError(f"the heartbeat's {key} is a {type(value).__name__}")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
        raise TypeError(f"the heartbeat's pid is a {type(pid).__name__}")
    return {
        "source": SnapshotSource.PID_HEARTBEAT.value,
        "answered": bool(known),
        "state": state or "unknown",
        "pid": pid,
        "reason": reason or "",
    }


def _supervisor_answer(root: Path, run: HeadRun) -> dict[str, Any]:
    """The supervisor's own `status` answer, or why it gave none."""
    try:
        seen = build_head_runtime(
            LOCAL_PTY_RUNTIME,
            local_pty_root=lambda: root,
            head_process_status=head_process_status,
        ).observe(run)
    except (OSError, ValueError, HostError) as exc:
        return {
            "answered": False,
            "state": SourceAvailability.UNAVAILABLE.value,
            "status": "",
            "reason": f"the supervisor could not be observed: {str(exc)[:200]}",
        }
    status = seen.evidence if isinstance(seen.evidence, dict) and "alive" in seen.evidence else {}
    answer: dict[str, Any] = {
        "answered": bool(status),
        "state": (SourceAvailability.AVAILABLE if status else SourceAvailability.UNAVAILABLE).value,
        "status": seen.status,
        "reason": seen.reason,
        **{
            key: status[key]
            for key in (
                "supervisor_pid",
                "head_pid",
                "alive",
                "turn_open",
                "turn",
                "output_bytes",
                "journal_seq",
                "draining",
                "stopping",
            )
            if key in status
        },
    }
    if not status and seen.evidence:
        answer["detail"] = str(seen.evidence)[:240]
    return answer


def _supervisor_lease(run_dir: Path) -> dict[str, Any]:
    """Who holds this run's supervisor lock, as the local-pty backend reads it without taking it."""
    lease = head_run_supervisor_lease(run_dir)
    if not lease.lock_readable:
        return {
            "answered": False,
            "state": SourceAvailability.UNAVAILABLE.value,
            "holder_pid": None,
            "reason": f"the supervisor lock could not be read ({lease.error})",
        }
    files: dict[str, Any] = {"written_pid": lease.written_pid, "supervisor_pid": lease.supervisor_pid}
    if lease.content_error:
        return {
            **files,
            "answered": False,
            "state": SourceAvailability.UNAVAILABLE.value,
            "holder_pid": None,
            "reason": f"the supervisor lock files hold unreadable content ({lease.content_error})",
        }
    if not lease.table_readable:
        return {
            **files,
            "answered": False,
            "state": SourceAvailability.UNAVAILABLE.value,
            "holder_pid": None,
            "reason": f"the kernel lock table could not be read ({lease.error})",
        }
    if not lease.holders:
        return {
            **files,
            "answered": True,
            "state": LEASE_FREE,
            "holder_pid": None,
            "reason": "no process holds the supervisor lock, so no supervisor owns this run",
        }
    return {**files, "answered": True, "state": LEASE_HELD, "holder_pid": lease.holders[0], "reason": ""}


def _journal_tail(run_dir: Path) -> dict[str, Any]:
    """The last records of the head's journal, or why it could not be read.

    Everything the rest of the module reads from the answer is normalised here -- each tail
    record's `at` is a finite positive float or absent -- so no consumer parses a raw journal field.
    A journal that cannot be read is an unavailable source; one read only in part is degraded.
    Neither says anything about the head. Content that cannot be interpreted at all raises, and
    `_source` reports that as the journal not answering.
    """
    try:
        read = head_run_journal_read(run_dir)
    except OSError as exc:
        return {
            "answered": False,
            "state": SourceAvailability.UNAVAILABLE.value,
            "reason": f"the journal could not be read ({str(exc)[:200]})",
            "tail": [],
        }
    kept = ("seq", "kind", "turn", "reason", "bytes", "subject")
    tail = []
    untimed = 0
    for event in read.events[-JOURNAL_TAIL_RECORDS:]:
        record = {key: event[key] for key in kept if key in event}
        at = _journal_time(event.get("at"))
        if at is None:
            untimed += 1
        else:
            record["at"] = at
        tail.append(record)
    damage = []
    if read.malformed:
        damage.append(f"{read.malformed} malformed line(s) skipped")
    if read.truncated_tail:
        damage.append("the final line is torn")
    if not read.ordered:
        damage.append("records are out of sequence")
    if untimed:
        damage.append(f"{untimed} tail record(s) carry no usable time")
    if not damage:
        return {"answered": True, "state": SourceAvailability.AVAILABLE.value, "reason": "", "tail": tail}
    # The journal answered, but not in full: its tail is what could be read, never a clean record.
    return {
        "answered": True,
        "state": JOURNAL_DEGRADED,
        "reason": "the journal tail is incomplete: " + ", ".join(damage),
        "malformed": read.malformed,
        "truncated_tail": read.truncated_tail,
        "tail": tail,
    }


def _journal_time(value: Any) -> float | None:
    """A journal record's `at` as seconds, or `None` for any value that is not a usable time.

    Total: a bool, a string, a container, an int too large for a float, an infinity or a NaN, or
    anything else that fails to convert gives `None` rather than raising.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except Exception:  # noqa: BLE001 - an int too large for a float, or anything else, is no time
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _supervised_verdict(heartbeat: dict[str, Any], supervisor: dict[str, Any]) -> tuple[str, str | None]:
    """Alive, absent or unproven, with only the pid heartbeat allowed to say absent."""
    if heartbeat["state"] == HEARTBEAT_LIVE_MATCH:
        return HEAD_ALIVE, SnapshotSource.PID_HEARTBEAT.value
    if heartbeat["state"] == HEARTBEAT_DEAD:
        return HEAD_ABSENT, SnapshotSource.PID_HEARTBEAT.value
    if supervisor["answered"] and supervisor.get("alive"):
        return HEAD_ALIVE, SUPERVISOR_SOURCE
    return HEAD_UNPROVEN, None


def _supervised_summary(row: dict[str, Any], observed_at: float) -> str:
    """One line: the verdict and its proof, then what each supervised source said."""
    who = f"{row['role']} head of {row['ref']} (local-pty run {row['run_id']})"
    if row["head"] == HEAD_UNPROVEN:
        verdict = f"{who} is UNPROVEN: no source proved it either way"
    else:
        verdict = f"{who} is {row['head'].upper()} by {row['proved_by']}"
    heartbeat = row["heartbeat"]
    parts = [f"process {heartbeat['state']} pid {heartbeat.get('pid') or '-'}"]
    supervisor = row["supervisor"]
    if supervisor["answered"]:
        flags = [
            "head alive" if supervisor.get("alive") else "head exited",
            f"turn {'open' if supervisor.get('turn_open') else 'closed'}",
        ]
        flags.extend(flag for flag in ("draining", "stopping") if supervisor.get(flag))
        parts.append("supervisor " + ", ".join(flags))
    lease = row["lease"]
    if lease["answered"]:
        held = lease["state"] == LEASE_HELD
        parts.append(f"lock held by pid {lease.get('holder_pid')}" if held else "lock held by nobody")
    journal = row["journal"]
    if journal["answered"]:
        # `at` is `_journal_tail`'s normalised float or absent, never a raw journal field.
        records = journal.get("tail") or []
        last = records[-1] if records else {}
        if "at" in last:
            age = f"{max(0.0, observed_at - last['at']):.0f}s ago"
        else:
            age = "at no readable time" if records else "never"
        parts.append(f"last journal record {last.get('kind') or '(none)'} {age}")
        if journal["state"] == JOURNAL_DEGRADED:
            parts.append(f"journal degraded ({journal['reason'].split(': ', 1)[-1]})")
    silent = row["unavailable_sources"]
    tail = (
        f"; did not answer: {', '.join(silent)}, which is a fact about those channels, not about the head"
        if silent
        else ""
    )
    return f"{verdict}: {'; '.join(parts)}{tail}."
