"""Ready-card admission and claim boundary for the production dispatcher.

This module owns the decision-making that happens before a worker launch: head selection,
sprint admission, broad-check and Git-access preflights, the durable board claim, and the
post-claim handoff. Worker bring-up/recovery is owned by `dispatch.worker_launch`; the typed
ClaimHandoff is the seam between the committed claim and that lifecycle.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any

from ummanu.board.audit_contract import card_transition_of
from ummanu.board.blocked_rework import blocked_rework
from ummanu.board.completion_evidence import has_candidate, is_po_executed, is_wait
from ummanu.broad_check import admitted_check
from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.assessment_decision import rework_instruction
from ummanu.dispatch.helpers import _REPORT_ACTIONS, _round_report_ids, _worker_id, scrub_host_output
from ummanu.dispatch.host import _blocked_actions_and_their_infrastructure_twins
from ummanu.dispatch.host_runtime.catalog import live_root_project_refusal
from ummanu.dispatch.launch import (
    CAUSE_ADMISSION_REFUSED,
    FAILURE_CLASS_ADMISSION,
    STAGE_CLAIM,
    WORKER_ROLE,
    BringUpFailure,
    bring_up_blocked_action as _bring_up_blocked_action,
    bring_up_terminal_reason as _bring_up_terminal_reason,
    classify_bring_up_failure as _classify_bring_up_failure,
)
from ummanu.dispatch.po_cards import claim_po_card
from ummanu.dispatch.state import (
    CLAIM_SKIP_FAILOVER_COLLAPSE,
    CLAIM_SKIP_GIT_ACCESS_UNREACHABLE,
    CLAIM_SKIP_OLD_HEAD_STOP_UNCONFIRMED,
    CLAIM_SKIP_RESOURCE_NOT_READY,
    CLAIM_SKIP_SPRINT_RESERVATION_UNVERIFIABLE,
    DispatcherRecord,
    OutcomeTerminalPath,
    PersistedHeadRun,
    attempt_request_id as _attempt_request_id,
    new_attempt_id as _new_attempt_id,
    record_attempt as _record_attempt,
    request_token,
)
from ummanu.dispatch.types import STOPPED_BY_REPLACEMENT, HostError
from ummanu.dispatch.wait_cards import claim_wait_card
from ummanu.dispatch.worker_launch import launch_worker_after_claim
from ummanu.head_health import HeadChoice, resolve_head_chain
from ummanu.infra.github_credential import ProjectGitAccess
from ummanu.projects.contract import (
    CONTRACT_FIT,
    CONTRACT_REFUSED,
    CONTRACT_UNDECIDABLE,
    UNDECIDABLE_NO_REGISTERED_PROJECT,
    UNDECIDABLE_PROJECT_UNAVAILABLE,
    ContractUnusable,
    ContractVerdict,
)
from ummanu.tasks import SprintReservationUnverifiable, _event_payload, assessment_resolution

#: The action token of a card refused at admission because an open sprint reserves its project.
SPRINT_RESERVATION_BLOCKED_ACTION = "sprint-reservation-blocked"
#: A `code` card outside every sprint, on a project an open sprint reserves.
SPRINT_RESERVATION_RESERVED = "sprint_reserved"
#: A `code` card outside every sprint, whose project's reservations could not be verified.
SPRINT_RESERVATION_UNVERIFIABLE = "sprint_reservation_unverifiable"
#: The action token of a card refused at admission because its project's repository is the live root.
LIVE_ROOT_PROJECT_BLOCKED_ACTION = "live-root-project-blocked"

#: The stages a claimed card's last attempt can have stopped at.
STAGE_ADMISSION = "admission"
STAGE_WORKER = "worker"
STAGE_GATE = "gate"
STAGE_REVIEW = "review"
#: The record state of a claim held on its round's candidate, before its move into Validate.
STAGE_RESUME_STATE = "stage_resume"
#: Blocked actions written before the attempt had a checkout, so the next claim creates one.
_NO_CHECKOUT_ACTIONS = _blocked_actions_and_their_infrastructure_twins(
    SPRINT_RESERVATION_BLOCKED_ACTION,
    LIVE_ROOT_PROJECT_BLOCKED_ACTION,
    "contract-preflight-blocked",
    "git-access-preflight-blocked",
    "codex-fanout-blocked",
)
#: Blocked actions out of Validate or Assessment that judged the candidate itself, so only new
#: work continues the card: a red review stop, a ledgered candidate, a reslice decision.
_WORKER_ACTIONS = (
    "red-review-ceiling",
    "red-review-repeated-blockers",
    "rejected-candidate-blocked",
    "assessment-reslice",
)
#: Action families the gate and the merge path block with out of Validate or Assessment, each
#: matched with any phase prefix (`merge-gate-blocked`, `review-freeze-infrastructure-...`).
_GATE_ACTIONS = (
    "gate-blocked",
    "gate-transport-blocked",
    "gate-receipt-blocked",
    "gate-topology-blocked",
    "gate-result-blocked",
    "gate-pending-stall",
    "infrastructure-reruns-exhausted",
    "infrastructure-rerun-blocked",
    "merge-blocked",
    "release-drift-blocked",
    "release-failed-blocked",
    "release-red-blocked",
    "cleanup-provenance-blocked",
    "completion-evidence-missing",
    "research-report-transfer-refused",
)


@dataclass(frozen=True)
class SprintAdmissionRefusal:
    """Why a card linked to no sprint is not admitted on its project."""

    code: str
    project: str
    sprints: tuple[str, ...]
    detail: str

    def evidence(self) -> dict[str, Any]:
        return {
            "refusal": self.code,
            "project": self.project,
            "sprints": list(self.sprints),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ResumePoint:
    """The round a dispatcher Blocked move interrupted, as the next claim of the card continues it.

    `generation` is the highest one the card's worker reports were filed under; the claim opens the
    next. `round_report` is the interrupted round's admitted `report:done`, with its generation and
    candidate SHA, when that report is the round's last. The default is a card with no interrupted round.
    """

    require_existing_workspace: bool = False
    stage: str = ""
    decision_id: str = ""
    decision: str = ""
    protocol_prerequisites: tuple[str, ...] = ()
    generation: int = 0
    round_report: str = ""
    round_generation: int = 0
    candidate_sha: str = ""


@dataclass(frozen=True)
class ClaimHandoff:
    """A durable claim that is ready for the worker bring-up lifecycle."""

    claimed: dict[str, Any]
    record: DispatcherRecord
    require_existing_workspace: bool = False


def _head_fallback(runtime: Any, head: str) -> list[str] | None:
    """`head`'s fallback chain, or None when the registry does not describe it at all.

    None is not an empty chain. The existence question is answered here as one lookup and never
    put to a readiness probe, whose `HostError` for an undescribed head would escape the walk and
    take the tick's Ready pass with it.
    """
    try:
        return runtime.catalog.head_fallback(head)
    except HostError:
        return None

def resolve_head(runtime: Any, preferred: str) -> HeadChoice:
    """The head to actually launch for `preferred`, walking the canon's fallback chain.

    Substitution follows only the chain the canon writes down, and only at claim, where the
    decision is recorded on the card. When nothing in the chain is launchable the answer is an
    empty head: the caller claim-skips and the card waits in Ready.
    """
    return resolve_head_chain(
        preferred,
        runtime.head_readiness,
        lambda head: _head_fallback(runtime, head),
    )


def _failover_collapse(runtime: Any, worker: HeadChoice, review: HeadChoice) -> dict[str, Any] | None:
    """The refusal when a failover would hand both roles to one head, else None.

    Only a failover can collapse the pair here: two roles pointed at one head by the canon itself
    is an installation's own decision and is not overruled.
    """
    if not review.resolved or review.head != worker.head:
        return None
    if not (worker.substituted or review.substituted):
        return None
    return {
        "status": "skipped",
        "step": "head-preflight",
        "action": CLAIM_SKIP_FAILOVER_COLLAPSE,
        "head": worker.head,
        "review_head": review.head,
        "readiness": worker.readiness.to_json(),
        "reason": (
            f"failover would run worker and reviewer on the same head {worker.head}: "
            f"worker {worker.reason}; reviewer {review.reason}"
        ),
        "failover": {"worker": worker.to_json(), "review": review.to_json()},
    }

def _comment_head_failover(
    runtime: Any, ref: str, attempt_id: str, worker: HeadChoice, review: HeadChoice
) -> None:
    """Write the substitution onto the card, once per claim, or do nothing."""
    lines = [
        f"{role} head {choice.head} instead of {choice.preferred}: {choice.reason}"
        for role, choice in (("Worker", worker), ("Reviewer", review))
        if choice.substituted
    ]
    if not lines:
        return
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body="Head failover at claim. " + " ".join(lines),
        request_id=_attempt_request_id(attempt_id, "head-failover-comment", ref),
    )

def _broad_check_contract_verdict(runtime: Any, task: dict[str, Any]) -> ContractVerdict:
    """This card's broad-check contract, as one of the three named states (secretary-1458).

    Offline and cheap: the binding and the adapter beside it, read before anything is claimed.
    Every way of not getting an answer is a named state rather than a fall-through, because a
    fall-through is what "nothing came back, so the card may go" was made of. A card that names
    no registered project, and a project this installation cannot look up at all, are open
    questions about the registry — the paths that need the binding fail on it in their own
    words — and they are returned as such, not as approval.
    """
    project = str(task.get("project") or "")
    if not project:
        return ContractVerdict.as_undecidable(
            UNDECIDABLE_NO_REGISTERED_PROJECT,
            "",
            f"card {task.get('ref')!r} names no registered project, so it has no adapter and "
            "no broad-check contract to judge",
        )
    try:
        return runtime.catalog.broad_check_verdict(project)
    except HostError as exc:
        return ContractVerdict.as_undecidable(
            UNDECIDABLE_PROJECT_UNAVAILABLE,
            "",
            f"registered project {project!r} could not be read: {exc}",
        )

def _contract_preflight_decision(
    runtime: Any,
    task: dict[str, Any],
    verdict: ContractVerdict,
    *,
    attempt_id: str,
    head: str,
    review_head: str,
) -> tuple[BringUpFailure, str, ContractUnusable] | None:
    """What the verdict buys this card: the outcome that stops it, or None to issue it.

    Exhaustive over the three states by name, with no default branch that lets an unrecognised
    answer through as permission. Which state buys what is `projects.contract`'s decision and
    is only carried out here:

    * `refused` stops the card before it is issued — that is the guarantee the card exists for;
    * `undecidable` issues it, because the open question is a documented compatibility promise
      (a relative interpreter is resolved from a workspace that does not exist yet) and the
      side that will hold that tree answers it there. It is a decision with a name, not the
      absence of one;
    * `fit` issues it, as always.
    """
    if verdict.state == CONTRACT_REFUSED and verdict.refusal is not None:
        failure, reason = _contract_preflight_outcome(runtime, 
            task,
            attempt_id=attempt_id,
            head=head,
            review_head=review_head,
            refusal=verdict.refusal,
        )
        return failure, reason, verdict.refusal
    if verdict.state in (CONTRACT_FIT, CONTRACT_UNDECIDABLE):
        return None
    raise HostError(f"unreadable broad-check contract verdict {verdict.state!r}")

def _contract_preflight_outcome(
    runtime: Any,
    task: dict[str, Any],
    *,
    attempt_id: str,
    head: str,
    review_head: str,
    refusal: ContractUnusable,
) -> tuple[BringUpFailure, str]:
    """The typed infrastructure outcome for a card nobody can broad-check, decided before claim.

    Pure: it turns the refusal the preflight already read off the registry into the class, the
    evidence and the card's Blocked reason, and touches neither the board, the host nor the
    filesystem. That is what lets the claim and the transition it is the door to stand next to
    each other with nothing that can fail in between.

    This is the same outcome a bring-up that produced no head carries, made by the same
    classifier and written with the same durable action token: an installation whose registry
    cannot supply a usable contract is a failure of the host, not a verdict about the card. The
    two properties the card must have follow from that token alone rather than from anything
    here — the sprint budget reads it and counts the block as uncharged, and a block is not a
    retry, so no new attempt is opened and nothing is scheduled to come back.
    """
    detail = (
        f"the broad-check contract of registered project {task.get('project')!r} cannot "
        f"attest this card: {refusal.detail()}"
    )
    failure = _unclaimed_preflight_failure(attempt_id=attempt_id, detail=detail)
    reason = (
        "the card was not given to a worker: this project's broad-check contract cannot "
        f"attest it, so no workspace and no head were created. {detail}\n{failure.clause()}"
    )
    return failure, reason

def _unclaimed_preflight_failure(*, attempt_id: str, detail: str) -> BringUpFailure:
    """A pre-claim refusal as the bring-up taxonomy names it: infrastructure, never retried."""
    return _classify_bring_up_failure(
        None,
        None,
        WORKER_ROLE,
        stage=STAGE_CLAIM,
        attempt_id=attempt_id,
        detail=detail,
    )

def _sprint_admission_refusal(runtime: Any, task: dict[str, Any]) -> SprintAdmissionRefusal | None:
    """Why a card outside every sprint may not run on its project now, or None to admit it.

    The single admission question about sprint reservations (secretary-1641). The write guard
    lets the PO cut a card of any kind outside a sprint; this is where that card's running is
    decided. A card linked to a sprint is that sprint's work and is not asked about. A card with
    no candidate (`research`, `infra`) touches no branch a sprint owns and is always admitted,
    so the board is not read for it. A `code` card is asked about through the same verified
    reservation index the write guard reads, and an index that cannot be verified refuses it
    rather than letting it race a sprint nobody could see.
    """
    if str(task.get("sprint") or "") or not has_candidate(task):
        return None
    project = str(task.get("project") or "")
    try:
        held = runtime.writer.open_sprints_reserving(project)
    except SprintReservationUnverifiable as exc:
        return SprintAdmissionRefusal(
            SPRINT_RESERVATION_UNVERIFIABLE,
            project,
            (exc.sprint_ref,) if exc.sprint_ref else (),
            f"the open sprints reserving project {project!r} could not be verified "
            f"({exc.cause.code}: {exc.cause.message}), so a code card outside a sprint is not "
            "admitted there until they can be",
        )
    if not held:
        return None
    sprints = ", ".join(held)
    return SprintAdmissionRefusal(
        SPRINT_RESERVATION_RESERVED,
        project,
        tuple(held),
        f"project {project!r} is reserved by open sprint {sprints}; this code card is linked "
        f"to no sprint, so it may run inside {sprints} (as a card of that sprint) or after "
        "that sprint closes, when it can be moved back to Ready",
    )

def _sprint_admission_blocked(
    runtime: Any,
    task: dict[str, Any],
    ref: str,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    *,
    attempt_id: str,
    head: str,
    review_head: str,
    refusal: SprintAdmissionRefusal,
) -> dict[str, Any]:
    """Write the sprint admission refusal decided before the claim, immediately after it."""
    failure = BringUpFailure(
        failure_class=FAILURE_CLASS_ADMISSION,
        cause=CAUSE_ADMISSION_REFUSED,
        stage=STAGE_CLAIM,
        role=WORKER_ROLE,
        attempt_id="",
        detail=refusal.detail,
    )
    reason = (
        "the card was not given to a worker: it was refused at admission, so no workspace and "
        f"no head were created. [sprint admission: refusal={refusal.code}, "
        f"project={refusal.project}, sprint={', '.join(refusal.sprints) or '(unknown)'}] "
        f"{refusal.detail}\n{failure.clause()}"
    )
    _write_claim_preflight_block(runtime, 
        task,
        ref,
        records,
        payload,
        attempt_id=attempt_id,
        action=SPRINT_RESERVATION_BLOCKED_ACTION,
        failure=failure,
        reason=reason,
    )
    return {
        "status": "blocked",
        "step": "sprint-reservation-refused",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "reason": "code card outside a sprint refused on a reserved project",
        "sprint_reservation": refusal.evidence(),
        **failure.outcome_fields(reason),
    }

def _live_root_project_blocked(
    runtime: Any,
    task: dict[str, Any],
    ref: str,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    *,
    attempt_id: str,
    refusal: str,
) -> dict[str, Any]:
    """Write the live-root refusal decided before the claim, immediately after it.

    The instance-repo landing is gone: a card whose project repository is the live root has no
    path to land on, so it is refused at admission, fail-closed, before any workspace exists.
    """
    failure = _unclaimed_preflight_failure(attempt_id=attempt_id, detail=refusal)
    reason = (
        "the card was not given to a worker: it was refused at admission, so no workspace and "
        f"no head were created. {refusal}\n{failure.clause()}"
    )
    _write_claim_preflight_block(runtime, 
        task,
        ref,
        records,
        payload,
        attempt_id=attempt_id,
        action=LIVE_ROOT_PROJECT_BLOCKED_ACTION,
        failure=failure,
        reason=reason,
    )
    return {
        "status": "blocked",
        "step": "live-root-project-refused",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "reason": "project repository is the live root",
        **failure.outcome_fields(reason),
    }

def _project_git_access(runtime: Any, task: dict[str, Any]) -> ProjectGitAccess:
    """The registered project's remote Git access, asked before anything is claimed."""
    try:
        return runtime.host.project_git_access(str(task.get("project") or ""))
    except HostError as exc:
        # The host could not even ask. That is silence about the credential, not a refusal.
        return ProjectGitAccess(
            "unreachable", "unknown", "none", "host", scrub_host_output(str(exc))[:240]
        )

