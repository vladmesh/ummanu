"""One durable recovery obligation for a refused production activation.

Persist the original refusal and exact board requests in the release record before creation. Every
retry services those requests before another activation or terminal record removal: commit one PO
operation, commit one canonical reason naming it, then Block the source and remove the record. Board
admission failures retain the obligation and leave the original refusal intact. Committed request ids
recover interruptions after any board write without repeating it or consulting a moving Git ref.
"""

from __future__ import annotations

import json
from typing import Any

from ummanu.board import po_origin as origin_field
from ummanu.board.production_rights import ACTIVATION_OPERATION_REQUEST_PREFIX
from ummanu.board.release_migrations import (
    DESTRUCTIVE_PENDING,
    LOCK_TIMEOUT,
    MIGRATION_FAILED,
    UNCLASSIFIED_PENDING,
)
from ummanu.dispatch.entrypoint_guard import ENTRYPOINT_MOVED, RUNBOOK
from ummanu.dispatch.production_checkout import ProductionActivationRefused
from ummanu.dispatch.state import (
    ActivationRecovery,
    DispatcherRecord,
    attempt_request_id as _attempt_request_id,
    request_token,
)
from ummanu.dispatch.types import HostError
from ummanu.kernel.errors import TaskError

ACTION = "production-activation-blocked"
COMMENT_ACTION = "production-activation-refused"
HEADING = "## Production activation refused"


def operation_request_id(reference: str, attempt_id: str) -> str:
    return f"{ACTIVATION_OPERATION_REQUEST_PREFIX}{request_token(reference)}-{request_token(attempt_id)}"


def block_refused_activation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    refused: ProductionActivationRefused,
    *,
    step: str,
) -> dict[str, Any]:
    ref = str(task["ref"])
    attempt = record.attempt_id or attempt_id
    if record.activation_recovery is None:
        facts = refused.facts()
        project = str(task.get("project") or "")
        sprint = str(task.get("sprint") or "")
        record.activation_recovery = ActivationRecovery(
            facts=facts,
            operation={
                "role": "dispatcher",
                "actor": runtime.owner,
                "project": project,
                "task_type": "operation",
                "title": f"Recover the production activation of {ref} ({facts['reason']})",
                "description": _description(task, facts),
                "target": "ready",
                "sprint": sprint,
                "touches_production": project,
                "origin": None if sprint else origin_field.po_origin(task),
                "request_id": operation_request_id(ref, attempt),
            },
            comment_request_id=_attempt_request_id(attempt, COMMENT_ACTION, ref),
            block_request_id=_attempt_request_id(attempt, ACTION, ref),
            step=step,
        )
    records[ref] = record
    return resume_refused_activation(runtime, task, record, records, payload, attempt_id)


def resume_refused_activation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    from ummanu.dispatch.release_lifecycle import block_merge_path

    owed = record.activation_recovery
    assert owed is not None
    ref = str(task["ref"])
    # Also flush on re-entry after a failed state save. No fallible board write may outrun this.
    runtime.save_records(payload, records)
    try:
        operation = _operation(runtime, owed)
        facts = {**owed.facts, "operation": operation}
        _once(
            runtime,
            owed.comment_request_id,
            lambda request_id: runtime.writer.comment(
                role="dispatcher", actor=runtime.owner, reference=ref,
                body=_comment(facts), request_id=request_id,
            ),
        )
        outcome = block_merge_path(
            runtime, task, record, records, payload, attempt_id,
            blocked_reason="implementation",
            action=ACTION, reason=_reason(facts), step=owed.step,
            outcome="production activation refused", request_id=owed.block_request_id,
        )
    except (TaskError, OSError, HostError) as exc:
        # A failed final state save must leave the obligation in this tick's records too.
        records[ref] = record
        return {
            "status": "degraded", "step": owed.step, "pilot_ref": ref,
            "action": "production-activation-recovery-pending",
            "reason": f"production activation recovery remains owed: {getattr(exc, 'code', type(exc).__name__)}: {exc}",
            "activation_refused": dict(owed.facts),
        }
    outcome["activation_refused"] = {
        key: facts[key] for key in ("code", "reason", "revision", "target", "old", "operation")
    }
    return outcome


