"""A product run's two events, written into the card history the read layer already reads.

No second history: events go through the card client's audit owner
(:func:`ummanu.tasks.task_audit_for`, `docs/BOARD_STORE.md` §7.3) as generic audit records, and
:class:`ummanu.webproto.journal.CommittedAudit` reads them back unchanged.

``product_run.started``   a head was raised: run, role, profile, pid, workspace, run dir, log
``product_run.finished``  the run reached a terminal state: state, exit, result and verdict

Idempotent through the audit itself: the request id derives from the run id, and every field
(including `occurred_at`) derives from the run record, so a republish (done on every path that
returns an existing run) builds a byte-identical event. Hence `state` must come from
:mod:`ummanu.webproto.run_state`, not from a run directory that may be swept. Generic, not typed
Card events: a run moves no card and must not wake a sprint observer.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from ummanu.tasks import BOARD_STORE_KIND, TaskError
from ummanu.webproto.errors import RuntimeUnavailable
from ummanu.webproto.runs import ProductRun

STARTED = "product_run.started"
FINISHED = "product_run.finished"

#: Deliberately not a pipeline role, so the history is never read as an attempt.
ACTOR = {"role": "product-runtime", "id": "ummanu.webproto"}


def publish_started(audit: Any, run: ProductRun) -> dict[str, Any]:
    """Record that this run's head was raised, on the card's own history."""
    return _publish(
        audit,
        run,
        kind=STARTED,
        outcome="success",
        occurred_at=_isoformat(run.started_at),
        payload={
            "run_id": run.run_id,
            "role": run.role,
            "profile": run.profile,
            "adapter": run.adapter,
            "runtime": run.runtime,
            "parent_run_id": run.parent_run_id,
            "project": run.project,
            "workspace": run.workspace,
            "run_dir": run.run_dir,
            "pid_file": run.pid_file,
            "journal": run.journal_path,
            "log": run.log_path,
            "result_path": run.result_path,
            "head_pid": run.head_pid,
            "supervisor_pid": run.supervisor_pid,
        },
    )


def publish_finished(audit: Any, run: ProductRun, state: dict[str, Any]) -> dict[str, Any]:
    """Record how this run ended, once, on the same history its start is on.

    `outcome` is the audit's two-valued field about the work (`success` only for `finished`);
    `payload.state` is the exact run state, e.g. `source_unavailable` with outcome `failure`.
    """
    result = state.get("result") if isinstance(state.get("result"), dict) else {}
    return _publish(
        audit,
        run,
        kind=FINISHED,
        outcome="success" if run.settled_state == "finished" else "failure",
        occurred_at=_isoformat(run.settled_at),
        payload={
            "run_id": run.run_id,
            "role": run.role,
            "profile": run.profile,
            "parent_run_id": run.parent_run_id,
            "project": run.project,
            "state": run.settled_state,
            "reason": run.settled_reason,
            "exit": state.get("exit"),
            "result_present": bool(result.get("present")),
            "result": result.get("value"),
            "verdict": result.get("verdict"),
            "workspace": run.workspace,
            "run_dir": run.run_dir,
            "journal": run.journal_path,
        },
    )


def request_id_for(run_id: str, kind: str) -> str:
    """The one request id a given run's given event is ever written under."""
    return f"product-run:{run_id}:{kind}"


def _publish(
    audit: Any,
    run: ProductRun,
    *,
    kind: str,
    outcome: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request_id = request_id_for(run.run_id, kind)
    event = {
        "event_id": "evt_" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32],
        "schema_version": 1,
        "occurred_at": occurred_at,
        "actor": dict(ACTOR),
        "kind": kind,
        "outcome": outcome,
        "task_id": "",
        "ref": run.ref,
        "backend": {"kind": BOARD_STORE_KIND, "task_id": None, "revision": "not_written"},
        "request_id": request_id,
        "payload": payload,
    }
    try:
        audit.stage(request_id, event)
        audit.append(request_id, event)
    except TaskError as exc:
        raise RuntimeUnavailable(
            f"this run's {kind} event could not be written to the card's history: {exc.message}"
        ) from None
    except OSError as exc:
        raise RuntimeUnavailable(
            f"this run's {kind} event could not be written to the card's history: {exc}"
        ) from None
    return event


def _isoformat(moment: float) -> str:
    """The journal's own UTC spelling, so a replay of the same run builds the same record."""
    return datetime.fromtimestamp(moment, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
