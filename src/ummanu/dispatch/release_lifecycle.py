"""Final release, merge, completion-evidence and Done lifecycle."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from ummanu.board.completion_evidence import (
    RESEARCH_REPORT_DIR,
    RESEARCH_REPORT_FILE,
    RESEARCH_TRANSFER_FILE_CAP_BYTES,
    RESEARCH_TRANSFER_SUFFIXES,
    RESEARCH_TRANSFER_TOTAL_CAP_BYTES,
    has_candidate,
    missing_completion_evidence,
    render_research_completion_link,
    research_report_path,
    research_report_refusal,
)
from ummanu.board.terminal_taxonomy import BlockedReason
from ummanu.dispatch import attempt_accounting, e2e_stage, post_merge, release_activation
from ummanu.dispatch.decision_pointer import decision_pointer
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.helpers import scrub_host_output
from ummanu.dispatch.production_checkout import ProductionActivationRefused
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id as _attempt_request_id
from ummanu.dispatch.tick_telemetry import tick_stage
from ummanu.dispatch.types import GateTransportError, HostError, MergeLanding
from ummanu.knowledge_write import (
    KnowledgeError,
    KnowledgeValidationError,
    write_knowledge_directory,
)
from ummanu.state_repo import StateRepoError


def released_verdict(record: DispatcherRecord) -> str:
    """The verdict a decided release carries: the parked one, or `missing` when no reviewer ran."""
    return "missing" if record.worker_continuation.verdict_outcome == "missing" else "green"


def review_drift(runtime: Any, task: dict[str, Any], record: DispatcherRecord) -> str:
    """Has the checkout moved off the commit the reviewer was pointed at? A verdict describes one code
    state; merging a different one lands work nobody reviewed. Returns the operator message for the
    bounce, or "" when the states match, or when neither can be read — an unreadable workspace is
    the gate's failure to report, not a silent bounce.
    """
    record.review_reconciliation = None
    if not record.review_commit:
        return ""
    current = runtime.host.head_commit(record)
    if not current or current == record.review_commit:
        return ""
    reconciliation = runtime.host.reconcile_reviewed_base_move(
        task, record, record.review_commit, current
    )
    if reconciliation is not None:
        record.review_reconciliation = reconciliation
        return ""
    return (
        f"The review was given for commit `{record.review_commit[:12]}` while the working copy "
        f"is now on `{current[:12]}`: the verdict describes a different state of the code. The "
        f"card is back in In progress; rework it and report again."
    )


def merge_readiness(
    runtime: Any, task: dict[str, Any], record: DispatcherRecord
) -> tuple[str, GateResult | None, str]:
    """Everything that must hold before this checkout may be merged, read once.

    Returns one of "drift", "transport", "failed", "pending", "red" or "green". Both sides of the
    seam ask it: Validate before parking a green verdict, and the release again immediately before
    the merge. "transport" is deliberately not "failed" — a backend that could not be reached says
    nothing about the checkout, so the caller retries rather than deciding the card on silence.
    """
    drift = review_drift(runtime, task, record)
    if drift:
        return "drift", None, drift
    try:
        result = runtime.host.gate_check(task, record)
    except GateTransportError as exc:
        return "transport", None, str(exc)
    except HostError as exc:
        return "failed", None, scrub_host_output(str(exc))
    if result.status == "green":
        # The gate may have refresh-merged the base after the first identity check.
        drift = review_drift(runtime, task, record)
        if drift:
            return "drift", None, drift
        return "green", result, ""
    if result.status == "pending":
        return "pending", result, ""
    return "red", result, ""


def block_merge_path(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    action: str,
    blocked_reason: BlockedReason,
    reason: str,
    step: str,
    outcome: str,
    decision: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """A merge path that cannot finish leaves the card Blocked with its heads down."""
    ref = task["ref"]
    with tick_stage("release_teardown"):
        runtime.host.stop(record)
    with tick_stage("release_terminal"):
        attempt_accounting.terminal_effect(runtime, 
            task,
            record,
            target="blocked",
            reason=reason,
            decision=decision,
            request_id=request_id or _attempt_request_id(record.attempt_id or attempt_id, action, ref),
            terminal_state="blocked",
            disposition="blocked",
            verdict=record.worker_continuation.verdict_outcome
            if record.worker_continuation.verdict_outcome in {"green", "red", "blocked"}
            else "missing",
            blocked_reason=blocked_reason,
        )
        records.pop(ref, None)
        runtime.save_records(payload, records)
    return {"status": "blocked", "step": step, "pilot_ref": ref, "reason": outcome}


def transfer_research_report(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
) -> dict[str, Any] | None:
    """Move a research card's report directory into knowledge and link it; None when done.

    The report of `<workspace>/.ummanu-report/` and its small text attachments replace
    `state/knowledge/reports/<ref>/` through the knowledge
    directory writer (files only, no Git; the next checkpoint carries them), then one
    `[completion:research]` comment keyed on the report generation is written, so a replayed tick
    writes nothing new and no second link. A refused or failed
    transfer Blocks the card with the cause named, keeps the workspace and writes no link. Any
    other kind answers None at once.
    """
    if task.get("type") != "research":
        return None
    with tick_stage("release_evidence"):
        return _transfer_research_report(runtime, task, record, records, payload, attempt_id, step=step)


def _transfer_research_report(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
) -> dict[str, Any] | None:
    ref = task["ref"]
    generation = str(record.report_generation)
    source = Path(record.workspace) / RESEARCH_REPORT_DIR
    refusal, message = "", ""
    if research_report_refusal(Path(record.workspace)):
        refusal = "report_missing"
        message = f"the workspace holds no non-empty {RESEARCH_REPORT_DIR}/report.md"
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="ummanu-research-") as staging:
                _stage_research_report(source, Path(staging))
                write_knowledge_directory(
                    Path(runtime.catalog.instance_dir),
                    directory=research_report_path(ref),
                    actor="dispatcher",
                    source_dir=Path(staging),
                )
        except KnowledgeValidationError as exc:
            refusal, message = exc.reason or "refused", str(exc)
        except (KnowledgeError, StateRepoError, OSError) as exc:
            refusal, message = "write_failed", str(exc)
    if refusal:
        outcome = block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            blocked_reason="implementation",
            action="research-report-transfer-refused",
            reason=(
                f"research report transfer refused ({refusal}): {scrub_host_output(message)}. "
                f"Nothing was linked and the card cannot be Done; the workspace and its "
                f'`{RESEARCH_REPORT_DIR}/` are kept. See docs/PROTOCOLS.md, "Card kinds, live impact '
                'and the review choice".'
            ),
            step=step,
            outcome="research report transfer refused",
        )
        outcome["transfer_refusal"] = refusal
        return outcome
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=render_research_completion_link(ref),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id, "completion-research", ref, generation
        ),
    )
    return None


def _stage_research_report(source: Path, staging: Path) -> None:
    """Copy the report and its small text attachments into `staging`, naming every file left behind.

    `report.md` always goes; another regular file goes when its suffix is a text one and it fits
    the per-file and total caps. What stays is listed at the end of the staged `report.md`.
    """
    report = source / RESEARCH_REPORT_FILE
    shutil.copyfile(report, staging / RESEARCH_REPORT_FILE)
    budget = RESEARCH_TRANSFER_TOTAL_CAP_BYTES
    kept: list[str] = []
    for root, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(root) / name
            relative = path.relative_to(source)
            if relative == Path(RESEARCH_REPORT_FILE):
                continue
            entry = path.lstat()
            if not stat.S_ISREG(entry.st_mode) or path.suffix.lower() not in RESEARCH_TRANSFER_SUFFIXES:
                kept.append(f"- `{relative}`: not a text attachment")
            elif entry.st_size > RESEARCH_TRANSFER_FILE_CAP_BYTES:
                kept.append(f"- `{relative}`: {entry.st_size} bytes, over the per-file cap")
            elif entry.st_size > budget:
                kept.append(f"- `{relative}`: {entry.st_size} bytes, over what the total cap leaves")
            else:
                budget -= entry.st_size
                (staging / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, staging / relative)
    if kept:
        with (staging / RESEARCH_REPORT_FILE).open("a", encoding="utf-8") as handle:
            handle.write(
                "\n\n## Not transferred\n\n"
                f"These files were not copied to knowledge and are kept at `{source}`:\n\n"
                + "\n".join(kept)
                + "\n"
            )


def release_parked(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Perform an accept decision: re-check the mechanical state, then merge."""
    from ummanu.dispatch import gate_lifecycle

    if not has_candidate(task):
        # Nothing to re-check or merge: the release goes to the completion evidence check. A
        # research card parked by a red verdict reaches here without a transfer, and one parked
        # green has already made it, which this repeats as a no-op.
        refused = transfer_research_report(runtime,
            task, record, records, payload, attempt_id, step="assessment"
        )
        if refused is not None:
            return refused
        return release_effect(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            step="assessment",
            move_reason=decision_pointer(runtime, task, "accept"),
            decision="accept",
            verdict=released_verdict(record),
        )
    # The release audit's e2e stage reads the gate itself and binds to the SHA it validated: a SHA with
    # a green run (or one reconciled to it by a base-only move) is not dispatched again, and one without
    # waits for its own run first. The gate result it read is accepted below, once.
    def gate() -> tuple[dict[str, Any] | None, GateResult | None]:
        with tick_stage("release_gate"):
            return read_release_gate(runtime, task, record, records, payload, attempt_id)

    with tick_stage("release_e2e"):
        e2e = e2e_stage.run_stage(
            runtime, task, record, records, payload, attempt_id, step="assessment", gate=gate
        )
    if isinstance(e2e, dict):
        return e2e
    if e2e is None:
        outcome, result = gate()
        if outcome is not None:
            return outcome
        reconciliation = None
    else:
        result, reconciliation = e2e.result, e2e.reconciliation
    assert result is not None
    with tick_stage("release_gate"):
        blocked = gate_lifecycle.accept_green_gate(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            stage="release",
            e2e_reconciliation=reconciliation,
        )
    if blocked is not None:
        return blocked
    return release_effect(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        step="assessment",
        move_reason=decision_pointer(runtime, task, "accept"),
        decision="accept",
        verdict=released_verdict(record),
    )


