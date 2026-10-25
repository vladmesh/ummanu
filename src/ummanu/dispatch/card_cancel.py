"""`task cancel` and `task supersede`: end any non-Done card in one bounded operation.

Under one allowance the operation takes the dispatcher tick lock and the card's effect lock, stops
the card's heads through the reconcile stop, archives the card with a `cancelled` audit event,
requests workspace disposal and drops the dispatcher record. A wait card that still waits records
its cancel instead, and the dispatcher delivers `cancelled` to its return addresses. A supersede is
that cancel with a successor: the archive transaction also moves every open dependent's
`blocked_by` to the successor and records the supersession, under a `superseded` audit event.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ummanu.board import wait_card
from ummanu.board.completion_evidence import is_wait
from ummanu.board.legacy_codec import positive_int, text
from ummanu.board.models import CardState
from ummanu.board.roles import Role
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.terminal_taxonomy import normalize_terminal_taxonomy
from ummanu.board.task_protocol.rows import (
    _TYPED_RECORD_TYPES,
    WAIT_CANCELLED,
    _blocker_refs,
    _check_execution_record,
    _CommittedWriteError,
    _digest,
    _normalize_comment,
    _now,
    _task_is_active,
    _task_number,
    project_card_by_reference,
)
from ummanu.dispatch.launch import REVIEW_ROLE, WORKER_ROLE, launch_intent
from ummanu.dispatch.ownership import _ALLOWANCE, Allowance, Deferred, _path_lock, reference_lock
from ummanu.dispatch.production import ProductionState, _stop_record_heads
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError
from ummanu.kernel.errors import TaskError
from ummanu.runtime.local_pty_inspection import _flock_holders

#: The audit kind of a card cancel.
CANCELLED = "cancelled"
#: The audit kind of a supersede: a cancel naming the successor.
SUPERSEDED = "superseded"
#: The whole operation's bound: every lock wait, board statement and journal write it admits.
CANCEL_ALLOWANCE = 20.0
_WAITING = {CardState.READY.value, CardState.IN_PROGRESS.value}


@dataclass
class _Progress:
    """How far one cancel got, for a refusal to say truthfully what it left behind."""

    asked: list[str] = field(default_factory=list)
    archived: str = ""


def cancel_card(
    writer: Any,
    *,
    role: str,
    actor: str,
    reference: str,
    reason: str,
    request_id: str | None = None,
    runtime: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Cancel a non-Done card as the PO, or as the observer of the card's own sprint.

    `runtime` builds the dispatcher runtime whose host stops the card's heads; it is called only
    when the card's dispatcher record still names a head, a launch or an unsettled workspace.
    """
    admitted, reason, request_id, current = _request(writer, role, actor, reference, reason, request_id)
    prior = writer.audit.committed_event(request_id) or writer.audit.pending_event(request_id)
    if (prior or {}).get("kind") == WAIT_CANCELLED or (prior is None and _waiting(current)):
        return _cancel_wait(writer, admitted, actor, reference, reason, request_id)
    return _bounded(
        writer, admitted, actor, reference, reason, request_id, runtime, replay=prior is not None, successor=""
    )


