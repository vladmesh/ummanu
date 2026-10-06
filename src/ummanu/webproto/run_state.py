"""What became of one product run, in the read layer's existing vocabulary. Writes nothing.

* `value` is one of :data:`ummanu.webproto.agents.AGENT_STATES`; `exit` is the process's own
  `{"code", "signal"}` from the supervisor journal (normal end: `finished`, code 0; failure:
  `process_failed` with a code or a signal), so no new vocabulary is added.
* `ended` (the run is over: its process is provably gone or never spawned) is a separate fact from
  `value` (how it ended); `source_unavailable` can be an ending.
* Evidence is only files this product owns: the head's launch identity (via
  `ummanu.dispatch.watchdog.head_process_status`), the supervisor journal (`run.exited`) and the
  run's result file. Never a pane, window or session manager.
* Decision order: a published result plus an ended process is `finished`, however the process ended
  (including a stop this product requested); only then do exit status and absence speak.

Settling a run is :meth:`ummanu.webproto.runs.RunStore.settle`, called by the operation layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ummanu.dispatch.watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_IDENTITY_MISMATCH,
    HEARTBEAT_LIVE_MATCH,
    HEARTBEAT_NOT_YET_WRITTEN,
    HEARTBEAT_UNREADABLE,
    head_process_status,
)
from ummanu.runtime.head.identity import task_binding
from ummanu.runtime.head.task_ref import TASK_CARD
from ummanu.webproto import sources
from ummanu.webproto.agents import (
    FINISHED,
    PROCESS_FAILED,
    RUNNING,
    SOURCE_UNAVAILABLE,
    UNKNOWN,
)
from ummanu.webproto.runs import CLAIMED, UNRESOLVED, ProductRun

# Re-exported for the operation layer, which records the journal's path on every run it starts.
from ummanu.runtime.local_pty_head import JOURNAL_NAME as JOURNAL_NAME

# The substrate's own names for the exit record and its file, so this reader and that writer agree.
from ummanu.runtime.local_pty_head import RUN_EXITED, head_run_journal

#: The three values that name an ending. A run that is over while the evidence says `running` or
#: `unknown` is recorded `source_unavailable` by :meth:`ummanu.webproto.lifecycle.RunLifecycle._ending`.
#: Whether a run is over is the separate `ended` fact, never decided from this tuple.
ENDING_VALUES = (FINISHED, PROCESS_FAILED, SOURCE_UNAVAILABLE)

#: A run's phase (:mod:`ummanu.webproto.lifecycle`) is not its state. Only settled (recorded ending)
#: and unresolved (`unknown`) phases answer the state by themselves; every other phase is decided
#: from process evidence by :func:`from_evidence`.


def observe(run: ProductRun, *, now: float) -> dict[str, Any]:
    """This run's state, its exit status and its result, from process evidence alone."""
    if run.ended:
        # A settled run says the same thing forever, from its record (files may have been swept);
        # this also keeps its terminal event deterministic when republished.
        return _document(
            run.settled_state,
            run.settled_reason,
            exit_status=settled_exit(run),
            result=settled_result(run),
            heartbeat={"state": "settled"},
            # Off the record, not the value: a settled run is over whatever it ended as.
            ended=True,
            now=now,
            settled_at=run.settled_at,
        )
    if run.phase == UNRESOLVED:
        # Cleanup not confirmed: `unknown`, not `process_failed` (no evidence of failure). `ended` stays
        # False so `admission.admit` refuses a second run; heartbeat and journal still travel.
        return _document(
            UNKNOWN,
            run.unresolved_reason
            or "this run's head could not be confirmed stopped, so its ownership is unresolved",
            exit_status=_exit_status(_journal(run)[0]),
            result=_result(run),
            heartbeat=head_process_status(run.pid_file, expected=expected_identity(run)),
            # A head may still be alive under this run, so no gate may treat it as over.
            ended=False,
            now=now,
        )
    if run.phase == CLAIMED:
        return _document(
            UNKNOWN,
            "this run holds a request and a workspace, and no head has been raised under it yet",
            exit_status=_empty_exit(),
            result=_result(run),
            heartbeat={"state": HEARTBEAT_NOT_YET_WRITTEN},
            # Not begun; the lifecycle knows no process was spawned and establishes `ended` on close.
            ended=False,
            now=now,
        )
    return from_evidence(run, now=now)