def read_release_gate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> tuple[dict[str, Any] | None, GateResult | None]:
    """The release's merge gate read: `(outcome, None)` for anything but green, `(None, result)` on green.

    Nothing is accepted or attested here; the release accepts the green result once.
    """
    from ummanu.dispatch import gate_lifecycle

    ref = task["ref"]
    kind, result, detail = merge_readiness(runtime, task, record)
    if kind == "transport":
        # A release that could not ask the gate is not a release that was refused.
        retry = gate_lifecycle.gate_transport_retry(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            GateTransportError(detail),
            step="assessment",
        )
        if retry is not None:
            return retry, None
        return gate_lifecycle.block_gate_transport(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            step="assessment",
            action="release-gate-transport-blocked",
            prefix="Observer decision: accept. ",
        ), None
    if kind != "drift":
        # Only a result handed back to this path clears the transport retry budget.
        gate_lifecycle.gate_answered(runtime, ref, record, records, payload)
    if kind == "pending":
        if result is None:
            return block_merge_path(runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                blocked_reason="gate",
                action="release-gate-result-blocked",
                reason="merge gate returned pending without a result payload",
                step="assessment",
                outcome="merge gate result unavailable",
            ), None
        return gate_lifecycle.gate_pending(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            step="assessment",
            action="merge-gate-pending",
        ), None
    if kind != "green":
        summary = {
            "drift": f"the release cannot land: {detail}",
            "failed": f"the merge gate could not be read: {detail}",
        }.get(kind, "the mechanical gate is no longer green for the checkout this release was decided on")
        return block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            blocked_reason="implementation" if kind == "drift" else "gate",
            action=f"release-{kind}-blocked",
            reason=f"Observer decision: accept. {summary}",
            step="assessment",
            outcome=f"release {kind}",
        ), None
    if result is None:
        return block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            blocked_reason="gate",
            action="release-gate-result-blocked",
            reason="merge gate returned green without a result payload",
            step="assessment",
            outcome="merge gate result unavailable",
        ), None
    return None, result