def supersede_card(
    writer: Any,
    *,
    role: str,
    actor: str,
    reference: str,
    successor: str,
    reason: str,
    request_id: str | None = None,
    runtime: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Cancel `reference` in favour of `successor`, which takes its place in every open dependent's
    `blocked_by` and records that it supersedes it, all in the cancel's archive transaction.

    The PO supersedes any card; the observer only when both cards are of its own sprint.
    """
    admitted, reason, request_id, current = _request(writer, role, actor, reference, reason, request_id)
    successor = successor.strip()
    prior = writer.audit.committed_event(request_id) or writer.audit.pending_event(request_id)
    if prior is None:
        new = _successor(writer, reference, successor)
        if admitted is Role.OBSERVER and new.get("sprint") != current["sprint"]:
            raise TaskError("role_forbidden", "the observer supersedes only by a card of its own sprint", 3)
        if _waiting(current):
            raise TaskError(
                "transition_forbidden",
                f"{reference} is a wait card that still waits; `task cancel` ends it and delivers "
                "`cancelled` to its return addresses",
                3,
            )
    result = _bounded(
        writer, admitted, actor, reference, reason, request_id, runtime,
        replay=prior is not None, successor=successor,
    )
    event = writer.audit.committed_event(request_id)
    return {**result, "successor": successor, "repointed": sorted(event["payload"]["repointed"])}


def _request(
    writer: Any, role: str, actor: str, reference: str, reason: str, request_id: str | None
) -> tuple[Role, str, str, dict[str, Any]]:
    """The admitted role, the board-safe reason, the request id and the card, for either verb."""
    admitted: Role = writer._role(role, {Role.PO, Role.OBSERVER}, actor=actor)
    reason = writer._redact_for_board(reason).strip()
    if not reason:
        raise TaskError("validation", "a cancel needs a non-empty reason (--reason-file)", 2)
    request_id = request_id or str(uuid.uuid4())
    current = writer.reader.show(reference)
    if admitted is Role.OBSERVER:
        if not str(current.get("sprint") or ""):
            raise TaskError("role_forbidden", "the observer cancels only a card of its own sprint", 3)
        writer._guard_observer_identity(
            role=admitted.value,
            actor=actor,
            project=str(current.get("project") or ""),
            card_sprint=str(current.get("sprint") or ""),
            request_id=request_id,
            reference=reference,
        )
    return admitted, reason, request_id, current


def _waiting(card: dict[str, Any]) -> bool:
    return is_wait(card) and card["state"] in _WAITING and not card.get("closed")


def _bounded(
    writer: Any,
    role: Role,
    actor: str,
    reference: str,
    reason: str,
    request_id: str,
    runtime: Callable[[], Any] | None,
    *,
    replay: bool,
    successor: str,
) -> dict[str, Any]:
    """The archive path of both verbs under one allowance, with the tick lock and the card's lock held."""
    state = ProductionState(Path(writer.data_dir))
    allowance = Allowance(CANCEL_ALLOWANCE)
    token = _ALLOWANCE.set(allowance)
    progress = _Progress()
    try:
        with contextlib.ExitStack() as held:
            if isinstance(writer.client, SqlCardClient):
                held.enter_context(writer.client.within(allowance.remaining, allowance.defer))
            held.enter_context(_path_lock(state.tick_lock))
            held.enter_context(reference_lock(writer.data_dir, reference))
            return _cancel_held(
                writer, state, role, actor, reference, reason, request_id, runtime,
                replay=replay, allowance=allowance, progress=progress, successor=successor,
            )
    except Deferred:
        raise _busy(reference, allowance, progress) from None
    finally:
        _ALLOWANCE.reset(token)


def _cancel_held(
    writer: Any,
    state: ProductionState,
    role: Role,
    actor: str,
    reference: str,
    reason: str,
    request_id: str,
    runtime: Callable[[], Any] | None,
    *,
    replay: bool,
    allowance: Allowance,
    progress: _Progress,
    successor: str,
) -> dict[str, Any]:
    """The cancel with both locks held: heads, then the archive, then the dispatcher record.

    A supersede's successor is checked before any head is stopped and again inside the archive
    transaction, whose repoint is the one that transaction computes.
    """
    payload = state.load()
    if payload.get("phase") == "unavailable":
        raise TaskError(
            "live_work", f"{reference}: the dispatcher state is unreadable, so nothing was cancelled", 3
        )
    records = state.records(payload)
    if not replay:
        task = writer.reader.show(reference)
        _admit(task, reference)
        if successor:
            _succession(writer, task, successor)
        record = records.get(reference)
        if record is not None:
            _stop_heads(runtime, task, record, reference, state, payload, records, progress)
        if task["state"] in {"ci", "review"}:
            writer.move(
                role=role.value, actor=actor, reference=reference, target="blocked",
                reason=reason, request_id=f"{request_id}-withdrawal",
                sprint_override=role is Role.PO,
                sprint_override_reason=reason if role is Role.PO else "",
                terminal_taxonomy=normalize_terminal_taxonomy(disposition="operator_stop").to_record(),
            )

    moves: list[tuple[dict[str, Any], str]] = []
    successors: list[dict[str, Any]] = []

    def event_payload(task: dict[str, Any]) -> dict[str, Any]:
        named: dict[str, Any] = {}
        if successor:
            new, moves[:] = _succession(writer, task, successor)
            successors[:] = [new]
            named = {"successor": successor, "repointed": {card["ref"]: blocked for card, blocked in moves}}
        return {
            **named,
            "reason_sha256": _digest(reason),
            "reason": reason,
            "from": task["state"],
            "type": task.get("type"),
            "sprint": task.get("sprint"),
            "claim": task["claim"]["worker"],
            "stopped": list(progress.asked),
            "dispatcher_record": reference in records,
        }

    def mutation(task: dict[str, Any]) -> None:
        number = _task_number(task)
        try:
            for card, blocked in moves:
                writer.client.call("saveTaskMetadata", task_id=_task_number(card), values={"blocked_by": blocked})
            for new in successors:
                writer.client.call("saveTaskMetadata", task_id=_task_number(new), values={"supersedes": reference})
            if task["claim"]["worker"] is not None:
                writer.client.call("saveTaskMetadata", task_id=number, values={"claim": ""})
            writer.client.call(
                "createComment", task_id=number, user_id=0, content=f"{_marker(successor)}\n{reason}"
            )
            if not writer.client.call("closeTask", task_id=number):
                raise TaskError("backend_error", "board store rejected the cancel", 1)
        except Exception as exc:
            raise _CommittedWriteError() from exc

    identity = {"reason_sha256": _digest(reason), **({"successor": successor} if successor else {})}
    result = writer._write(
        SUPERSEDED if successor else CANCELLED, role.value, actor, reference, request_id, event_payload,
        mutation, identity=identity, retry_payload={"reason": reason},
    )
    progress.archived = result["event_id"]
    try:
        writer._request_workspace_cleanup(result["task"], "archive")
        removed = records.pop(reference, None) is not None
        if removed:
            state.put_records(payload, records)
            state.save(payload)
    except (TaskError, HostError, OSError) as exc:
        if allowance.deferrals:
            raise _busy(reference, allowance, progress) from None
        raise TaskError(
            "cancel_pending",
            f"{reference} is archived (event {progress.archived}), but {_PENDING}: "
            f"{exc.message if isinstance(exc, TaskError) else exc}; {_FINISH}",
            3,
        ) from None
    return {**result, "stopped": progress.asked, "record_removed": removed, "workspace_cleanup": "requested"}


def _admit(task: dict[str, Any], reference: str) -> None:
    if task.get("record_type") in {"issue", "product"}:
        raise TaskError(
            "transition_forbidden",
            "a Product or Issue is not cancelled: close an issue with `ummanu issue close`",
            3,
        )
    if task.get("closed"):
        raise TaskError("transition_forbidden", f"{reference} is already archived", 3)
    if task["state"] == CardState.DONE.value:
        raise TaskError(
            "transition_forbidden", f"{reference} is Done; a Done card is archived with `task archive`", 3
        )


def _successor(writer: Any, reference: str, successor: str) -> dict[str, Any]:
    """The open card that may take `reference`'s place, or the refusal naming why it may not."""
    if not successor:
        raise TaskError("validation", "a supersede needs the successor card (--by)", 2)
    if successor == reference:
        raise TaskError("validation", f"{reference} cannot supersede itself", 2)
    try:
        new = writer.reader.show(successor)
    except TaskError as exc:
        if exc.code != "not_found":
            raise
        raise TaskError("not_found", f"the successor {successor} was not found", 2) from None
    if new.get("record_type") in _TYPED_RECORD_TYPES:
        raise TaskError("validation", f"the successor {successor} is a {new['record_type']}, not a card", 2)
    if new.get("closed"):
        raise TaskError("validation", f"the successor {successor} is archived", 2)
    if new["state"] == CardState.DONE.value:
        raise TaskError("validation", f"the successor {successor} is Done; it can no longer hold a dependent", 2)
    held = new["workspace"]["supersedes"]
    if held and held != reference:
        raise TaskError(
            "validation", f"the successor {successor} already supersedes {held}; a card supersedes one card", 2
        )
    return new


def _succession(
    writer: Any, old: dict[str, Any], successor: str
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], str]]]:
    """The successor, and each open dependent of `old` with the `blocked_by` it gets with the successor
    in `old`'s place; refused when that would make the successor wait for itself."""
    reference = old["ref"]
    new = _successor(writer, reference, successor)
    cards = [card for card in writer.reader.list() if card.get("record_type") not in _TYPED_RECORD_TYPES]
    graph = {card["ref"]: _blocker_refs(card) for card in cards}
    moves = []
    for card in cards:
        blockers = graph[card["ref"]]
        if reference in blockers and card["state"] != CardState.DONE.value:
            graph[card["ref"]] = list(dict.fromkeys(successor if ref == reference else ref for ref in blockers))
            moves.append((card, ",".join(graph[card["ref"]])))
    cycle = _cycle(graph, successor)
    if cycle:
        raise TaskError(
            "dependency_cycle",
            f"the repoint would make {successor} wait for itself: {' -> '.join(cycle)}, each blocked_by "
            "the next; nothing was repointed",
            2,
        )
    return new, moves