def _git_access_preflight_outcome(
    runtime: Any,
    task: dict[str, Any],
    access: ProjectGitAccess,
    *,
    attempt_id: str,
    head: str,
    review_head: str,
) -> tuple[BringUpFailure, str]:
    """The typed outcome for a card whose project's remote Git access was refused by name.

    Pure, like `_contract_preflight_outcome`: it names the project, the refusal code, the
    transport and the fixed-vocabulary reason, and touches neither board nor host. A refused
    credential is a determinate host condition, so the card is blocked uncharged and no
    attempt is scheduled to come back; it is not the transport class the gate retries.
    """
    detail = (
        f"registered project {task.get('project')!r} refused remote Git access "
        f"(refusal={access.code}, transport={access.transport}): {access.reason}"
    )
    failure = _unclaimed_preflight_failure(attempt_id=attempt_id, detail=detail)
    reason = (
        "the card was not given to a worker: this project's remote Git access was refused "
        f"before the claim, so no workspace and no head were created. {detail}\n{failure.clause()}"
    )
    return failure, reason

def _git_access_preflight_blocked(
    runtime: Any,
    task: dict[str, Any],
    ref: str,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    *,
    attempt_id: str,
    access: ProjectGitAccess,
    failure: BringUpFailure,
    reason: str,
) -> dict[str, Any]:
    """Write the Git access refusal decided before the claim, immediately after it."""
    _write_claim_preflight_block(runtime, 
        task,
        ref,
        records,
        payload,
        attempt_id=attempt_id,
        action="git-access-preflight-blocked",
        failure=failure,
        reason=reason,
    )
    return {
        "status": "blocked",
        "step": "git-access-preflight",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "reason": "project Git access preflight refused",
        "git_access": {"project": str(task.get("project") or ""), **access.to_json()},
        **failure.outcome_fields(reason),
    }