def from_evidence(run: ProductRun, *, now: float) -> dict[str, Any]:
    """This run's state from heartbeat, journal and result file right now, with no phase shortcut.

    A close classifies from here, not :func:`observe`: an unresolved run may since have published a
    result and ended normally, and the stored `unresolved` shortcut would lose that ending.
    """
    heartbeat = head_process_status(run.pid_file, expected=expected_identity(run))
    journal, journal_failure = _journal(run)
    exit_status = _exit_status(journal)
    result = _result(run)
    state, reason, ended = _classify(
        heartbeat=heartbeat,
        exit_status=exit_status,
        result=result,
        journal_failure=journal_failure,
    )
    return _document(
        state,
        reason,
        exit_status=exit_status,
        result=result,
        heartbeat=heartbeat,
        ended=ended,
        now=now,
    )


def terminal_evidence(run: ProductRun) -> tuple[dict[str, Any], dict[str, Any]]:
    """The exit status and result behind an ending, read once at settle time, in the recorded shape."""
    return _exit_status(_journal(run)[0]), _result(run)


def settled_exit(run: ProductRun) -> dict[str, Any]:
    """The exit status recorded with this run's ending, or the empty one for an ending without."""
    recorded = run.settled_exit
    return dict(recorded) if isinstance(recorded, dict) and recorded else _empty_exit()


def settled_result(run: ProductRun) -> dict[str, Any]:
    """The result recorded with this run's ending, or "there was none"."""
    recorded = run.settled_result
    if isinstance(recorded, dict) and recorded:
        return dict(recorded)
    return {"present": False, "value": None, "reason": None}


def expected_identity(run: ProductRun) -> dict[str, str]:
    """The identity this run's head wrote, in the shape `LocalPtyHeadRuntime._process_alive` compares.

    Run id, role and the card task binding (`card:<ref>`); the reader also accepts the older bare ref.
    """
    return {"run_id": run.run_id, "role": run.role, "task": task_binding(TASK_CARD, run.ref)}


def _classify(
    *,
    heartbeat: dict[str, Any],
    exit_status: dict[str, Any],
    result: dict[str, Any],
    journal_failure: str,
) -> tuple[str, str, bool]:
    """How the run ended, why, and whether it is over (`ended`, from evidence, not from the value).

    A gone head with an unreadable journal is over yet `source_unavailable`; an unreadable launch
    identity is not over under the same value. A pid owned by another process is `unknown` and not
    over: pid reuse proves nothing about this run.
    """
    state = str(heartbeat.get("state") or "")
    if state == HEARTBEAT_LIVE_MATCH:
        return RUNNING, "a live process matches this run's launch identity", False
    if state == HEARTBEAT_UNREADABLE:
        return (
            SOURCE_UNAVAILABLE,
            (
                f"this run's launch identity could not be read ({heartbeat.get('reason') or 'unreadable'}), "
                "so nothing is proven about its process either way"
            ),
            False,
        )
    ended = state == HEARTBEAT_DEAD or bool(exit_status["recorded"])
    if not ended:
        if state == HEARTBEAT_IDENTITY_MISMATCH:
            return (
                UNKNOWN,
                (
                    "the pid in this run's launch identity belongs to another process, which proves "
                    "nothing about this run"
                ),
                False,
            )
        return UNKNOWN, "this run's head has not published a launch heartbeat yet", False
    if journal_failure and not exit_status["recorded"]:
        return (
            SOURCE_UNAVAILABLE,
            (
                f"this run's head is gone and its journal could not be read ({journal_failure}), so "
                "how it ended could not be established"
            ),
            True,
        )
    if result["present"]:
        return (
            FINISHED,
            "this run published its result and its head's process has ended" + _exit_tail(exit_status),
            True,
        )
    if exit_status["code"] == 0:
        return FINISHED, "this run's head process ended normally, and it published no result", True
    if exit_status["code"] is not None:
        return PROCESS_FAILED, f"this run's head process exited with status {exit_status['code']}", True
    if exit_status["signal"] is not None:
        return PROCESS_FAILED, f"this run's head process was ended by signal {exit_status['signal']}", True
    return (
        PROCESS_FAILED,
        "this run's head process is gone, it published no result, and nothing recorded how it ended",
        True,
    )


