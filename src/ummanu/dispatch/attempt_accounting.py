"""Attempt usage/outcome accounting and terminal lifecycle effects.

This package-owned boundary freezes durable outcome lineage, records phase usage,
publishes staged accounting obligations, and performs terminal board effects.
Routing/adoption/provider ingress remain collaborators of DispatcherRuntime.
"""

from __future__ import annotations

from typing import Any

from ummanu.board.models import Actor, EntityKind, EventKind
from ummanu.board.outcome_round_context import OutcomeRoundContext, OutcomeRoundPhase
from ummanu.board.roles import Role
from ummanu.board.terminal_taxonomy import (
    TerminalTaxonomy,
    TerminalTaxonomyValidationError,
    normalize_terminal_taxonomy,
)
from ummanu.dispatch.attempt_usage import (
    attempt_usage_data as _attempt_usage_data,
    attempt_usage_reason as _attempt_usage_reason,
    attribute_phase as _attribute_phase,
    causal_predecessor as _causal_predecessor,
    collect_usage as _collect_usage,
    provider_usage_source as _provider_usage_source,
)
from ummanu.dispatch.helpers import _round_report_ids
from ummanu.dispatch.state import DispatcherRecord, OutcomeTerminalPath
from ummanu.dispatch.state import attempt_request_id as _attempt_request_id
from ummanu.routing_journal import MODEL_UNKNOWN, REVIEWER, WORKER, HeadRun
from ummanu.routing_journal import routing_head_snapshot_from_launch as _routing_head_snapshot_from_launch
from ummanu.tasks import TaskError, specification_revision