def _contract_preflight_blocked(
    runtime: Any,
    task: dict[str, Any],
    ref: str,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    *,
    attempt_id: str,
    refusal: ContractUnusable,
    failure: BringUpFailure,
    reason: str,
) -> dict[str, Any]:
    """Write the outcome decided before the claim, immediately after it."""
    _write_claim_preflight_block(runtime, 
        task,
        ref,
        records,
        payload,
        attempt_id=attempt_id,
        action="contract-preflight-blocked",
        failure=failure,
        reason=reason,
    )
    return {
        "status": "blocked",
        "step": "contract-preflight",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "reason": "broad-check contract preflight failed",
        "contract_refusal": refusal.evidence(),
        **failure.outcome_fields(reason),
    }

def _write_claim_preflight_block(
    runtime: Any,
    task: dict[str, Any],
    ref: str,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    *,
    attempt_id: str,
    action: str,
    failure: BringUpFailure,
    reason: str,
) -> None:
    """Nothing is computed here and nothing is read: the transition is the first statement made
    about the claimed card, and the dispatcher's own bookkeeping only follows it. An admission
    refusal releases the claim in that transition, since no work ever held it.
    """
    attempt_accounting.terminal_effect(runtime, 
        task,
        DispatcherRecord(
            worker=_worker_id(task),
            workspace="",
            handle="",
            head="",
            review_head="",
            attempt_id=attempt_id,
            attempt_round=runtime._journal_round(ref) + 1,
            comment_baseline=0,
            review_baseline=0,
            state="",
            claimed_at=0.0,
        ),
        target="blocked",
        reason=reason,
        request_id=_attempt_request_id(
            attempt_id,
            _bring_up_blocked_action(action, failure),
            ref,
        ),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason=_bring_up_terminal_reason(failure),
        release_claim=failure.failure_class == FAILURE_CLASS_ADMISSION,
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)