def _once(runtime: Any, request_id: str, write: Any) -> dict[str, Any] | None:
    """Write under `request_id` unless a write under it already committed; the committed event then."""
    committed = runtime.audit.committed_event(request_id)
    if committed is not None:
        return committed
    return write(request_id)


def _operation(runtime: Any, owed: ActivationRecovery) -> str:
    """Read a committed operation or submit the persisted request through ordinary admission."""
    committed = runtime.audit.committed_event(owed.operation["request_id"])
    if committed is not None and committed.get("ref"):
        return str(committed["ref"])
    if not owed.operation["sprint"] and owed.operation["origin"] is None:
        raise TaskError(
            "validation", "the source belongs to no sprint and carries no PO origin: no PO session executes an operation", 2,
        )
    created = runtime.writer.create(**owed.to_json()["operation"])
    return str(created["task"]["ref"])


def _landing_line(facts: dict[str, Any]) -> str:
    merge = facts.get("remote_merge") or {}
    if not merge.get("sha") and not merge.get("branch"):
        return "the release recorded no remote merge"
    via = "pull request merge" if merge.get("path") == "github-pr" else merge.get("path") or "merge"
    commit = f"`{merge['sha']}`" if merge.get("sha") else f"the merge of `{merge.get('branch')}`"
    return f"{commit} landed on `{merge.get('base')}` ({via}) and stays there"


def _transition_only(facts: dict[str, Any]) -> str:
    """Why an `entrypoint_moved` refusal is not retried: what to do instead."""
    return (
        f"The production checkout `{facts['checkout']}` deliberately stays on the old commit "
        f"`{str(facts['old'])[:12]}`: activation happens only through the transition runbook in "
        f"`{RUNBOOK}`, not by retrying the release or running `upgrade`"
    )


def _reason(facts: dict[str, Any]) -> str:
    revision = f" at revision {facts['revision']}" if facts.get("revision") else ""
    operation = facts["operation"]
    if facts["reason"] == ENTRYPOINT_MOVED:
        return (
            f"production activation refused ({facts['code']}: {facts['reason']}): {facts['message']}. "
            f"The merge landed on main: {_landing_line(facts)}. {_transition_only(facts)}. "
            f"Operation card: {operation}."
        )
    return (
        f"production activation refused ({facts['code']}: {facts['reason']}{revision}): {facts['message']}. "
        f"Delivered to the remote: {_landing_line(facts)}; the production checkout {facts['checkout']} stays at "
        f"{str(facts['old'])[:12]}. Operation card: {operation}."
    )


def _comment(facts: dict[str, Any]) -> str:
    cause = (
        f"does not keep the running entrypoint ({facts['reason']}); activation happens only through the "
        f"transition runbook in `{RUNBOOK}`"
        if facts["reason"] == ENTRYPOINT_MOVED
        else f"was refused ({facts['reason']})"
    )
    subject = "" if facts["reason"] == ENTRYPOINT_MOVED else "the board schema of "
    return "\n".join(
        [
            HEADING,
            "",
            (
                f"Delivered to the remote: {_landing_line(facts)}. Not activated: the production checkout "
                f"`{facts['checkout']}` stays at `{facts['old']}`, because {subject}`{facts['target']}` {cause}."
            ),
            "",
            f"Operation card: {facts['operation']}",
            "",
            "```json",
            json.dumps(facts, indent=2, sort_keys=True),
            "```",
        ]
    )