def _cycle(graph: dict[str, list[str]], start: str) -> list[str]:
    """A `blocked_by` path from `start` back to itself, or `[]` when there is none."""
    seen: set[str] = set()
    paths = [[start]]
    while paths:
        path = paths.pop()
        for blocker in graph.get(path[-1], ()):
            if blocker == start:
                return [*path, start]
            if blocker not in seen:
                seen.add(blocker)
                paths.append([*path, blocker])
    return []


def _marker(successor: str) -> str:
    """The first line of the archive comment: `[cancel]`, or the successor a supersede names."""
    return f"[superseded by {successor}]" if successor else "[cancel]"


def _stop_heads(
    runtime: Callable[[], Any] | None,
    task: dict[str, Any],
    record: DispatcherRecord,
    reference: str,
    state: ProductionState,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    progress: _Progress,
) -> None:
    """Stop every head the record names, each stop receipt saved as it lands; `progress.asked`
    names the runs once the first is asked to stop."""
    if not launch_intent(record) and not record.needs_settling():
        return
    if runtime is None:
        raise TaskError(
            "live_work", f"{reference} has dispatcher heads and no dispatcher runtime to stop them", 3
        )
    dispatcher = runtime()
    runs = _named_runs(record)
    allowance = _ALLOWANCE.get()

    def flush() -> None:
        state.put_records(payload, records)
        state.save(payload)

    try:
        # Durable before any head is asked to stop, so an unconfirmed stop leaves its heads named.
        dispatcher.cleanup.remember(task, record)
        progress.asked = runs
        with dispatcher.host.committing(flush):
            failure = _stop_record_heads(dispatcher, record, reference, task["state"])
        flush()
        if failure is not None:
            dispatcher.cleanup.remember(task, record)
    except Deferred:
        raise
    except HostError as exc:
        raise TaskError("live_work", f"{reference}: {exc}; nothing was archived", 3) from None
    if failure is not None and allowance is not None and allowance.deferrals:
        # A receipt flush that ran out of allowance surfaces as a refused stop; it is a held lock.
        raise allowance.defer(allowance.deferred_at)
    if failure is not None:
        raise TaskError(
            "head_stop_unconfirmed",
            f"{reference}: {', '.join(_named_runs(record)) or 'a head'} not confirmed stopped: "
            f"{failure['reason']}; nothing was archived, the dispatcher record and its cleanup intent "
            "keep the heads; retry with the same request id",
            3,
        )