def _old_head_stop_skip(unconfirmed: dict[str, Any], run: PersistedHeadRun, role: str) -> dict[str, Any]:
    """The claim skip for a card whose previous `role` head could not be confirmed stopped."""
    run_id = str(run.get("run_id") or "")
    named = f"{role} run {run_id}" if run_id else f"{role} head with no recorded run"
    return {
        **unconfirmed,
        "status": "skipped",
        "action": CLAIM_SKIP_OLD_HEAD_STOP_UNCONFIRMED,
        "reason": f"{named}: {unconfirmed['reason']}",
    }


def _prepare_claim(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    fresh_attempt: bool = False,
) -> ClaimHandoff | dict[str, Any]:
    ref = task["ref"]
    # Both heads are decided here, before anything is claimed, and both may be decided against
    # the card's preference. Nothing launchable at the end of either walk is a claim-skip: the
    # card stays in Ready and the outcome below names the dead resource.
    worker_choice = resolve_head(runtime, runtime.catalog.worker_head(task))
    if not worker_choice.resolved:
        return {
            "status": "skipped",
            "step": "head-preflight",
            "action": CLAIM_SKIP_RESOURCE_NOT_READY,
            "pilot_ref": ref,
            "head": worker_choice.preferred,
            "readiness": worker_choice.readiness.to_json(),
            "reason": worker_choice.reason,
            "failover": {"worker": worker_choice.to_json()},
        }
    review_choice = resolve_head(runtime, runtime.catalog.review_head(task))
    collapse = _failover_collapse(runtime, worker_choice, review_choice)
    if collapse is not None:
        return dict(collapse, pilot_ref=ref)
    head = worker_choice.head
    review_head = review_choice.head or review_choice.preferred
    # Sprint admission is asked first: a card that may not run on its project now is refused for
    # that, and the registry and the host are not asked about it.
    sprint_refusal = _sprint_admission_refusal(runtime, task)
    if sprint_refusal is not None and sprint_refusal.code == SPRINT_RESERVATION_UNVERIFIABLE:
        # Refused, but not blocked: the dispatcher's own Blocked move meets the same index at the
        # write guard and would fail closed on it after the claim. The card stays in Ready and
        # is asked again on the next tick, when the index can answer.
        return {
            "status": "skipped",
            "step": "sprint-admission",
            "action": CLAIM_SKIP_SPRINT_RESERVATION_UNVERIFIABLE,
            "pilot_ref": ref,
            "sprint_reservation": sprint_refusal.evidence(),
            "reason": sprint_refusal.detail,
        }
    # A project whose repository is the live root is refused next, off the registry alone, so the
    # contract and the host are never asked about a repository no card may land in.
    live_root_refusal = (
        "" if sprint_refusal is not None
        else live_root_project_refusal(runtime.catalog, str(task.get("project") or ""))
    )
    contract_verdict = (
        None
        if sprint_refusal is not None or live_root_refusal
        else _broad_check_contract_verdict(runtime, task)
    )
    # Project Git access is a separate preflight at the same boundary. It is asked only when the
    # contract does not already refuse the card, so that refusal stays what it was: decided off
    # the registry with the host untouched. An unanswered probe leaves the card in Ready.
    git_access = (
        None
        if contract_verdict is None or contract_verdict.state == CONTRACT_REFUSED
        else _project_git_access(runtime, task)
    )
    if git_access is not None and git_access.state == "unreachable":
        return {
            "status": "skipped",
            "step": "git-access-preflight",
            "action": CLAIM_SKIP_GIT_ACCESS_UNREACHABLE,
            "pilot_ref": ref,
            "git_access": {"project": str(task.get("project") or ""), **git_access.to_json()},
            "reason": (
                f"project {task.get('project')!r} remote gave the Git access preflight no answer: "
                f"{git_access.reason or git_access.code}"
            ),
        }
    # A card the dispatcher still holds a record for, back in Ready with its claim already
    # committed under the current attempt, is a re-run. An attempt id otherwise lives as long as
    # the record, so the claim would replay idempotently, return the old event and leave the card
    # Ready: every re-run gets a fresh identity before claiming. A committed claim with no record
    # is a genuine board divergence and still fails closed below.
    active = records.get(ref)
    requeued = active is not None
    # Audit errors propagate here, before any replacement or claim effect.
    resume = resume_point(runtime, task)
    if requeued and active is not None:
        # The preempted head can still be in the workspace the next round claims, and it is
        # stopped through the workspace, not the handle: an adopted head has no handle on record.
        if active.owns_head("review"):
            # A preempt out of Validate leaves the worker pane closed by `start_review` but the
            # reviewer up; left alone its verdict would land on the new attempt.
            unconfirmed = runtime._end_review_pane_confirmed(
                active,
                records,
                payload,
                ref,
                step="claim",
                attempt_id=attempt_id,
                initiator=STOPPED_BY_REPLACEMENT,
            )
            if unconfirmed is not None:
                return _old_head_stop_skip(unconfirmed, active.review_head_run, "review")
        if active.needs_settling():
            unconfirmed = runtime._stop_worker_confirmed(active, ref, step="claim", attempt_id=attempt_id)
            if unconfirmed is not None:
                return _old_head_stop_skip(unconfirmed, active.worker_head_run, WORKER_ROLE)
    # Production creates this identity immediately before calling us; a caller reusing an attempt
    # gets a new one after a Blocked round, whose claim the old attempt has already committed.
    if not fresh_attempt and (resume.stage or requeued):
        attempt_id = _new_attempt_id()
        _record_attempt(payload, attempt_id, ref, runtime.owner, runtime.owner)
        payload["attempt_id"] = attempt_id
    claim_request_id = _attempt_request_id(attempt_id, "claim", ref)
    worker_id = _worker_id(task)
    # Claim is the only board transition that can record a Ready refusal.
    contract_outcome = (
        None
        if contract_verdict is None
        else _contract_preflight_decision(runtime, 
            task,
            contract_verdict,
            attempt_id=attempt_id,
            head=head,
            review_head=review_head,
        )
    )
    git_access_outcome = (
        _git_access_preflight_outcome(runtime, 
            task, git_access, attempt_id=attempt_id, head=head, review_head=review_head
        )
        if contract_outcome is None and git_access is not None and git_access.state == "refused"
        else None
    )
    runtime.writer.claim(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        worker=worker_id,
        resolved_head=head,
        resolved_review_head=review_head,
        slug=task.get("workspace", {}).get("slug") or "",
        base_branch=task.get("workspace", {}).get("base_branch") or "",
        cap=runtime.catalog.head_capacity(),
        code_cap=runtime.catalog.code_concurrency(str(task.get("project") or "")),
        request_id=claim_request_id,
    )
    if sprint_refusal is not None:
        return _sprint_admission_blocked(runtime, 
            task,
            ref,
            records,
            payload,
            attempt_id=attempt_id,
            head=head,
            review_head=review_head,
            refusal=sprint_refusal,
        )
    if live_root_refusal:
        return _live_root_project_blocked(
            runtime,
            task,
            ref,
            records,
            payload,
            attempt_id=attempt_id,
            refusal=live_root_refusal,
        )
    if contract_outcome is not None:
        failure, blocked_reason, refusal = contract_outcome
        return _contract_preflight_blocked(runtime, 
            task,
            ref,
            records,
            payload,
            attempt_id=attempt_id,
            refusal=refusal,
            failure=failure,
            reason=blocked_reason,
        )
    if git_access_outcome is not None and git_access is not None:
        failure, blocked_reason = git_access_outcome
        return _git_access_preflight_blocked(runtime, 
            task,
            ref,
            records,
            payload,
            attempt_id=attempt_id,
            access=git_access,
            failure=failure,
            reason=blocked_reason,
        )
    _comment_head_failover(runtime, ref, attempt_id, worker_choice, review_choice)
    claimed = runtime.reader.show(ref)
    baseline = len(claimed.get("comments") or [])
    record = DispatcherRecord(
        worker=worker_id,
        workspace="",
        handle="",
        head=head,
        review_head=review_head,
        attempt_id=attempt_id,
        comment_baseline=baseline,
        # Past every verdict already on the card, so an earlier attempt's is never this round's.
        review_baseline=baseline,
        report_generation=resume.generation + 1,
        report_decision=resume.decision,
        report_decision_id=resume.decision_id,
        report_protocol_prerequisites=resume.protocol_prerequisites,
        state="claim_verified",
        claimed_at=time.time(),
        preferred_head=worker_choice.preferred if worker_choice.substituted else "",
        preferred_review_head=(review_choice.preferred if review_choice.substituted else ""),
    )
    held = _held_candidate_workspace(runtime, claimed, record, resume)
    if held:
        # No worker round opens: the record continues the interrupted round and its accepted report.
        record.workspace = held
        record.report_generation = resume.round_generation
        record.state = STAGE_RESUME_STATE
        record.attempt_round = max(runtime._journal_round(ref), 1)
        record.outcome_terminal_path = OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT
    else:
        runtime.open_worker_round(record, round_number=runtime._journal_round(ref) + 1)
    records[ref] = record
    runtime.save_records(payload, records)
    if held:
        return continue_in_validate(runtime, claimed, record, records, payload)
    return ClaimHandoff(
        claimed=claimed,
        record=record,
        require_existing_workspace=resume.require_existing_workspace,
    )