def release_effect(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
    move_reason: str,
    decision: str = "",
    verdict: str = "green",
) -> dict[str, Any]:
    """Merge the reviewed branch, tear the round down and move the card to Done.

    This is the only way a card reaches Done, so the completion evidence check sits here: every
    release, automatic or decided, first taken or replayed after a lost tick, goes through it.

    A merge that landed a commit opens the post-merge CI watch and saves it before anything else
    happens, so a crash between the merge and Done cannot lose it; the Done then carries the
    `release_merge` marker, and the observer is woken on the watch's result instead of on the move.
    """
    ref = task["ref"]
    if record.activation_recovery is not None:
        with tick_stage("release_terminal"):
            return release_activation.resume_refused_activation(
                runtime, task, record, records, payload, attempt_id
            )
    release_merge: dict[str, Any] | None = None
    if has_candidate(task):
        try:
            # Its runtime fence, delivery, refresh and landed-commit read are each their own stage.
            landing = runtime.host.complete_green(task, record)
        except ProductionActivationRefused as exc:
            # Delivered to the remote, not activated on production: its own reason, one operation
            # for the PO, and the card Blocked (secretary-1824).
            with tick_stage("release_terminal"):
                return release_activation.block_refused_activation(
                    runtime, task, record, records, payload, attempt_id, exc, step=step
                )
        except HostError as exc:
            # A rejected merge must land the card in Blocked rather than escape the tick: an
            # escaping error leaves the verdict standing and every later tick retries the merge.
            return block_merge_path(runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                blocked_reason="implementation",
                action="merge-blocked",
                reason=f"merge failed: {scrub_host_output(str(exc))}",
                step=step,
                outcome="merge failed",
            )
        if isinstance(landing, MergeLanding):
            with tick_stage("release_landed"):
                watch = post_merge.open_watch(task, payload, landing, workspace=record.workspace)
                runtime.save_records(payload, records)
                release_merge = post_merge.release_merge_marker(watch)
    blocked = require_completion_evidence(runtime, task, record, records, payload, attempt_id, step=step)
    if blocked is not None:
        return blocked
    try:
        with tick_stage("release_teardown"):
            cleanup_receipt = runtime.host.teardown(record)
    except HostError as exc:
        # Cleanup is a provenance boundary, not best effort. A mismatch keeps the checkout and
        # prevents Done so the next tick cannot repeatedly run an already-failed release path.
        # The move carries no decision: an accept decision moves a card to Done and nowhere else,
        # so the board refuses a Blocked move that names it, and the card would stay in Assessment.
        return block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            blocked_reason="implementation",
            action="cleanup-provenance-blocked",
            reason=f"release cleanup refused: {scrub_host_output(str(exc))}",
            step=step,
            outcome="release cleanup refused",
        )
    with tick_stage("release_terminal"):
        attempt_accounting.terminal_effect(runtime,
            task,
            record,
            target="done",
            reason=move_reason,
            decision=decision,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "review-green", ref),
            terminal_state="done",
            disposition="accept",
            verdict=verdict,
            release_merge=release_merge,
        )
        records.pop(ref, None)
        runtime.save_records(payload, records)
    return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "done",
            **({"cleanup": cleanup_receipt} if isinstance(cleanup_receipt, dict) else {})}


def require_completion_evidence(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
) -> dict[str, Any] | None:
    """Completion evidence for kind: the one check between a release and Done.

    A code card's evidence is the merge `complete_green` has just made. A research or infra card
    is read fresh from the board, because its evidence is a marked comment written since the
    tick's snapshot; without it the card is Blocked, naming the missing marker, and not torn down.
    """
    if has_candidate(task):
        return None
    with tick_stage("release_evidence"):
        return _require_completion_evidence(runtime, task, record, records, payload, attempt_id, step=step)


def _require_completion_evidence(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
) -> dict[str, Any] | None:
    missing = missing_completion_evidence(runtime.reader.show(task["ref"]))
    if not missing:
        return None
    outcome = block_merge_path(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        blocked_reason="implementation",
        action="completion-evidence-missing",
        reason=(
            f"completion evidence missing: this {task.get('type')} card has no `[{missing}]` "
            "record, so it cannot be Done. The workspace is kept; see docs/PROTOCOLS.md, "
            '"Card kinds, live impact and the review choice".'
        ),
        step=step,
        outcome="completion evidence missing",
    )
    outcome["missing_evidence"] = missing
    return outcome