def _named_runs(record: DispatcherRecord) -> list[str]:
    """The heads the record still owns, by role and run id."""
    names = []
    for role, label, stored in (
        (REVIEW_ROLE, "reviewer", record.review_head_run),
        (WORKER_ROLE, "worker", record.worker_head_run),
    ):
        run = stored.to_json()
        if record.owns_head(role) and run.get("lifecycle") != "exited":
            names.append(f"{label} run {run.get('run_id') or 'unrecorded'}")
    intent = launch_intent(record)
    if intent:
        launched = intent["head_run"] if isinstance(intent.get("head_run"), dict) else {}
        names.append(f"{intent['role']} launch {launched.get('run_id') or 'unrecorded'}")
    return names


_PENDING = "removing its dispatcher record and requesting its workspace disposal are still pending"
_FINISH = "repeat the same request id to finish them"


def _busy(reference: str, allowance: Allowance, progress: _Progress) -> TaskError:
    """The refusal of a spent allowance, naming what held the card and what the cancel left."""
    held = _holder(allowance.deferred_at)
    if progress.archived:
        return TaskError(
            "card_busy",
            f"{reference} is archived (event {progress.archived}), but {_PENDING}: within "
            f"{allowance.seconds:g} s {held}; {_FINISH}",
            3,
        )
    after = (
        f"its heads ({', '.join(progress.asked)}) were asked to stop and the cleanup intent naming them "
        "is kept"
        if progress.asked
        else "nothing was stopped"
    )
    return TaskError(
        "card_busy",
        f"{reference} was not cancelled within {allowance.seconds:g} s: {held}; "
        f"{after}, nothing was archived; retry with the same request id",
        3,
    )


def _holder(what: str) -> str:
    """What a lock wait waited on, with the processes `/proc/locks` names as holding it."""
    if not os.path.isabs(what):
        return f"waited on {what}"
    try:
        pids = sorted(set(_flock_holders(os.stat(what))))
    except OSError as exc:
        return f"{what} is held (holder unreadable: {exc.strerror or exc})"
    if not pids:
        return f"{what} was held and is free now"
    return f"{what} is held by " + ", ".join(f"pid {pid} ({_command(pid)})" for pid in pids)


def _command(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "exited"
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:160] or "no command line"