def _exit_tail(exit_status: dict[str, Any]) -> str:
    if exit_status["code"] is not None:
        return f" (exit status {exit_status['code']})"
    if exit_status["signal"] is not None:
        return f" (ended by signal {exit_status['signal']})"
    return ""


def _journal(run: ProductRun) -> tuple[tuple[dict[str, Any], ...], str]:
    """This run's supervisor journal, and the reason it could not be read when it could not."""
    run_dir = Path(run.journal_path).parent if run.journal_path else Path(run.run_dir or ".")
    try:
        return head_run_journal(run_dir), ""
    except OSError as exc:
        return (), f"{type(exc).__name__}: {exc}"


def _exit_status(events: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("kind") != RUN_EXITED:
            continue
        code = event.get("exit_code")
        signal = event.get("signal")
        result: dict[str, Any] = {
            "recorded": True,
            "code": code if isinstance(code, int) else None,
            "signal": signal if isinstance(signal, int) else None,
            "at": event.get("at"),
        }
        if event.get("head_loss_reason") == "memory_limit" and signal == 9:
            result["head_loss_reason"] = "memory_limit"
        return result
    return _empty_exit()


def _empty_exit() -> dict[str, Any]:
    return {"recorded": False, "code": None, "signal": None, "at": None}


def _result(run: ProductRun) -> dict[str, Any]:
    """The head's own result document, when it wrote one.

    A result that is not JSON is present with a null value and a reason, unlike no result at all.
    """
    if not run.result_path:
        return {"present": False, "value": None, "reason": "this run declares no result path"}
    path = Path(run.result_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"present": False, "value": None, "reason": None}
    except OSError as exc:
        return {"present": False, "value": None, "reason": f"the result file could not be read: {exc}"}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        return {"present": True, "value": None, "reason": f"the result file is not JSON: {exc}"}
    return {"present": True, "value": value if isinstance(value, dict) else {"value": value}, "reason": None}


def verdict_of(result: dict[str, Any]) -> str | None:
    """The reviewer's verdict, when the result carries one. Free text is not a verdict."""
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    verdict = value.get("verdict")
    return verdict if isinstance(verdict, str) and verdict else None


def _document(
    state: str,
    reason: str,
    *,
    exit_status: dict[str, Any],
    result: dict[str, Any],
    heartbeat: dict[str, Any],
    ended: bool,
    now: float,
    settled_at: float = 0.0,
) -> dict[str, Any]:
    """One run's state document. `ended` is passed in by the caller holding the evidence, never derived."""
    available = state != SOURCE_UNAVAILABLE
    source = sources.available(now) if available else sources.unavailable(reason, now=now)
    return {
        "value": state,
        "reason": reason,
        "ended": bool(ended),
        "settled_at": sources.isoformat(settled_at) if settled_at else None,
        "source": source.to_json(),
        "exit": {"code": exit_status["code"], "signal": exit_status["signal"], "at": exit_status["at"]},
        "result": {
            "present": bool(result["present"]),
            "value": result["value"],
            "reason": result["reason"],
            "verdict": verdict_of(result),
        },
        "evidence": {
            "kind": "process_heartbeat",
            "heartbeat_state": str(heartbeat.get("state") or ""),
            "pid": heartbeat.get("pid") if isinstance(heartbeat.get("pid"), int) else None,
            "detail": str(heartbeat.get("reason") or "") or None,
        },
    }