def _held_candidate_workspace(
    runtime: Any, task: dict[str, Any], record: DispatcherRecord, resume: ResumePoint
) -> str:
    """The retained checkout when a gate or review stage continues on the round's own candidate, else "".

    Its HEAD must be the interrupted round's admitted candidate, and its TASK.md must still name that
    round's reports, which is where the Validate path reads the round's admission evidence from.
    """
    if resume.stage not in {STAGE_GATE, STAGE_REVIEW} or not resume.candidate_sha:
        return ""
    ref = str(task["ref"])
    workspace = runtime.host.restore_workspace(task, record.worker)
    if resume.round_report not in _round_report_ids(workspace, record.attempt_id, ref, resume.round_generation):
        return ""
    if runtime.host.head_commit(replace(record, workspace=workspace)) != resume.candidate_sha:
        return ""
    return workspace


def continue_in_validate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Move a claim held on its round's candidate into Validate, where the gate and review run on it.

    Idempotent under one record: a tick that dies before the move replays it with the same request id.
    """
    ref = str(task["ref"])
    attempt_accounting.persist_outcome_round_context(runtime, task, record, phase="worker")
    attempt_accounting.capture_outcome_source(
        runtime, task, record, phase="report", kind="card.reported", marker="report:done"
    )
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="validate",
        reason=(
            "the checkout still holds the candidate of the round's accepted report:done; "
            "validation continues on it with no worker round"
        ),
        request_id=_attempt_request_id(record.attempt_id, "stage-resume", ref, str(record.report_generation)),
    )
    record.state = "validate"
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "claim",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": "stage-resumed",
        "workspace": record.workspace,
        "to": "validate",
    }


def resume_point(runtime: Any, task: dict[str, Any]) -> ResumePoint:
    """How the claim of `task` resumes, read from the card's audit alone.

    The interrupted round is the one the card's last dispatcher Blocked move ended, unless a worker
    round opened since. Its decision is the rework decision that opened it, kept by every claim that
    resumes it; a Blocked move out of Assessment carries none, the observer having decided already.
    A rework decided on the Blocked card replaces that decision and resumes at the worker stage.
    """
    ref = str(task["ref"])
    token = re.escape(request_token(ref))
    reports = re.compile(rf"-({'|'.join(map(re.escape, _REPORT_ACTIONS))})-{token}-(\d+)$")
    bounce = re.compile(rf"-stale-done-rework-{token}-\d+$")
    events = runtime.audit.events(ref)
    generation = 0
    decision: dict[str, Any] | None = None
    stage = ""
    stage_decision: dict[str, Any] | None = None
    # The round's last worker report, and that report as the interrupted round left it.
    last_report: tuple[dict[str, Any], str, int] | None = None
    stage_report: tuple[dict[str, Any], str, int] | None = None
    for index, event in enumerate(events):
        request_id = str(event.get("request_id") or "")
        if match := reports.search(request_id):
            generation = max(generation, int(match[2]))
            last_report = (event, match[1], int(match[2]))
        dispatcher = _actor_role(event) == "dispatcher"
        if dispatcher and bounce.search(request_id):
            decision = None
        transition = card_transition_of(event)
        if transition is None:
            continue
        source, target = transition
        if target == "in_progress":
            if source == "assessment":
                _visit, decided = assessment_resolution(events[:index])
                decision = decided if decided and _event_payload(decided).get("decision") == "rework" else None
            elif (reworked := blocked_rework(events[:index])[1]) is not None:
                decision = reworked
            elif source in {"ready", "blocked"} and stage:
                decision = stage_decision
            else:
                decision = None
            if source in {"validate", "assessment"}:
                last_report = None
            stage, stage_decision = "", None
        elif target == "blocked" and dispatcher and source in {"in_progress", "validate", "assessment"}:
            stage = _interrupted_stage(request_id, token, source)
            stage_decision = None if source == "assessment" else decision
            stage_report = last_report
    _visit, reworked = blocked_rework(events)
    if reworked is not None:
        # The observer's rework replaces the round's decision, and only new work answers it.
        stage_decision = reworked
        stage = STAGE_WORKER if stage in {STAGE_GATE, STAGE_REVIEW} else stage
    elif not stage:
        return ResumePoint()
    body, prerequisites = (
        rework_instruction(task, events, stage_decision) if stage_decision is not None else ("", ())
    )
    report, action, report_generation = stage_report or ({}, "", 0)
    admitted = (
        admitted_check(_event_payload(report).get("worker_check")) if action == "worker-report-done" else None
    )
    return ResumePoint(
        require_existing_workspace=stage not in {"", STAGE_ADMISSION},
        stage=stage,
        decision_id=str(stage_decision.get("event_id") or "") if body and stage_decision else "",
        decision=body,
        protocol_prerequisites=prerequisites,
        generation=generation,
        round_report=str(report.get("request_id") or "") if admitted else "",
        round_generation=report_generation if admitted else 0,
        candidate_sha=str(admitted["candidate_sha"]) if admitted else "",
    )


def _actor_role(event: dict[str, Any]) -> str:
    actor = event.get("actor")
    return str(actor.get("role") or "") if isinstance(actor, dict) else ""


def _interrupted_stage(request_id: str, token: str, source: str) -> str:
    """The stage a dispatcher Blocked move out of `source` stopped its attempt at."""
    def names(actions: tuple[str, ...]) -> bool:
        return any(re.search(rf"-{re.escape(action)}-{token}(?:-|$)", request_id) for action in actions)

    if names(_NO_CHECKOUT_ACTIONS):
        return STAGE_ADMISSION
    if source == "in_progress" or names(_WORKER_ACTIONS):
        return STAGE_WORKER
    return STAGE_GATE if names(_GATE_ACTIONS) else STAGE_REVIEW


def claim_ready_task(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    fresh_attempt: bool = False,
) -> dict[str, Any]:
    """Claim one Ready card and cross the typed handoff into worker bring-up."""
    if is_po_executed(task):
        # No head, no workspace, no broad-check or Git preflight: the PO service executes it.
        return claim_po_card(runtime, task, records, payload, attempt_id)
    if is_wait(task):
        # The same for a wait card: the dispatcher itself advances it, once per tick.
        return claim_wait_card(runtime, task, records, payload, attempt_id)
    prepared = _prepare_claim(runtime, task, records, payload, attempt_id, fresh_attempt=fresh_attempt)
    if not isinstance(prepared, ClaimHandoff):
        return prepared
    return launch_worker_after_claim(
        runtime,
        prepared.claimed,
        prepared.record,
        records,
        payload,
        require_existing_workspace=prepared.require_existing_workspace,
    )