def _cancel_wait(
    writer: Any, role: Role, actor: str, reference: str, reason: str, request_id: str
) -> dict[str, Any]:
    """Record the cancel of a waiting wait card: `wait_cancel`, `[wait:cancel]` and `wait_cancelled`
    in one write. The dispatcher freezes `cancelled`, delivers it and closes the card."""
    at = _now()

    def payload(task: dict[str, Any]) -> dict[str, Any]:
        _check_execution_record(task)
        if not is_wait(task):
            raise TaskError(
                "validation", f"{reference} is a {task.get('type') or 'typeless'} card, not a wait", 2
            )
        if task["state"] not in _WAITING:
            raise TaskError(
                "transition_forbidden", f"a wait card is cancelled while it waits; {reference} is {task['state']}", 3
            )
        if (held := wait_card.wait_cancel(task)) is not None:
            raise TaskError(
                "already_cancelled",
                f"{reference} was cancelled at {held['at']}: {held['reason']}; "
                f"its `cancelled` delivery to {_returns(task)} is pending",
                3,
            )
        if (result := wait_card.wait_state(task).result) is not None:
            raise TaskError(
                "already_settled",
                f"{reference} already has its result ({result.get('outcome')}); it stands, and its delivery "
                f"to {_returns(task)} is pending",
                3,
            )
        return {"marker": role.value, "reason_sha256": _digest(reason), "sprint": task.get("sprint"),
                "wait_cancel": at}

    def mutation(task: dict[str, Any]) -> None:
        number = _task_number(task)
        writer.client.call(
            "saveTaskMetadata",
            task_id=number,
            values={wait_card.WAIT_CANCEL: wait_card.cancel_text(at, actor, role.value, reason)},
        )
        writer.client.call(
            "createComment", task_id=number, user_id=0, content=f"[{role.value}]\n[wait:cancel]\n\n{reason}\n"
        )

    return writer._write(
        WAIT_CANCELLED, role, actor, reference, request_id, payload, mutation,
        identity={"reason_sha256": _digest(reason)},
    )


def _returns(task: dict[str, Any]) -> str:
    """The wait's return addresses still owed its result; `observer` is the terminal move."""
    spec = wait_card.wait_spec(task)
    if spec is None:
        return "the terminal move"
    owed = wait_card.pending_addresses(spec, wait_card.wait_state(task))
    return ", ".join([*owed, "the terminal move"])


def finish_pending_cancel(writer: Any, event: dict[str, Any], retry_payload: dict[str, Any] | None) -> None:
    """Finish a staged cancel's or supersede's board effect: the recorded repoint and supersession,
    the claim settled, the reason comment, the card closed."""
    payload = event["payload"] if isinstance(event.get("payload"), dict) else {}
    expected = text(payload.get("reason_sha256"))
    successor = text(payload.get("successor"))
    reason = text((retry_payload or {}).get("reason"))
    if reason and _digest(reason) != expected:
        raise TaskError("validation", "cancel retry reason does not match the pending request", 2)
    board_id, _, _ = writer.reader._board()
    raw = project_card_by_reference(writer.client, board_id, str(event["ref"]))
    if not isinstance(raw, dict):
        raise TaskError("not_found", "task was not found", 2)
    if not _task_is_active(raw):
        return
    task = writer.reader.show(str(event["ref"]))
    number = _task_number(task)
    if successor:
        for dependent, blocked in payload["repointed"].items():
            writer.client.call(
                "saveTaskMetadata", task_id=_task_number(writer.reader.show(dependent)), values={"blocked_by": blocked}
            )
        writer.client.call(
            "saveTaskMetadata", task_id=_task_number(writer.reader.show(successor)), values={"supersedes": task["ref"]}
        )
    if task["claim"]["worker"] is not None:
        writer.client.call("saveTaskMetadata", task_id=number, values={"claim": ""})
    if not _has_cancel_reason(writer, raw, _marker(successor), expected):
        if not reason:
            raise TaskError("backend_error", "pending cancel reason comment is missing", 1)
        writer.client.call("createComment", task_id=number, user_id=0, content=f"{_marker(successor)}\n{reason}")
    if not writer.client.call("closeTask", task_id=number):
        raise TaskError("backend_error", "pending cancel remains incomplete", 1)


def _has_cancel_reason(writer: Any, raw: dict[str, Any], marker: str, reason_sha256: str) -> bool:
    task_id = positive_int(raw.get("id"))
    if task_id is None:
        raise TaskError("backend_error", "board store returned an invalid task", 1)
    for comment in writer.client.call("getAllComments", task_id=task_id) or []:
        body = _normalize_comment(comment)["body"] if isinstance(comment, dict) else ""
        if body.startswith(f"{marker}\n") and _digest(body.split("\n", 1)[1]) == reason_sha256:
            return True
    return False