def _what_to_fix(facts: dict[str, Any]) -> str:
    reason, revision = facts["reason"], facts.get("revision") or "(none)"
    if reason == MIGRATION_FAILED:
        return (
            f"Revision `{revision}` failed on the production board: `{facts.get('cause')}`. It is a defect of "
            "the merged code: fix the revision on `main` through a code card; never repair the production "
            "database by hand."
        )
    if reason == LOCK_TIMEOUT:
        return (
            f"The migration waited longer than its bound for a lock (revision `{revision}`): another session "
            "held the migration advisory lock or a table the revision alters. Find it (`pg_locks`, "
            "`pg_stat_activity`), let it finish, then upgrade."
        )
    if reason in (DESTRUCTIVE_PENDING, UNCLASSIFIED_PENDING):
        return (
            f'Revision `{revision}` is not declared `release_safety = "additive"`, so the release applied '
            "nothing. Decide whether the running code survives it; applying it is a deliberate upgrade "
            "(`docs/BOARD_STORE.md` §7.4)."
        )
    return f"The release could not read or reach what it needed: {facts['message']}"


def _entrypoint_description(task: dict[str, Any], facts: dict[str, Any]) -> str:
    return "\n".join(
        [
            (
                f"The dispatcher released {task['ref']} but did not activate it on production: the target "
                "commit does not keep the entrypoint the live units execute, so the production checkout was "
                "not moved and the board schema was not touched."
            ),
            "",
            "## State",
            "",
            f"- The merge landed on `main`: {_landing_line(facts)}.",
            f"- Production checkout `{facts['checkout']}`: deliberately still at `{facts['old']}` (old code).",
            f"- Target code revision: `{facts['target']}`.",
            f"- Refusal: `{facts['code']}` / `{facts['reason']}`: {facts['message']}",
            "",
            "## Supported recovery",
            "",
            (
                f"Activation happens only through the transition runbook in `{RUNBOOK}`. Do not retry the "
                "release and do not run `upgrade`: both refuse this target the same way, and every "
                "later advance of this checkout is refused until the transition moves the installation."
            ),
            "",
            f"1. Drain the pipeline and run the transition as `{RUNBOOK}` describes.",
            "2. Verify the installation the transition reports, then complete this card with what was done.",
        ]
    )


def _description(task: dict[str, Any], facts: dict[str, Any]) -> str:
    if facts["reason"] == ENTRYPOINT_MOVED:
        return _entrypoint_description(task, facts)
    applied = ", ".join(facts.get("applied") or []) or "none"
    pending = ", ".join(facts.get("pending") or []) or "none recorded"
    return "\n".join(
        [
            (
                f"The dispatcher released {task['ref']} but did not activate it on production: the board "
                "schema of the target commit was refused, so the production checkout was not moved."
            ),
            "",
            "## State",
            "",
            f"- Delivered to the remote: {_landing_line(facts)}.",
            f"- Production checkout `{facts['checkout']}`: still at `{facts['old']}` (old code).",
            f"- Target code revision: `{facts['target']}`, migration head `{facts.get('target_head')}`.",
            f"- Board schema before the release: `{facts.get('store_revision_before')}`.",
            f"- Owed migrations, in order: {pending}.",
            f"- Applied by this release (committed, additive): {applied}.",
            (
                f"- Refused or failing migration: `{facts.get('revision') or 'none'}` — `{facts['code']}` / "
                f"`{facts['reason']}`: {facts['message']}"
            ),
            "",
            "## What to fix",
            "",
            _what_to_fix(facts),
            "",
            "## Supported recovery",
            "",
            (
                "1. Once the cause is fixed on `main`, run `ummanu upgrade` as the installation's runtime user: it "
                f"fast-forwards `{facts['checkout']}` and applies every owed revision under the same migration "
                "advisory lock."
            ),
            (
                "2. Verify: `git -C "
                f"{facts['checkout']} rev-parse HEAD` is `{facts['target']}` or a descendant of it, and "
                "`ummanu doctor --json` reports the board schema `current` with no pending migration and no "
                "finding beyond the sprint's baseline."
            ),
            "3. Complete this card with what was done and how it was verified.",
        ]
    )


__all__ = ["ACTION", "block_refused_activation", "operation_request_id", "resume_refused_activation"]
