"""Production host contracts implementation."""

from __future__ import annotations

from typing import Any

from ummanu.dispatch.helpers import scrub_host_output
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tui import READINESS_BUSY, delivery_readiness_state as _delivery_readiness_state
from ummanu.runtime import head as head_ops
from ummanu.runtime.head import with_pid_heartbeat as _with_pid_heartbeat


def _runtime_writes_launch_identity(runtime: Any) -> bool:
    """Whether this backend writes a head's launch-identity heartbeat itself.

    Exactly one layer owns the launch-identity heartbeat.
    """
    return bool(getattr(runtime, "writes_launch_identity", False))

def _launch_command(runtime: Any, command: str, pid_file: str, identity: dict[str, str]) -> str:
    """The command a head's backend is handed: wrapped by this dispatcher only when it owns the writer."""
    if _runtime_writes_launch_identity(runtime):
        return command
    return _with_pid_heartbeat(command, pid_file, identity=identity)

def _durable_head_run(subject: Any) -> head_ops.HeadRun | None:
    """The run a record names at a workspace-scoped stop, or `None` when it names none.

    Stop by the durable run identity, not the current profile.
    """
    if subject is None:
        return None
    if isinstance(subject, head_ops.HeadRun):
        return subject if subject.run_id else None
    if not isinstance(subject, dict) or not subject.get("run_id"):
        return None
    try:
        run = head_ops.HeadRun.from_json(subject)
    except (head_ops.HeadRunError, head_ops.TaskRefError, TypeError, ValueError):
        return None
    return run if run.run_id else None

def _delivery_evidence_json(carrier: Any, subject: str) -> dict[str, Any]:
    """The bounded evidence a delivery verdict or failure carries, ready to persist."""
    evidence = getattr(carrier, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if not isinstance(evidence, dict) or not evidence:
        if isinstance(carrier, BaseException):
            return {"subject": subject, "reason": scrub_host_output(str(carrier))[:400]}
        return {}
    evidence = dict(evidence)
    evidence["subject"] = subject
    return evidence

def _record_worker_delivery_evidence(
    record: DispatcherRecord, carrier: Any, *, failure: bool = False
) -> None:
    """Persist a worker prompt receipt before recovery can replace its conversation."""
    evidence = (
        dict(carrier)
        if isinstance(carrier, dict) and carrier
        else _delivery_evidence_json(carrier, "worker-prompt")
    )
    if not evidence:
        return
    record.worker_delivery_evidence = evidence
    typed = record.worker_delivery_evidence.evidence
    if failure and typed is not None and _delivery_readiness_state(typed) != READINESS_BUSY:
        record.worker_delivery_failures += 1