def _usage_fallback_snapshot(
    journal_role: str,
    record: DispatcherRecord,
    lifecycle_run: dict[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    """A minimal routing-shaped snapshot for a phase whose launch record was lost.

    Everything here comes from the head's own attested run, never from the registry as it reads
    now: the model is recorded as unresolved rather than as a value some later edit supplied.
    """
    spec = lifecycle_run.get("spec") if isinstance(lifecycle_run, dict) else None
    spec = spec if isinstance(spec, dict) else {}
    return HeadRun(
        role=journal_role,
        head=record.head if role == Role.WORKER.value else record.review_head,
        adapter=str(spec.get("adapter") or ""),
        model=str(spec.get("model") or ""),
        model_source=MODEL_UNKNOWN,
    ).to_json()


def _pending_attempt_usage(runtime: Any) -> list[str]:
    """Card refs whose canonical usage occurrence still awaits export publication."""
    canon = runtime.writer.board_host.canon
    if canon is None:
        return []
    return [
        occurrence.event.ref for occurrence in canon.attempt_usage_occurrences() if occurrence.pending
    ]


def _attempt_outcome_obligation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    *,
    terminal_state: str,
    disposition: str,
    verdict: str = "missing",
    decision: str = "",
    terminal_path: OutcomeTerminalPath,
    taxonomy: TerminalTaxonomy,
) -> dict[str, Any] | None:
    """Freeze the forward lineage before its lifecycle effect is issued.

    The finisher and recovery path consume this exact object only.  They
    never reopen a card, walk comments, or search the journal for a newer
    source after the effect has happened.
    """
    reference = str(task.get("ref") or "")
    if not record.attempt_id or record.attempt_round < 1:
        return None
    context = _outcome_round_context(runtime, reference, record)
    worker_context = context.get("worker")
    attempt_id = (worker_context.attempt_id if worker_context is not None else record.attempt_id) or ""
    attempt = worker_context.attempt if worker_context is not None else record.attempt_round
    generation = (
        worker_context.report_generation if worker_context is not None else record.report_generation
    )
    if not attempt_id or attempt < 1 or generation < 1:
        return None
    reviewed = "review" in context or bool(record.review_run)
    report_context = context.get("report")
    revision = (
        report_context.specification_revision
        if report_context is not None
        else worker_context.specification_revision
        if worker_context is not None
        else None
    )
    # Select requiredness before source lookup. The dispatcher persists
    # this typed path when it accepts the report; it never consults the
    # handoff being validated, so a missing handoff remains incomplete.
    report_relevant = (
        terminal_path is OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT
        or verdict in {"green", "red", "blocked"}
        or bool(decision)
    )
    verdict_relevant = reviewed and verdict in {"green", "red"}
    decision_relevant = bool(decision)
    source, diagnostic = _outcome_lineage_sources(runtime, 
        reference,
        revision=revision,
        context=context,
        report_required=report_relevant,
        verdict_required=verdict_relevant,
        decision_required=decision_relevant,
    )
    completeness: dict[str, str] = {}
    for role, ledger_role in (("worker", "worker"), ("review", "reviewer")):
        usage = _outcome_usage_source(runtime, reference, attempt_id, attempt, generation, ledger_role)
        if usage is None:
            completeness[role] = "missing"
            continue
        source[f"{role}_usage"] = usage.event_id
        completeness[role] = "collected" if usage.data.get("outcome") == "collected" else "degraded"
    # Requiredness is selected from the path before source resolution. A
    # null source is evidence of incomplete lineage, never permission to
    # redefine the path as one that did not consume it. Verdict and
    # decision paths are necessarily report-derived.
    required = {
        "specification_revision": report_relevant,
        "report": report_relevant,
        "verdict": verdict_relevant,
        "decision": decision_relevant,
        "effect": True,
        "worker_usage": completeness["worker"] in {"collected", "degraded"},
        "review_usage": completeness["review"] in {"collected", "degraded"},
    }
    return {
        "version": 2,
        "attempt_id": attempt_id,
        "attempt": attempt,
        "report_generation": generation,
        "sprint_ref": task.get("sprint") or None,
        "specification_revision": revision or None,
        "terminal_state": terminal_state,
        "verdict": verdict,
        "disposition": taxonomy.disposition,
        "blocked_reason": taxonomy.blocked_reason,
        "source_event_ids": source,
        "usage_completeness": completeness,
        "lineage_required": required,
        **({"lineage_diagnostic": diagnostic} if diagnostic else {}),
    }


def _outcome_lineage_sources(
    runtime: Any,
    reference: str,
    *,
    revision: str | None,
    context: dict[str, OutcomeRoundContext],
    report_required: bool,
    verdict_required: bool,
    decision_required: bool,
) -> tuple[dict[str, str | None], str]:
    """Read only the already-durable exact source handoff.

    Source handlers recorded the event ids before they selected a terminal
    effect.  This method deliberately does not search marker history or
    reconstruct a request id, so it remains valid after dispatcher adoption.
    """
    source: dict[str, str | None] = {
        "report": None,
        "verdict": None,
        "decision": None,
        "effect": None,
        "worker_usage": None,
        "review_usage": None,
    }
    canon = runtime.writer.board_host.canon
    events = {event.event_id: event for event in canon.events(ref=reference)} if canon is not None else {}

    def one(name: str, phase: str, kind: str, marker: str) -> str:
        if canon is None:
            return f"attempt_outcome_lineage_missing_{name}"
        handoff = context.get(phase)
        if handoff is None or not handoff.source_event_id:
            return f"attempt_outcome_lineage_missing_{name}"
        event = events.get(handoff.source_event_id)
        if event is None:
            return f"attempt_outcome_lineage_dangling_{name}"
        data = event.data
        if event.kind.value != kind or event.ref != reference or data.get("marker") != marker:
            return f"attempt_outcome_lineage_incompatible_{name}"
        if "specification_revision" not in data:
            return f"attempt_outcome_lineage_legacy_{name}"
        if data.get("specification_revision") != revision:
            return f"attempt_outcome_lineage_incompatible_{name}"
        if phase == "decision" and data.get("assessment_visit") != handoff.assessment_visit:
            return f"attempt_outcome_lineage_incompatible_{name}"
        source[name] = event.event_id
        return ""

    report_context = context.get("report")
    verdict_context = context.get("verdict")
    decision_context = context.get("decision")
    diagnostics = [
        one(
            "report", "report", "card.reported",
            report_context.marker if report_context is not None else "",
        )
        if report_required else "",
        one(
            "verdict", "verdict", "card.verdict",
            verdict_context.marker if verdict_context is not None else "",
        )
        if verdict_required else "",
        one(
            "decision", "decision", "card.decided",
            decision_context.marker if decision_context is not None else "",
        )
        if decision_required else "",
    ]
    return source, next((diagnostic for diagnostic in diagnostics if diagnostic), "")


def _outcome_round_context_request_id(runtime: Any, record: DispatcherRecord, reference: str, phase: str) -> str:
    return _attempt_request_id(
        record.attempt_id, f"outcome-round-context-{phase}", reference, str(record.report_generation)
    )


def persist_outcome_round_context(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    *,
    phase: str,
    assessment_visit: str = "",
    request_ids: set[str] | None = None,
    source_event_id: str = "",
    marker: str = "",
    source_revision: str | None = None,
    freeze_source_revision: bool = False,
) -> None:
    """Write a stable forward round handoff at a launch or source boundary.

    This method is intentionally never called from ``terminal_effect``.
    The worker launch creates the stable identity; review and source records
    carry that identity forward without consulting mutable dispatcher state.
    """
    reference = str(task.get("ref") or "")
    if not reference or not record.attempt_id or record.attempt_round < 1 or record.report_generation < 1:
        return
    existing_context = _outcome_round_context(runtime, reference, record)
    worker = existing_context.get("worker")
    round_id = worker.round_id if worker is not None else ""
    if phase == "worker":
        context_request = _outcome_round_context_request_id(runtime, record, reference, phase)
        round_id = context_request
    elif not round_id:
        # A pre-v2 or unavailable launch handoff cannot be repaired from a
        # terminal path.  Its terminal outcome is honestly incomplete.
        return
    else:
        context_request = _attempt_request_id(round_id, f"outcome-round-context-{phase}", reference)
    if runtime.audit.committed_event(context_request) is not None:
        return
    if request_ids is None:
        if phase == "worker":
            request_ids = _round_report_ids(
                record.workspace, record.attempt_id, reference, record.report_generation
            )
        elif phase == "review":
            request_ids = {
                _attempt_request_id(
                    record.attempt_id, f"review-{kind}", reference, str(record.review_baseline)
                )
                for kind in ("green", "red")
            }
        else:
            return
    revision = source_revision if phase != "worker" else None
    if phase != "worker" and not freeze_source_revision and source_revision is None:
        revision = worker.specification_revision if worker is not None else None
    if phase == "worker":
        revision = (
            specification_revision(runtime.audit.events(reference), str(task.get("description") or ""))
            or None
        )
    try:
        context = OutcomeRoundContext(
            version=2,
            phase=OutcomeRoundPhase(phase),
            round_id=round_id,
            attempt_id=(
                worker.attempt_id
                if phase != "worker" and worker is not None
                else record.attempt_id
            ),
            attempt=(
                worker.attempt
                if phase != "worker" and worker is not None
                else record.attempt_round
            ),
            report_generation=(
                worker.report_generation
                if phase != "worker" and worker is not None
                else record.report_generation
            ),
            request_ids=tuple(sorted(request_ids)),
            assessment_visit=assessment_visit,
            source_event_id=source_event_id,
            specification_revision=revision,
            marker=marker,
        )
    except ValueError as exc:
        raise TaskError("validation", str(exc), 2) from None
    runtime.writer.outcome_round_context(
        role="dispatcher", actor=runtime.owner, reference=reference,
        request_id=context_request, data=context,
    )


def capture_outcome_source(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    *,
    phase: str,
    kind: str,
    marker: str,
    assessment_visit: str = "",
) -> None:
    """Attach the exact marker id as soon as this dispatcher consumes it.

    A journal outage here must not veto the later lifecycle effect.  In
    that case there is no invented identity: the effect carries the named
    missing-source diagnostic and projection marks the required lineage
    incomplete.
    """
    reference = str(task.get("ref") or "")
    context = _outcome_round_context(runtime, reference, record)
    owner = context.get("worker" if phase == "report" else "review")
    if owner is None:
        return
    request_ids = owner.request_ids
    canon = runtime.writer.board_host.canon
    if canon is None:
        return
    matches = [
        event
        for request_id in request_ids
        if (event := canon.committed(str(request_id))) is not None
        and event.kind.value == kind
        and event.ref == reference
        and event.data.get("marker") == marker
    ]
    if len(matches) != 1:
        return
    try:
        persist_outcome_round_context(runtime, 
            task,
            record,
            phase=phase,
            assessment_visit=assessment_visit,
            request_ids={str(matches[0].event_id)},
            source_event_id=matches[0].event_id,
            marker=marker,
            source_revision=matches[0].data.get("specification_revision"),
            freeze_source_revision=True,
        )
    except (OSError, TaskError, ValueError):
        return


def _outcome_round_context(
    runtime: Any, reference: str, record: DispatcherRecord
) -> dict[str, OutcomeRoundContext]:
    """Find one unsettled durable handoff without re-estimating its identity.

    The fast path keeps ordinary dispatch cheap. Adoption can lose the
    process-local attempt id and report generation, so its fallback uses
    only durable handoffs and excludes rounds already sealed by a lifecycle
    effect. It never uses card comments, workspace text, event order or
    request-id grammar to choose a source.
    """
    payloads: list[OutcomeRoundContext] = []
    for event in runtime.audit.events(reference, kind="outcome_round_context"):
        payload = (
            event.get("data")
            if event.get("record_type") == "board.protocol_event"
            else event.get("payload")
        )
        if not isinstance(payload, dict) or payload.get("version") != 2:
            continue
        try:
            payloads.append(OutcomeRoundContext.from_data(payload))
        except ValueError:
            continue
    workers = [payload for payload in payloads if payload.phase is OutcomeRoundPhase.WORKER]
    exact = [
        payload for payload in workers
        if payload.attempt_id == record.attempt_id
        and payload.attempt == record.attempt_round
        and payload.report_generation == record.report_generation
    ]
    if len(exact) == 1:
        worker = exact[0]
    else:
        sealed = {
            (data.get("attempt_id"), data.get("attempt"), data.get("report_generation"))
            for event in runtime.writer.board_host.canon.events(ref=reference)
            if isinstance((data := event.data.get("attempt_outcome_owed")), dict)
        }
        unsettled = [
            payload for payload in workers
            if (payload.attempt_id, payload.attempt, payload.report_generation) not in sealed
        ]
        if len(unsettled) != 1:
            return {}
        worker = unsettled[0]
    context = {"worker": worker}
    for payload in payloads:
        if payload.phase is not OutcomeRoundPhase.WORKER and payload.round_id == worker.round_id:
            context[payload.phase.value] = payload
    return context


def _outcome_usage_source(
    runtime: Any, reference: str, attempt_id: str, attempt: int, generation: int, role: str
) -> Any | None:
    canon = runtime.writer.board_host.canon
    if canon is None:
        return None
    matches = [
        occurrence.event
        for occurrence in canon.attempt_usage_occurrences(ref=reference)
        if occurrence.event.data.get("attempt_id") == attempt_id
        and occurrence.event.data.get("attempt") == attempt
        and occurrence.event.data.get("report_generation") == generation
        and occurrence.event.data.get("role") == role
    ]
    return matches[0] if len(matches) == 1 else None


def _finish_attempt_outcome(runtime: Any, obligation: dict[str, Any], effect_event_id: str) -> dict[str, Any]:
    """Stage/append one owed row, reporting degradation without lifecycle work."""
    try:
        canon = runtime.writer.board_host.canon
        if canon is None:
            raise RuntimeError("board event canon is unavailable")
        reference = str(obligation.get("card_ref") or "")
        # Transition obligations inherit their Card subject; immediate
        # callers add it below so recovery never consults live card state.
        if not reference:
            raise ValueError("attempt outcome obligation has no card ref")
        source = dict(obligation["source_event_ids"])
        source["effect"] = effect_event_id
        required = obligation.get("lineage_required")
        if not isinstance(required, dict):
            raise ValueError(  # noqa: TRY004 - corrupt persisted state, not caller type input.
                "attempt_outcome_lineage_requiredness_missing"
            )
        data = {
            **{
                key: value
                for key, value in obligation.items()
                if key not in {"card_ref", "lineage_diagnostic", "source_event_ids"}
            },
            "source_event_ids": source,
        }
        return runtime.writer.attempt_outcome(
            role="dispatcher",
            actor=runtime.owner,
            reference=reference,
            data=data,
            reason="confirmed terminal lifecycle effect",
            request_id=_attempt_request_id(
                str(obligation["attempt_id"]),
                "attempt-outcome",
                reference,
                str(obligation["report_generation"]),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - analytics never gates a lifecycle effect
        return {
            "status": "degraded",
            "step": "attempt-outcome",
            "action": "attempt-outcome-owed",
            "reason": f"outcome remains owed: {type(exc).__name__}: {exc}",
        }


def terminal_effect(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    *,
    target: str,
    reason: str,
    request_id: str,
    terminal_state: str,
    disposition: str,
    verdict: str = "missing",
    blocked_reason: str | None = None,
    decision: str = "",
    release_merge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The lifecycle-owned terminal effect and its non-blocking finisher.

    `release_merge` marks a Done whose release landed a commit on the base; the observer is then
    woken on that commit's post-merge CI result instead of on this move.
    """
    taxonomy: TerminalTaxonomy | None = None
    try:
        taxonomy = normalize_terminal_taxonomy(disposition=disposition, blocked_reason=blocked_reason)
    except TerminalTaxonomyValidationError:
        # The effect is authoritative. A malformed observational value is
        # rejected at its typed boundary but cannot delay or retract it.
        pass
    obligation = (
        _attempt_outcome_obligation(runtime, 
            task,
            record,
            terminal_state=terminal_state,
            disposition=disposition,
            verdict=verdict,
            decision=decision,
            terminal_path=record.outcome_terminal_path,
            taxonomy=taxonomy,
        )
        if taxonomy is not None
        else None
    )
    if obligation is not None:
        obligation = {"card_ref": task["ref"], **obligation}
    if decision in {"release", "reslice"}:
        # These paths have no frozen continuation. Their native transition intent owns
        # the reason once staged, including full-body reasons written by released code.
        # The writer/host still checks the complete operation identity and data below.
        canon = runtime.writer.board_host.canon
        existing = canon.event(request_id) if canon is not None else None
        if (
            existing is not None
            and existing.entity_kind is EntityKind.CARD
            and existing.kind is (
                EventKind.CARD_RELEASED if decision == "release" else EventKind.CARD_BLOCKED
            )
            and existing.ref == task["ref"]
            and existing.actor == Actor("dispatcher", runtime.owner)
            and existing.target_state == target
        ):
            reason = existing.reason
    effect = runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=task["ref"],
        target=target,
        reason=reason,
        decision=decision,
        request_id=request_id,
        outcome_owed=obligation,
        terminal_taxonomy=taxonomy.to_record() if taxonomy is not None else None,
        **({"release_merge": release_merge} if release_merge is not None else {}),
    )
    effect_obligation = effect.get("outcome_owed") if isinstance(effect, dict) else None
    if isinstance(effect_obligation, dict):
        _finish_attempt_outcome(runtime, effect_obligation, str(effect["event_id"]))
    elif obligation is not None:
        _finish_attempt_outcome(runtime, obligation, str(effect["event_id"]))
    return effect


def publish_pending_attempt_outcomes(runtime: Any) -> list[dict[str, Any]]:
    """Recover committed terminal-effect obligations, then exact staged rows.

    This is journal-only and fail-open: malformed analytics history is a
    diagnostic, never a reason to abort the production tick.
    """
    try:
        canon = runtime.writer.board_host.canon
        if canon is None:
            return []
        outcomes: list[dict[str, Any]] = []
        for effect in canon.attempt_outcome_effects():
            obligation = effect.data.get("attempt_outcome_owed")
            if not isinstance(obligation, dict):
                continue
            outcome = _finish_attempt_outcome(runtime, obligation, effect.event_id)
            if outcome.get("status") == "degraded":
                outcomes.append(outcome)
        runtime.writer.finish_attempt_outcomes(role="dispatcher")
        return outcomes
    except Exception as exc:  # noqa: BLE001 - no analytics reader may gate a tick
        return [
            {
                "status": "degraded",
                "step": "attempt-outcome-recovery",
                "action": "attempt-outcome-pending-unreadable",
                "reason": f"outcome recovery remains owed: {type(exc).__name__}: {exc}",
            }
        ]


def publish_pending_attempt_usage(runtime: Any) -> list[dict[str, Any]]:
    """Publish every staged `attempt.usage` occurrence the installation still owes.

    The single enforcement site of the durability order. A phase is accounted for as soon as its
    occurrence is staged, and the card is then free to advance — including into Blocked or Done,
    which no later step of the tick looks at again. So the obligation is finished from the
    pending set itself: no dispatcher record, no board lookup, no card state, nothing a terminal
    card has already given up. It runs before observer fencing, before the active cycle is read
    and before any phase boundary is, because every one of those reads a journal these records
    belong in.

    The canonical committed-plus-pending projection is also the source of recovery obligations;
    recovery does not interpret the pending directory independently. Publishing the exact staged
    record is the whole of it. A failure publishes nothing in its
    place: the record stays pending, stays exact, and is eligible again on every later permitted
    tick, whatever state its card has reached by then.
    """
    try:
        owed = _pending_attempt_usage(runtime)
    except Exception as exc:  # noqa: BLE001 - an unreadable pending set is reported, not raised
        return [
            {
                "status": "degraded",
                "step": "attempt-usage-recovery",
                "action": "attempt-usage-pending-unreadable",
                "reason": (
                    "staged usage occurrences could not be read, so any obligation among them "
                    f"is still owed: {type(exc).__name__}: {exc}"
                ),
            }
        ]
    if not owed:
        return []
    failure = ""
    try:
        runtime.writer.finish_attempt_usage(role="dispatcher")
    except Exception as exc:  # noqa: BLE001 - the obligation outlives its own recovery failing
        failure = f"{type(exc).__name__}: {exc}"
    try:
        remaining = _pending_attempt_usage(runtime)
    except Exception as exc:  # noqa: BLE001 - unknown is owed, not settled
        remaining = list(owed)
        failure = failure or f"{type(exc).__name__}: {exc}"
    outcome: dict[str, Any] = {
        "status": "degraded" if remaining else "ok",
        "step": "attempt-usage-recovery",
        "action": "attempt-usage-still-pending" if remaining else "attempt-usage-published",
        "published": max(0, len(owed) - len(remaining)),
        "pending": len(remaining),
        "refs": sorted({ref for ref in owed if ref}),
    }
    if remaining:
        outcome["pending_refs"] = sorted({ref for ref in remaining if ref})
        outcome["reason"] = (
            f"{len(remaining)} staged usage occurrence(s) could not be published and stay owed"
            + (f": {failure}" if failure else "")
        )
    return [outcome]


def record_attempt_usage(runtime: Any, ref: str, record: DispatcherRecord, *, role: str, attempt_id: str) -> None:
    """Persist what the phase that just finished cost, before the card can advance past it.

    Called on the acceptance paths themselves — a terminal worker report, a reviewer verdict —
    because that is the last point at which the exact run that did the work is still on the
    record with its bound provider session.

    Two failures, and they are not the same failure. Reading the provider never decides
    anything: an unbound session, an unreadable journal and malformed records are named degraded
    outcomes inside the occurrence, and the report or verdict is accepted exactly as it would
    have been. Failing to make the occurrence durable is an audit failure, and this method
    refuses to swallow it: the control event and the transition may not outrun the account of
    the phase they close. A staged-but-unappended obligation is durable enough to advance past,
    because the canonical occurrence projection makes it authoritative immediately and the global
    publication reconciler later appends that exact record.
    """
    try:
        _write_attempt_usage(runtime, ref, record, role=role, attempt_id=attempt_id)
    except TaskError as exc:
        if exc.code != "audit_pending":
            raise
        # The exact occurrence is staged in the append-only audit. The phase is accounted for;
        # only its publication is outstanding, and a later tick finishes it.
        return


def _write_attempt_usage(runtime: Any, ref: str, record: DispatcherRecord, *, role: str, attempt_id: str) -> None:
    phase = "worker" if role == Role.WORKER.value else "review"
    journal_role = WORKER if role == Role.WORKER.value else REVIEWER
    # A round is what binds the occurrence to a phase. Every accepted terminal report has one;
    # a record rebuilt without one still owes the phase an account, so the first round answers
    # for it rather than the occurrence being dropped.
    attempt = max(record.attempt_round or runtime._journal_round(ref), 1)
    generation = max(record.report_generation, 1)
    snapshot = dict(record.worker_run if role == Role.WORKER.value else record.review_run)
    lifecycle = dict(record.worker_head_run if role == Role.WORKER.value else record.review_head_run)
    if not snapshot:
        # A recovered record can hold the head's own run without the routing snapshot of its
        # configuration. The head's attested launch spec answers for the adapter; re-reading
        # today's `heads.toml` for a head launched hours ago would not.
        snapshot = _usage_fallback_snapshot(journal_role, record, lifecycle, role=role)
    try:
        run = _routing_head_snapshot_from_launch(snapshot, lifecycle_run=lifecycle)
    except ValueError:
        # An incomplete launch attestation is not a reason to drop the occurrence: the routing
        # snapshot still reports its adapter, model and whatever session identity it already held.
        run = HeadRun.from_json(snapshot)
    # One order, for every provider and every lifecycle path. Projection integrity and causal
    # identity first, because neither depends on what a provider journal says and a phase slot
    # owned by another attempt may not be written whatever that journal would have said.
    try:
        occurrences = runtime.writer.board_host.canon.attempt_usage_occurrences(ref=ref)
        predecessor = _causal_predecessor(
            occurrences,
            adapter=run.adapter,
            session_id=run.session_id or "",
            attempt=attempt,
            attempt_id=record.attempt_id or attempt_id,
            report_generation=generation,
            phase=phase,
            role=journal_role,
        )
    except (OSError, TypeError, ValueError, TaskError) as exc:
        raise TaskError(
            "audit_unavailable", f"attempt usage projection is unreadable: {exc}", 4
        ) from None
    # The provider read second: a whole-session total, with no arithmetic of its own.
    collection = _collect_usage(
        adapter=run.adapter,
        source=_provider_usage_source(lifecycle, adapter=run.adapter),
    )
    # Attribution and cross-account validation third, in the one place that does either.
    try:
        collection = _attribute_phase(collection, predecessor)
    except (TypeError, ValueError) as exc:
        raise TaskError(
            "audit_unavailable", f"attempt usage projection is unreadable: {exc}", 4
        ) from None
    data = _attempt_usage_data(
        attempt=attempt,
        attempt_id=record.attempt_id or attempt_id,
        phase=phase,
        role=journal_role,
        report_generation=generation,
        head=run.head,
        adapter=run.adapter or "unknown",
        model=run.model,
        model_source=run.model_source,
        session_id=run.session_id,
        session_id_reason=(
            run.session_id_reason
            or ("" if run.session_id else "no provider session identity was recorded for this run")
        ),
        launch_id=run.launch_id,
        collection=collection,
    )
    runtime.writer.attempt_usage(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        data=data,
        reason=_attempt_usage_reason(data),
        # One occurrence per completed phase: the round it closed names it, so a re-entered
        # acceptance and a replayed request commit the same event rather than a second one.
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"attempt-usage-{phase}",
            ref,
            f"{attempt}-{generation}",
        ),
    )
