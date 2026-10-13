"""The durable identity carried by a dispatcher-launched head heartbeat.

The heartbeat file is deliberately an assertion about one *launch*, not merely an answer to
"does this integer still name a process?".  A PID can be reused after the head exits, and a file
survives a reboot, so every reader compares the producer's run, role and task binding as well as
the kernel's boot and process-start identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ummanu.dispatch.card_roles import head_role

# One definition, beside the writer and the reader of the record it versions.
from ummanu.runtime.head.identity import HEARTBEAT_VERSION as HEARTBEAT_VERSION, task_binding as launch_task


def heartbeat_role(role: str) -> str:
    """The stable role name written into a heartbeat record."""
    return head_role(str(role or ""))


def task_binding(task_ref: Mapping[str, Any] | None, fallback: str = "") -> str:
    """A compact durable binding for the task a run was launched to serve."""
    if isinstance(task_ref, Mapping):
        binding = launch_task(str(task_ref.get("kind") or ""), str(task_ref.get("ref") or ""))
        if binding:
            return binding
    return fallback


def sprint_task(reference: str) -> str:
    """An observer's binding for one sprint: `sprint:<ID>`, whether or not `reference` is prefixed."""
    return launch_task("sprint", reference)


def heartbeat_identity(
    *,
    run_id: str,
    role: str,
    task_ref: Mapping[str, Any] | None = None,
    task: str = "",
    leaf: str = "",
) -> dict[str, str]:
    """The non-kernel facts a heartbeat writer and reader must agree on."""
    return {
        "run_id": str(run_id or ""),
        "role": heartbeat_role(role),
        "task": task_binding(task_ref, task),
        "leaf": str(leaf or ""),
    }


def intent_heartbeat_identity(intent: Mapping[str, Any], *, task: str = "") -> dict[str, str]:
    """Expected identity while a launch is still represented only by its durable intent."""
    role = str(intent.get("role") or "")
    return heartbeat_identity(
        run_id=str(intent.get("run_id") or ""),
        role=role,
        task=task or str(intent.get("task") or ""),
        leaf=str(intent.get("leaf") or ""),
    )


def run_heartbeat_identity(
    run: Mapping[str, Any] | None, *, role: str, task: str = "", leaf: str = ""
) -> dict[str, str]:
    """Expected identity for a persisted ``ummanu.runtime.head.HeadRun``."""
    payload = run if isinstance(run, Mapping) else {}
    return heartbeat_identity(
        run_id=str(payload.get("run_id") or ""),
        role=role,
        task_ref=(payload.get("task_ref") if isinstance(payload.get("task_ref"), Mapping) else None),
        task=task,
        leaf=leaf or str(payload.get("leaf") or ""),
    )
