"""The task protocol over the Pipeline board of the PostgreSQL board store."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu.board import e2e_budget, e2e_record, owner_events, wait_card
from ummanu.board import po_execution as execution_field
from ummanu.board import po_origin as origin_field
from ummanu.board.audit_contract import card_transition_of, is_protocol_event
from ummanu.board.backend import BOARD_STORE_KIND, entity_id, entity_number
from ummanu.board.card_transitions import CardTransitionForbidden, card_transition
from ummanu.board.completion_evidence import (
    PO_COMPLETION_MARKERS,
    has_candidate,
    infra_report_fields,
    is_headless,
    is_po_executed,
    is_wait,
    po_completion_fields,
    render_po_completion_record,
    research_report_refusal,
)
from ummanu.board.events import AnalyticsOutcomeConflict, BoardEventCanon, BoardEventPending
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.host import MarkerComment, MutationResult, TransitionRequest
from ummanu.board.legacy_codec import (
    TASK_KNOWN_METADATA as _KNOWN_METADATA,
)
from ummanu.board.legacy_codec import (
    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,
)
from ummanu.board.legacy_codec import (
    enum_or_default as _enum_or_default,  # noqa: F401 - released private compatibility alias
)
from ummanu.board.legacy_codec import (
    enum_or_none as _enum_or_none,  # noqa: F401 - released private compatibility alias
)
from ummanu.board.legacy_codec import (
    nonnegative_int as _nonnegative_int,
)
from ummanu.board.legacy_codec import (
    null_if_empty as _null_if_empty,  # noqa: F401 - released private compatibility alias
)
from ummanu.board.legacy_codec import (
    positive_int as _positive_int,
)
from ummanu.board.legacy_codec import (
    split_heads as _split_heads,  # noqa: F401 - released private compatibility alias
)
from ummanu.board.legacy_codec import (
    text as _text,
)
from ummanu.board.models import (
    Actor,
    CardState,
    EntityKind,
    Event,
    EventKind,
    RelatedRefs,
)
from ummanu.board.outcome_round_context import OutcomeRoundContext
from ummanu.board.owner_handover import (
    CLEAR_MARK,
    HANDED_TO_OWNER,
    OWNER,
    OWNER_ANSWER,
    OWNER_ANSWER_RECORDED,
    OWNER_ESCALATION,
    OWNER_ROLE,
    attention_record,
    carries_mark_fields,
    current_handover,
    mark_values,
    owner_answer_event_ids,
    owner_comments_since_handover,
    po_episode,
    render_handover_comment,
    waiting_owner,
)
from ummanu.board.production_rights import (
    ACTIVATION_OPERATION_REQUEST_PREFIX,
    NO_PRODUCTION,
    TOUCHES_PRODUCTION,
)
from ummanu.board.production_rights import (
    create_refusal as production_create_refusal,
)
from ummanu.board.production_rights import (
    touches_production as card_production,
)
from ummanu.board.protocol_artifacts import (
    ArtifactOwnershipViolation,
    validate_rework_prerequisites,
)
from ummanu.board.roles import (
    BOARD_ROLES,
    COMMENT_ROLES,
    CREATE_ROLES,
    EDIT_ROLES,
    PROPOSAL_CREATE_ROLES,
    Role,
)
from ummanu.board.task_routing import (
    ACTIVE_STATES,
    BLOCK_CLASSIFICATION_VALUES,
    DECIDED_TARGETS,
    DECISION_TARGETS,
    DECISION_VALUES,
    EDITABLE_STATES,
    FAMILY_PREFERENCE_VALUES,
    PO_EXECUTED_TYPES,
    ROUTING_PHASE_VALUES,
    TASK_COMPLEXITY_VALUES,
    TASK_TYPE_VALUES,
    UNDECIDED_EXITS,
    BlockClassification,
    FamilyPreference,
    RoutingPhase,
    TaskComplexity,
    TaskDecision,
    TaskMetadata,
    TaskReview,
    TaskType,
    default_review,
    impact_bounds_refusal,
)
from ummanu.board.transitions import BoardProtocolError
from ummanu.dispatch.cleanup import CleanupJournal, serialized
from ummanu.projects.integration_base import (
    integration_base_refusal,
    seed_ref_refusal,
)
from ummanu.runtime.head import CODEX_LAUNCH_MODES
from ummanu.runtime.redact import redact
from ummanu.runtime.references import (
    BoardRowsUnavailable,
    board_rows,
    next_reference,
    reference_allocation_lock,
)
from ummanu.runtime.role_env import RUNTIME_ENV_FILE_ENVS, runtime_env_path

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient


class TaskError(Exception):
    """A task command failed without exposing backend credentials."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


#: The one role/actor pair no write may carry: the observer's actor under the PO's role. The observer
#: speaks in its own name (`--role observer`), under the identity guard that binds it to its sprint;
#: a PO write naming it would be that head stepping around the guard.
MASQUERADE = (Role.PO, "observer")


def admit_role(role: Role | str, actor: str, allowed: Collection[Role | str]) -> Role:
    """The role check every role-taking sprint, task and issue write makes before anything else.

    Two refusals, in this order. A PO write whose actor is the observer is `role_masquerade`, whatever
    the operation would otherwise admit: that check lives here and nowhere else, so a writer entry
    point that admits its role through this function cannot skip it. Then a role outside `allowed` is
    `role_forbidden`. Neither reads or writes anything.
    """
    if str(role) == MASQUERADE[0].value and str(actor or "").strip() == MASQUERADE[1]:
        raise TaskError(
            "role_masquerade",
            "the observer writes in its own name: use --role observer --actor observer, not --role po",
            3,
        )
    try:
        normalized_role = Role(role)
    except (TypeError, ValueError):
        raise TaskError("role_forbidden", "role is not permitted for this operation", 3) from None
    if normalized_role not in {Role(value) for value in allowed}:
        raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
    return normalized_role


class ArtifactOwnershipTaskError(TaskError):
    """The task-protocol form of a registry-backed ownership violation."""

    def __init__(self, violation: ArtifactOwnershipViolation) -> None:
        self.violation = violation
        super().__init__("artifact_ownership_violation", violation.message, 3)


class SprintReservationUnverifiable(Exception):
    """The open sprints reserving a project could not be read; `sprint_ref` names the failed read."""

    def __init__(self, sprint_ref: str, cause: TaskError) -> None:
        super().__init__(cause.message)
        self.sprint_ref = sprint_ref
        self.cause = cause


class _CommittedWriteError(Exception):
    """A later step failed after a board mutation was committed."""


RUNTIME_TAILS = ("ummanu-data",)


def durability_dirt(porcelain: str) -> list[str]:
    """Porcelain lines that count against durability."""
    dirt: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? "):
            path = line[3:].strip().strip('"').rstrip("/")
            if path in RUNTIME_TAILS or any(path.startswith(f"{tail}/") for tail in RUNTIME_TAILS):
                continue
        dirt.append(line)
    return dirt


def workspace_dirt(workspace: str | os.PathLike[str]) -> list[str]:
    """Uncommitted work in a checkout, or nothing when it is not a git checkout."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return durability_dirt(completed.stdout)


def _sprint_guard_denial_request_id(request_id: str) -> str:
    """Keep a denied guard check from consuming the operation's retry key."""
    return "sprint-guard-denied-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _sprint_guard_override_request_id(request_id: str) -> str:
    """Keep a granted override from consuming the operation's retry key."""
    return "sprint-guard-override-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _artifact_ownership_refusal_request_id(request_id: str) -> str:
    """Keep a refused instruction visible without consuming its decision retry key."""
    return "artifact-ownership-refusal-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _done_retention_request_id(task_id: int, date_moved: int) -> str:
    """One durable retry key for one card's one Done dwell episode."""
    # The identity is part of every stored retry key: changing it would orphan a pending episode,
    # which is why revision 0014 refuses a store holding a non-committed `done-retention-` request.
    identity = f"card:{task_id}:done:{date_moved}"
    return "done-retention-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


# Retired launch modes normalize away rather than silently changing a requested shape.
_CODEX_LAUNCH_MODES = CODEX_LAUNCH_MODES
# Agent roles that may not open an execution card are represented by
# PROPOSAL_CREATE_ROLES in the canonical board-role vocabulary.
_READY_RESET_METADATA = {
    "claim": "",
    "resolved_head": "",
    "resolved_review_head": "",
    "retry_same": "",
    "retry_switch": "",
    "retry_heads": "",
}
# Dispatcher Assessment moves require decisions; human escape-hatch moves do not.
_DECISION_BOUND_ROLES: frozenset[Role] = frozenset({Role.DISPATCHER})
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")
# A Product or an Issue is not an execution task: it never takes a claim or a task transition,
# whatever column it currently sits in.
_TYPED_RECORD_TYPES = {"issue", "product"}
#: The audit kind of `task cancel` on a wait card.
WAIT_CANCELLED = "wait_cancelled"
#: The transition data key a wait card's terminal move (and a dependent it Blocks) carries.
WAIT_OUTCOME_KEY = "wait_outcome"
#: The PO session whose turn ran `task complete` or `task handover` (secretary-1792): the transition
#: data of a completion, the payload of a handover. Read from `UMMANU_PO_SESSION` by the CLI; absent
#: when the command ran outside a PO turn.
PO_SESSION_KEY = "po_session"

_MARKER_EVENT_ACTIONS = {
    EventKind.CARD_REPORTED.value: "reported",
    EventKind.CARD_VERDICTED.value: "verdict",
    EventKind.CARD_DECIDED.value: "decided",
}


def _event_action(event: dict[str, Any]) -> str:
    """The released control-plane action spelling for generic history readers."""
    return _MARKER_EVENT_ACTIONS.get(str(event.get("kind") or ""), str(event.get("kind") or ""))


def _projection_slice(
    records: list[tuple[dict[str, Any], bool]], kinds: frozenset[str], outcome_owed: bool
) -> list[tuple[dict[str, Any], bool]]:
    """The records an occurrence projection over `kinds` is decided from, in their read order."""
    own = [record for record, _pending in records if record.get("kind") in kinds]
    request_ids = {record.get("request_id") for record in own if isinstance(record.get("request_id"), str)}
    event_ids = {record.get("event_id") for record in own if isinstance(record.get("event_id"), str)}

    def kept(record: dict[str, Any]) -> bool:
        if record.get("kind") in kinds:
            return True
        request_id, event_id = record.get("request_id"), record.get("event_id")
        if (isinstance(request_id, str) and request_id in request_ids) or (
            isinstance(event_id, str) and event_id in event_ids
        ):
            return True
        data = record.get("data")
        return outcome_owed and isinstance(data, dict) and "attempt_outcome_owed" in data

    return [(record, pending) for record, pending in records if kept(record)]


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Read a legacy payload or the typed marker data without rewriting history."""
    if is_protocol_event(event):
        data = event.get("data")
        return data if isinstance(data, dict) else {}
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def specification_revision(events: Iterable[dict[str, Any]], description: str) -> str:
    """Return the durable event id of the description the card currently exposes.

    A digest identifies content, but not an edit that returns a card to earlier text. The latest
    create/edit event which wrote the current digest is therefore the revision boundary. Legacy,
    malformed, or divergent history deliberately has no boundary: callers that would otherwise
    replay an instruction must fail closed.
    """
    current_digest = _digest(description)
    latest: dict[str, Any] | None = None
    for event in events:
        if str(event.get("kind") or "") not in {"created", "edited"}:
            continue
        payload = _event_payload(event)
        digest = payload.get("description_sha256")
        if digest is None:
            # A title/routing-only edit does not create a specification revision.
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return ""
        latest = event
    if latest is None:
        return ""
    payload = _event_payload(latest)
    if payload.get("description_sha256") != current_digest:
        return ""
    revision = latest.get("event_id")
    return revision if isinstance(revision, str) and revision else ""


def _po_card_create_refusal(
    kind: str,
    *,
    role: str,
    sprint: str,
    head: str,
    review_head: str,
    review: TaskReview,
    live_impact: bool,
    seed_ref: str,
    base_branch: str,
    origin: bool = False,
    activation_operation: bool = False,
    execution: bool = False,
) -> str:
    """Why a `decision`/`operation`/`wait` card cannot be created as asked, or `""`.

    The PO service executes a decision or operation card in a turn of its sprint's PO session: it
    needs that sprint, unless the PO cuts it inside a PO turn (`origin`), where the session of that turn
    executes it (secretary-1792). The dispatcher advances a wait card itself; the observer cuts one only
    for its own sprint, the PO in a sprint or outside every sprint, and the dispatcher one for a code
    card's e2e run (secretary-1795), in that card's sprint or in none. None of them has a head, a
    reviewer, a checkout or a live impact of its own to declare.
    """
    waits = kind == TaskType.WAIT.value
    # The dispatcher cuts a code card's e2e wait, the decision a spent e2e budget needs
    # (secretary-1796), and the operation a refused production activation needs, under its request id
    # (secretary-1824); nothing else.
    if (
        role == Role.DISPATCHER.value
        and kind not in {TaskType.WAIT.value, TaskType.DECISION.value}
        and not (kind == TaskType.OPERATION.value and activation_operation)
    ):
        return f"the dispatcher cuts only a wait or a decision card, not a {kind} card"
    if role not in {Role.OBSERVER.value, Role.PO.value, Role.DISPATCHER.value}:
        return f"a {kind} card is cut by the observer or the PO, not by {role}"
    if not sprint and not waits and not origin and not execution:
        return (
            f"a {kind} card needs --sprint: the PO session of that sprint executes it (cut inside a PO "
            "turn, the session of that turn does)"
        )
    if not sprint and role == Role.OBSERVER.value:
        return "the observer cuts a wait card for its own sprint: it needs --sprint"
    runner = "the dispatcher advances it" if waits else "the PO service does"
    refused = [
        (bool(head), "--head", f"no head runs it; {runner}"),
        (bool(review_head), "--review-head", "nobody reviews it"),
        (review is TaskReview.REQUIRED, "--review required", "its review is skipped"),
        (live_impact, "--live-impact", "that is a research attribute"),
        (bool(seed_ref), "--seed-ref", "it has no checkout to seed"),
        (bool(base_branch), "--base-branch", "it integrates into no branch"),
    ]
    for present, flag, reason in refused:
        if present:
            return f"a {kind} card takes no {flag}: {reason}"
    return ""


#: The create flags of a wait card, as `TaskWriter.create(wait=...)` takes them.
_WAIT_FIELDS = ("run", "run_id", "card", "states", "until", "deadline", "transient_window")


def _wait_request(wait: Mapping[str, Any] | None) -> dict[str, Any]:
    """A wait card's create flags, normalized; `{}` when none was given.

    This is what the create's request id binds (a retry recomputes it), not the spec, whose relative
    deadline and creation time depend on the clock.
    """
    if not wait:
        return {}
    request: dict[str, Any] = {
        key: _text(wait.get(key)).strip() for key in _WAIT_FIELDS if _text(wait.get(key)).strip()
    }
    returns = wait.get("returns") or []
    if isinstance(returns, str):
        returns = [returns]
    named = [_text(value).strip() for value in returns if _text(value).strip()]
    if named:
        request["returns"] = named
    return request


def _origin_request(origin: Mapping[str, Any] | None) -> dict[str, str]:
    """The PO turn a create runs in, `{session, request}` normalized; `{}` when there is none."""
    if not origin:
        return {}
    session = _text(origin.get("session")).strip()
    if not session:
        return {}
    return {"session": session, "request": _text(origin.get("request")).strip()}


def _check_execution_record(task: dict[str, Any]) -> None:
    """Reject a Product or an Issue on an execution-task path, before any write."""
    if task.get("record_type") in _TYPED_RECORD_TYPES:
        raise TaskError(
            "transition_forbidden",
            "Product issues and products cannot enter execution task columns",
            3,
        )


def _forbidden_move_message(role: str, source: str, target: str) -> str:
    """Why a role may not make this move, said in the terms of the role that asked."""
    if role == "observer" and source == "assessment":
        return (
            "the observer decides about a parked card and the dispatcher performs the decision: "
            "record it with `task decide` instead of moving the card"
        )
    return f"{role} may not move {source} to {target}"


def _transition_reason(reason: str, target: str) -> str:
    """The non-empty reason a typed Card transition event carries."""
    return reason if reason.strip() else f"Card transition to {target}"


def assessment_resolution(events: Iterable[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """The current Assessment visit and its one canonical decision."""
    ordered = list(events)
    latest_park = -1
    for index, event in enumerate(ordered):
        payload = _event_payload(event)
        lifecycle = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        if (event.get("kind") == "moved" and str(payload.get("to") or "") == "assessment") or (
            is_protocol_event(event)
            and str(lifecycle.get("target") or "") == "assessment"
        ):
            latest_park = index
    if latest_park < 0:
        return "", None
    visit = str(ordered[latest_park].get("event_id") or ordered[latest_park].get("request_id") or "")
    for event in ordered[latest_park + 1 :]:
        payload = _event_payload(event)
        if _event_action(event) != "decided" or not str(payload.get("decision") or ""):
            continue
        recorded_visit = str(payload.get("assessment_visit") or "")
        if not recorded_visit or recorded_visit == visit:
            return visit, event
    return visit, None


def standing_decision(events: Iterable[dict[str, Any]]) -> str:
    """The canonical decision for the current Assessment visit, or an empty string."""
    _visit, event = assessment_resolution(events)
    payload = _event_payload(event) if isinstance(event, dict) else {}
    return str(payload.get("decision") or "")


def recorded_card_transition(event: dict[str, Any]) -> tuple[str, str] | None:
    """The board-state transition one audit event records, or `None` (`audit_contract.card_transition_of`)."""
    return card_transition_of(event)


#: The data key a dispatcher release puts on the Done transition when its release merged a commit
#: onto the integration base. Its presence says "the post-merge CI result will follow".
RELEASE_MERGE_KEY = "release_merge"
#: The payload key of the one card event that records a post-merge CI result.
POST_MERGE_CI_KEY = "post_merge_ci"
POST_MERGE_CI_RESULTS = ("green", "red", "absent", "timeout")


def post_merge_ci_fact(event: dict[str, Any]) -> dict[str, Any] | None:
    """The post-merge CI result a dispatcher card event records, or None for any other event."""
    if is_protocol_event(event) or str(event.get("kind") or "") != "commented":
        return None
    if str(event.get("outcome") or "") != "success":
        return None
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    if str(actor.get("role") or "") != "dispatcher":
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    fact = payload.get(POST_MERGE_CI_KEY)
    if not isinstance(fact, dict) or str(fact.get("result") or "") not in POST_MERGE_CI_RESULTS:
        return None
    return fact


def is_dispatcher_hotfix(event: dict[str, Any]) -> bool:
    """Whether an event is the dispatcher's create of a sprint `hotfix` card (secretary-1807).

    The dispatcher cuts one for a red after-merge e2e run in the open sprint of the newest card that
    run covered; the sprint's observer wakes on it the way it wakes on a Blocked card of the sprint.
    """
    if str(event.get("kind") or "") != "created" or str(event.get("outcome") or "") != "success":
        return False
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(actor.get("role") or "") == "dispatcher" and payload.get("budget_event") == "hotfix"


def is_merged_release(event: dict[str, Any]) -> bool:
    """Whether a Done transition is a dispatcher release whose merge landed on the base.

    Such a Done is not yet the release's last word: the post-merge CI result is, and that is the
    event the observer is woken on.
    """
    if not is_protocol_event(event):
        return False
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return str(actor.get("role") or "") == "dispatcher" and isinstance(data.get(RELEASE_MERGE_KEY), dict)


def is_significant_card_event(event: dict[str, Any], *, linked_refs: set[str]) -> bool:
    """Whether a card transition needs a new observer decision.

    Deliberately small, because it drives both wake delivery and resume freshness: a broad
    "successful event" rule turns every piece of machinery telemetry — the observer's own decision
    included — into another observer turn.

    This is the one place the post-merge rule lives: a Done that a dispatcher release-with-merge
    produced is not a wake, and the post-merge CI result recorded for that merge is. Every other
    Done — a research or infra card, a release that merged nothing, a manual PO or steward Done —
    wakes as before.
    """
    if str(event.get("ref") or "") not in linked_refs:
        return False
    typed = is_protocol_event(event)
    if not typed and str(event.get("outcome") or "") != "success":
        return False
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    if str(actor.get("role") or "") == "observer":
        return False
    if post_merge_ci_fact(event) is not None:
        return True
    if is_dispatcher_hotfix(event):
        # A red after-merge e2e run's hotfix card, cut in the sprint: a decision, as a Blocked card is.
        return True
    moved = recorded_card_transition(event)
    if moved is None:
        return False
    source, target = moved
    if target == "done" and is_merged_release(event):
        return False
    # Assessment requires a decision; Blocked requires classification.
    if target in {"assessment", "blocked", "done"}:
        return True
    # Only human control-plane removal of work wakes the observer.
    return (
        str(actor.get("role") or "") in {"po", "steward"} and source in ACTIVE_STATES and target == "issues"
    )


def is_significant_observer_event(
    event: dict[str, Any],
    *,
    linked_refs: set[str],
    sprint_ref: str,
) -> bool:
    """Whether an audit event is a semantic wake for one sprint observer."""
    if is_significant_card_event(event, linked_refs=linked_refs):
        return True
    if str(event.get("ref") or "") != sprint_ref:
        return False
    if str(event.get("outcome") or "") != "success":
        return False
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    if str(actor.get("role") or "") == "observer":
        return False
    kind = str(event.get("kind") or "")
    if kind in {"budget_recorded", "budget_hard_stopped"}:
        return True
    # PO is the human control-plane role.  Dispatcher and role comments are routine telemetry.
    return kind == "commented" and str(actor.get("role") or "") == "po"


_BATCH_CHUNK = 200


def all_project_cards(client: SqlCardClient, project_id: int) -> list[dict[str, Any]]:
    """Return every card of one board, open and archived alike."""
    try:
        return board_rows(client.call, project_id)
    except BoardRowsUnavailable:
        raise TaskError("backend_error", "board store returned an invalid task list", 1) from None


def _task_metadata(answer: Any) -> dict[str, str]:
    """One task's metadata as the flat str->str map every reader works with."""
    if answer is not None and not isinstance(answer, dict):
        raise TaskError("backend_error", "board store returned invalid task metadata", 1)
    return {str(key): _text(value) for key, value in (answer or {}).items()}


def project_card_by_reference(
    client: SqlCardClient, project_id: int, reference: str
) -> dict[str, Any] | None:
    """Return the live card for a reference when an archived duplicate exists."""
    card = client.call("getTaskByReference", project_id=project_id, reference=reference)
    if not isinstance(card, dict) or _task_is_active(card):
        return card if isinstance(card, dict) else None
    active_cards = client.call("getAllTasks", project_id=project_id, status_id=1)
    if not isinstance(active_cards, list):
        raise TaskError("backend_error", "board store returned an invalid task list", 1)
    for candidate in active_cards:
        if isinstance(candidate, dict) and candidate.get("reference") == reference:
            return candidate
    return card


def project_card_by_id(client: SqlCardClient, project_id: int, task_id: int) -> dict[str, Any] | None:
    """Return the exact board row named by a recorded board task id."""
    for card in all_project_cards(client, project_id):
        if _positive_int(card.get("id")) == task_id:
            return card
    return None


def next_project_reference(client: SqlCardClient, project_id: int, project: str) -> str:
    """Allocate the reference immediately after this project's board-wide high-water mark."""
    return next_reference(all_project_cards(client, project_id), f"{project}-")


@contextlib.contextmanager
def assessment_decision_lock(data_dir: Path, reference: str) -> Iterator[None]:
    """Serialize the complete decision transaction for one card, across observer processes."""
    directory = data_dir / "board" / "assessment-decisions"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(reference.encode("utf-8")).hexdigest() + ".lock"
    with (directory / name).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class TaskReader:
    def __init__(self, client: SqlCardClient, board_name: str = "Pipeline") -> None:
        self.client = client
        self.board_name = board_name

    def list(
        self, *, states: set[str] | None = None, project: str | None = None, sprint: str | None = None
    ) -> list[dict[str, Any]]:
        project_id, columns, swimlanes = self._board()
        cards = self.client.call("getAllTasks", project_id=project_id, status_id=1) or []
        if not isinstance(cards, list):
            raise TaskError("backend_error", "board store returned an invalid task list", 1)
        rows = [card for card in cards if isinstance(card, dict)]
        # One batched read for the whole listing: metadata is per task in the board vocabulary, so
        # asking row by row is what made a board-wide listing a per-row round trip.
        metadata = self._metadata_of(rows)
        result = []
        for card in rows:
            normalized = self._normalize(
                card, columns, swimlanes, metadata[_task_number(card)], comments=None
            )
            if states and normalized["state"] not in states:
                continue
            if project is not None and normalized["project"] != project:
                continue
            if sprint is not None and normalized["sprint"] != sprint:
                continue
            result.append(normalized)
        self._attach_origin_returns(result)
        return sorted(result, key=lambda task: (task["state"], task["position"], task["ref"], task["id"]))

    def _attach_origin_returns(self, cards: list[dict[str, Any]]) -> None:
        """Fill each delegated card's `origin.returns` from its outbox rows, in one read (secretary-1792).

        The outbox (`board/origin_outbox.py`) is the one record of what a card owes its origin session
        and what was delivered. A client with no outbox, or a store before 0022, leaves them empty.
        """
        delegated = [card for card in cards if isinstance(card.get("origin"), dict)]
        if not delegated:
            return
        from ummanu.board.origin_outbox import outbox_for

        outbox = outbox_for(self.client)
        if outbox is None:
            return
        rows = outbox.rows_for(card["ref"] for card in delegated)
        for card in delegated:
            card["origin"]["returns"] = [row.to_json() for row in rows.get(card["ref"], [])]

    def steward_reports_in_progress(self, project: str) -> list[dict[str, Any]]:
        """Return the small, durable report view a steward dispatch needs.

        This intentionally is not a second public Card list: callers get only the
        identity, freshness timestamp and report marker needed to decide whether a
        steward sweep is already running.  The board vocabulary exposes metadata per task,
        so all candidate metadata is fetched in one batch rather than one call per row.
        """
        project_id, columns, _ = self._board()
        in_progress_id = next(
            (identifier for identifier, title in columns.items() if title == "In progress"), None
        )
        if in_progress_id is None:
            raise TaskError("backend_error", "board schema is invalid", 1)
        raw = self.client.call("getAllTasks", project_id=project_id, status_id=1) or []
        if not isinstance(raw, list):
            raise TaskError("backend_error", "board store returned an invalid task list", 1)
        cards = [
            card
            for card in raw
            if isinstance(card, dict) and _positive_int(card.get("column_id")) == in_progress_id
        ]
        metadata = self._metadata_of(cards)
        reports: list[dict[str, Any]] = []
        for card in cards:
            task_id = _task_number(card)
            meta = metadata[task_id]
            if meta.get("project") != project or meta.get("steward_report") != "1":
                continue
            reports.append(
                {
                    "reference": _text(card.get("reference")),
                    "date_moved": _positive_int(card.get("date_moved")),
                    "steward_report": "1",
                }
            )
        return reports

    def steward_signal_cards(
        self, *, states: set[str] | None = None, project: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the bounded operational card view used by steward anomaly reads.

        This is deliberately narrower than :meth:`list`: it exposes only the
        active-card fields a watchdog needs and keeps raw board rows private.
        Metadata is fetched once for the active board snapshot, never per card.
        """
        if states is not None and (unknown := states - set(_STATE_BY_COLUMN.values())):
            raise TaskError("validation", f"unknown task states: {sorted(unknown)}", 2)
        project_id, columns, _ = self._board()
        raw = self.client.call("getAllTasks", project_id=project_id, status_id=1)
        if not isinstance(raw, list) or any(not isinstance(card, dict) for card in raw):
            raise TaskError("backend_error", "board store returned an invalid task list", 1)
        metadata = self._metadata_of(raw)
        cards: list[dict[str, Any]] = []
        for card in raw:
            task_id = _task_number(card)
            column = columns.get(_positive_int(card.get("column_id")) or -1)
            state = _STATE_BY_COLUMN.get(column or "")
            if state is None:
                raise TaskError("backend_error", "board schema is invalid", 1)
            meta = metadata[task_id]
            card_project = _text(meta.get("project"))
            if states is not None and state not in states:
                continue
            if project is not None and card_project != project:
                continue
            cards.append(
                {
                    "reference": _text(card.get("reference")),
                    "state": state,
                    "column": column,
                    "project": card_project,
                    "date_moved": _positive_int(card.get("date_moved")),
                    "steward_report": _text(meta.get("steward_report")),
                }
            )
        return cards

    def done_retention_candidates(self) -> list[dict[str, Any]]:
        """Return the deliberately small view used by Done-retention cleanup.

        The cleanup is allowed one active-board snapshot and one metadata batch.
        It must not infer an age for incomplete board rows, so an unusable
        ``date_moved`` is represented as ``None`` for the caller to skip.
        """
        project_id, columns, _ = self._board()
        done_id = next((identifier for identifier, title in columns.items() if title == "Done"), None)
        if done_id is None:
            raise TaskError("backend_error", "board schema is invalid", 1)
        raw = self.client.call("getAllTasks", project_id=project_id, status_id=1)
        if not isinstance(raw, list) or any(not isinstance(card, dict) for card in raw):
            raise TaskError("backend_error", "board store returned an invalid task list", 1)
        done = [
            card for card in raw if _positive_int(card.get("column_id")) == done_id and _task_is_active(card)
        ]
        metadata = self._metadata_of(done)
        candidates: list[dict[str, Any]] = []
        for card in done:
            task_id = _task_number(card)
            # Product and Issue rows may be displayed in a task column, but they
            # are not execution-task retention candidates.
            if metadata[task_id].get("record_type") in _TYPED_RECORD_TYPES:
                continue
            candidates.append(
                {
                    "reference": _text(card.get("reference")),
                    "date_moved": _positive_int(card.get("date_moved")),
                }
            )
        return sorted(candidates, key=lambda candidate: str(candidate["reference"]))

    def export(self) -> list[dict[str, Any]]:
        """Return the complete legacy checkpoint projection in bounded board reads.

        Checkpoints retain the established board schema while reading it through the
        canonical Ummanu transport.  Metadata and comments are per task in the board
        vocabulary, so both are requested in one batch for the whole board rather than
        once per card.
        """
        # The installed head registry remains the authority for legacy effective-head values;
        # this is deliberately not a dependency on pipeline board operations or its export CLI.
        from ummanu.runtime.heads import HeadRegistryError, default_head, reviewer_head

        def role_default_or_blank(lookup: Callable[[], str]) -> str:
            # A read path: a registry with no role default for this role leaves the effective head
            # blank rather than refusing to list the board.
            try:
                return lookup()
            except HeadRegistryError:
                return ""

        project_id, columns, swimlanes = self._board()
        cards = all_project_cards(self.client, project_id)
        rows = [card for card in cards if isinstance(card, dict)]
        task_ids = [_task_number(card) for card in rows]
        answers = self.client.call_batch(
            (method, {"task_id": task_id})
            for task_id in task_ids
            for method in ("getTaskMetadata", "getAllComments")
        )
        result = []
        for index, card in enumerate(rows):
            meta = _task_metadata(answers[index * 2])
            raw_comments = answers[index * 2 + 1] or []
            if not isinstance(raw_comments, list):
                raise TaskError("backend_error", "board store returned invalid task comments", 1)
            task_id = task_ids[index]
            head = _text(meta.get("head"))
            review = _text(meta.get("review_head"))
            result.append(
                {
                    "id": task_id,
                    "reference": _text(card.get("reference")),
                    "title": _text(card.get("title")),
                    "description": _text(card.get("description")),
                    "column": columns.get(_positive_int(card.get("column_id")) or -1, ""),
                    "swimlane": swimlanes.get(_positive_int(card.get("swimlane_id")) or -1, ""),
                    "position": _nonnegative_int(card.get("position")),
                    "date_moved": _positive_int(card.get("date_moved")),
                    "closed": not _task_is_active(card),
                    "metadata": meta,
                    "task_type": _text(meta.get("task_type")),
                    "project": _text(meta.get("project")),
                    "blocked_by": _text(meta.get("blocked_by")),
                    "head": head,
                    "effective_head": _text(meta.get("resolved_head")) or head or role_default_or_blank(default_head),
                    "review_head": review,
                    "effective_review_head": (
                        _text(meta.get("resolved_review_head")) or review or role_default_or_blank(reviewer_head)
                    ),
                    "claim": _text(meta.get("claim")),
                    "slug": _text(meta.get("slug")),
                    "base_branch": _text(meta.get("base_branch")),
                    "seed_ref": _text(meta.get("seed_ref")),
                    "supersedes": _text(meta.get("supersedes")),
                    "comments": [
                        {
                            "ts": _text(comment.get("date_creation")),
                            "text": _text(comment.get("comment")),
                        }
                        for comment in raw_comments
                        if isinstance(comment, dict)
                    ],
                }
            )
        return result

    def restore_snapshot(self) -> dict[str, dict[str, Any]]:
        """Read every active or archived card as a normalized, authoritative snapshot.

        Recovery uses this after its setup writes and again for final parity.  The
        board rows are one read and metadata/comments share bounded JSON-RPC posts;
        unlike ``show`` this never grows a pair of HTTP reads per card.
        """
        project_id, columns, swimlanes = self._board()
        rows = [row for row in all_project_cards(self.client, project_id) if isinstance(row, dict)]
        task_ids = [_task_number(row) for row in rows]
        answers = self.client.call_batch(
            (method, {"task_id": task_id})
            for task_id in task_ids
            for method in ("getTaskMetadata", "getAllComments")
        )
        result: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            raw_comments = answers[index * 2 + 1] or []
            if not isinstance(raw_comments, list):
                raise TaskError("backend_error", "board store returned invalid task comments", 1)
            card = self._normalize(
                row,
                columns,
                swimlanes,
                _task_metadata(answers[index * 2]),
                comments=[_normalize_comment(value) for value in raw_comments if isinstance(value, dict)],
            )
            previous = result.get(card["ref"])
            if previous is None or (not card.get("closed") and previous.get("closed")):
                result[card["ref"]] = card
        return result

    def show(self, reference: str) -> dict[str, Any]:
        project_id, columns, swimlanes = self._board()
        card = project_card_by_reference(self.client, project_id, reference)
        return self._show_card(card, columns, swimlanes)

    def sprint_e2e_budget(self, sprint_ref: str) -> dict[str, Any] | None:
        """`{budget, used, charges}` of a sprint's e2e run budget (secretary-1796), or None for no sprint."""
        return self.client.call("getSprintE2eBudget", sprint_ref=sprint_ref)

    def show_id(self, task_id: int) -> dict[str, Any]:
        """Return one row by board id, without resolving a duplicate reference."""
        project_id, columns, swimlanes = self._board()
        card = project_card_by_id(self.client, project_id, task_id)
        return self._show_card(card, columns, swimlanes)

    def _show_card(
        self,
        card: dict[str, Any] | None,
        columns: dict[int, str],
        swimlanes: dict[int, str],
    ) -> dict[str, Any]:
        if not isinstance(card, dict):
            raise TaskError("not_found", "task was not found", 2)
        task_id = _positive_int(card.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "board store returned an invalid task", 1)
        raw_comments = self.client.call("getAllComments", task_id=task_id) or []
        comments = [_normalize_comment(comment) for comment in raw_comments if isinstance(comment, dict)]
        shown = self._normalize(
            card,
            columns,
            swimlanes,
            _task_metadata(self.client.call("getTaskMetadata", task_id=task_id)),
            comments=comments,
        )
        self._attach_origin_returns([shown])
        return shown

    def _metadata_of(self, cards: list[dict[str, Any]]) -> dict[int, dict[str, str]]:
        """The task metadata of every given row, keyed by board task id, in one batched read."""
        task_ids = [_task_number(card) for card in cards]
        answers = self.client.call_batch(("getTaskMetadata", {"task_id": task_id}) for task_id in task_ids)
        return {task_id: _task_metadata(answer) for task_id, answer in zip(task_ids, answers, strict=True)}

    def _board(self) -> tuple[int, dict[int, str], dict[int, str]]:
        board = self.client.call("getProjectByName", name=self.board_name)
        if not isinstance(board, dict) or (project_id := _positive_int(board.get("id"))) is None:
            raise TaskError("backend_error", "Pipeline board is unavailable", 1)
        columns = {
            identifier: str(column.get("title") or "")
            for column in (self.client.call("getColumns", project_id=project_id) or [])
            if isinstance(column, dict) and (identifier := _positive_int(column.get("id"))) is not None
        }
        swimlanes = {
            identifier: str(swimlane.get("name") or "")
            for swimlane in (self.client.call("getActiveSwimlanes", project_id=project_id) or [])
            if isinstance(swimlane, dict) and (identifier := _positive_int(swimlane.get("id"))) is not None
        }
        return project_id, columns, swimlanes

    def _normalize(
        self,
        card: dict[str, Any],
        columns: dict[int, str],
        swimlanes: dict[int, str],
        meta: dict[str, str],
        *,
        comments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        task_id = _positive_int(card.get("id"))
        column = columns.get(_positive_int(card.get("column_id")) or -1)
        if task_id is None or column not in _STATE_BY_COLUMN:
            raise TaskError("backend_error", "board schema is invalid", 1)
        ref = _text(card.get("reference"))
        kind = BOARD_STORE_KIND
        result: dict[str, Any] = {
            "id": entity_id("task", task_id),
            "ref": ref,
            "title": _text(card.get("title")),
            "description": _text(card.get("description")),
            "state": _STATE_BY_COLUMN[column],
            "closed": _nonnegative_int(card.get("is_active", card.get("status", 1))) == 0,
            "position": _nonnegative_int(card.get("position")),
            **TaskMetadata.from_legacy(meta, codex_modes=_CODEX_LAUNCH_MODES).to_document_fields(),
            "audit": {
                "created_at": _rfc3339(card.get("date_creation")),
                "updated_at": _rfc3339(card.get("date_modification")),
                "backend": {"kind": kind, f"{kind}_task_id": task_id, "board": self.board_name},
            },
        }
        extensions = {key: value for key, value in meta.items() if key not in _KNOWN_METADATA}
        lane = swimlanes.get(_positive_int(card.get("swimlane_id")) or -1)
        if lane:
            extensions["swimlane"] = lane
        if extensions:
            result["extensions"] = {EXTENSION_BAG: extensions}
            # A card handed to the owner says so where a reader looks first, not only in the bag.
            if (mark := waiting_owner(result)) is not None:
                result["waiting_owner"] = mark
            # The production an operation card touches, beside the kind it belongs to.
            if (production := card_production(result)) is not None:
                result["touches_production"] = production
        # A wait card's one structured block: target, deadline, last observation, state.
        if (waiting := wait_card.wait_view(result)) is not None:
            result["wait"] = waiting
        # The PO session a delegated card came from, and where its results went (secretary-1792).
        if (origin := origin_field.origin_view(result)) is not None:
            result["origin"] = origin
        if (execution := execution_field.assignment(result)) is not None:
            result[execution_field.PO_EXECUTION] = execution.to_document()
        # The e2e runs the dispatcher dispatched for a code card, and their wait cards (secretary-1795).
        if (e2e := e2e_record.e2e_view(result)) is not None:
            result["e2e"] = e2e
        if comments is not None:
            result["comments"] = comments
        return result


def task_audit_for(client: Any, data_dir: str | os.PathLike[str] | None = None) -> Any:
    """The audit owner of a card client: `requests`/`board_events` (docs/BOARD_STORE.md §7.3).

    Cards have one implementation, so their audit has one owner.  The file journal under
    `<data>/board` is not a card audit owner any more: building one beside a card client read a
    journal nobody writes, which is how on 2026-09-10 a worker's `report:done` committed in SQL was
    never seen and the worker was declared stalled (sprint:1437, secretary-1614).  `data_dir` is
    accepted and ignored so the callers that still hand one in need not know that.
    """
    from ummanu.board.sql_audit import SqlTaskAudit

    return SqlTaskAudit(client)


class TaskWriter:
    """Protocol writes, role guards and normalized audit events."""

    def __init__(
        self,
        client: SqlCardClient,
        *,
        data_dir: str | os.PathLike[str],
        workspace: str | os.PathLike[str] | None = None,
    ) -> None:
        self.client = client
        self.reader = TaskReader(client)
        self.data_dir = Path(data_dir)
        self.instance_dir = Path(client.instance_dir).expanduser().resolve()
        self.audit = task_audit_for(client, data_dir)
        # Importing the concrete adapter here keeps the protocol leaves usable
        # by the legacy task reader while giving migrated writes the same audit
        # owner as generic control-plane operations.
        from ummanu.board.sql_host import SqlBoardHost

        self.board_host = SqlBoardHost(
            client,
            data_dir=os.fspath(data_dir),
            audit=self.audit,
        )
        self.workspace = Path(workspace) if workspace is not None else None
        self._redaction_cache: tuple[tuple[tuple[str, int, int, int], ...], tuple[str, ...]] | None = None

    def _redaction_values(self) -> tuple[str, ...]:
        """Open the catalog at most once while its on-disk inputs are unchanged."""
        root = self.instance_dir / "secrets"
        paths = [root / "catalog.yaml", root / "installation.key"]
        values_dir = root / "values"
        if values_dir.is_dir():
            paths.extend(sorted(path for path in values_dir.iterdir() if path.is_file()))
        fingerprint: list[tuple[str, int, int, int]] = []
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                fingerprint.append((str(path), -1, -1, -1))
            else:
                fingerprint.append((str(path), info.st_mtime_ns, info.st_size, info.st_mode))
        key = tuple(fingerprint)
        if self._redaction_cache is not None and self._redaction_cache[0] == key:
            return self._redaction_cache[1]
        from ummanu.secret_store import SecretStoreError, redaction_values

        try:
            values = redaction_values(self.instance_dir)
        except SecretStoreError as exc:
            raise TaskError(
                "backend_unavailable", f"board redaction configuration is unavailable: {exc}", 1
            ) from None
        self._redaction_cache = (key, values)
        return values

    def _redact_for_board(self, text: str) -> str:
        """Remove credentials before they reach either board or audit history.

        An interrupted archive keeps its retry body locally and every board comment is exported into the
        checkpoint, so scrubbing happens at the protocol boundary and both copies receive the same safe
        text. An explicit role-env override selects its external file, so ``UMMANU_RUNTIME_ENV_FILE``
        and ``TA_RUNTIME_ENV_FILE`` are scrubbed too.
        """
        # Delay this import to avoid the config/sprints import cycle.
        return redact(
            text,
            env_files=[
                runtime_env_path()
                if any(os.environ.get(name) for name in RUNTIME_ENV_FILE_ENVS)
                else self.instance_dir / "runtime.env"
            ],
            secret_values=self._redaction_values(),
        )

    def create(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        task_type: str,
        title: str,
        description: str = "",
        target: str = "ready",
        reference: str = "",
        blocked_by: str = "",
        head: str = "",
        review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        seed_ref: str = "",
        supersedes: str = "",
        complexity: str = TaskComplexity.STANDARD.value,
        family_preference: str = FamilyPreference.AUTO.value,
        codex_launch_mode: str = "",
        sprint: str = "",
        priority: str = "",
        budget_event: str = "",
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        review: str = "",
        live_impact: bool = False,
        touches_production: str = "",
        wait: Mapping[str, Any] | None = None,
        origin: Mapping[str, Any] | None = None,
        po_execution: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        restoring: bool = False,
    ) -> dict[str, Any]:
        """Create an ordinary task through the released admission contract.

        `wait` carries a wait card's create flags (`run`, `run_id`, `card`, `states`, `until`,
        `deadline`, `returns`, `transient_window`); every other kind takes none of them.

        `origin` is the PO turn the create runs in, `{session, request}` (secretary-1792): the CLI
        reads it from the turn's environment for `--role po` only, and any other role is refused one.
        It is written once, here, and part of the create's request identity.
        """
        return self._create(
            role=role,
            actor=actor,
            project=project,
            task_type=task_type,
            title=title,
            description=description,
            target=target,
            reference=reference,
            blocked_by=blocked_by,
            head=head,
            review_head=review_head,
            slug=slug,
            base_branch=base_branch,
            seed_ref=seed_ref,
            supersedes=supersedes,
            complexity=complexity,
            family_preference=family_preference,
            codex_launch_mode=codex_launch_mode,
            sprint=sprint,
            priority=priority,
            budget_event=budget_event,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason,
            review=review,
            live_impact=live_impact,
            touches_production=touches_production,
            wait=wait,
            origin=origin,
            po_execution=po_execution,
            request_id=request_id,
            restoring=restoring,
            steward_report=False,
        )

    def _create(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        task_type: str,
        title: str,
        description: str = "",
        target: str = "ready",
        reference: str = "",
        blocked_by: str = "",
        head: str = "",
        review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        seed_ref: str = "",
        supersedes: str = "",
        complexity: str = TaskComplexity.STANDARD.value,
        family_preference: str = FamilyPreference.AUTO.value,
        codex_launch_mode: str = "",
        sprint: str = "",
        priority: str = "",
        budget_event: str = "",
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        review: str = "",
        live_impact: bool = False,
        touches_production: str = "",
        wait: Mapping[str, Any] | None = None,
        origin: Mapping[str, Any] | None = None,
        po_execution: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        restoring: bool = False,
        steward_report: bool,
    ) -> dict[str, Any]:
        # Restore bypasses new-work admission only; all other guards still apply. The dispatcher creates
        # nothing but the wait card of a code card's e2e run (secretary-1795), the decision a spent e2e
        # budget needs (secretary-1796), the `code` hotfix of a red after-merge e2e run, under that
        # run's hotfix request id (secretary-1807), and the `operation` a refused production activation
        # needs, under its activation request id and touching its own project's production
        # (secretary-1824); every other kind it names is refused below, before anything is read.
        task_type = task_type.strip()
        activation_operation = task_type == TaskType.OPERATION.value and str(request_id or "").startswith(
            ACTIVATION_OPERATION_REQUEST_PREFIX
        )
        disposition_operation = task_type == TaskType.OPERATION.value and str(request_id or "").startswith(
            execution_field.DISPOSITION_PREFIX
        )
        dispatcher_creates = (
            task_type in {TaskType.WAIT.value, TaskType.DECISION.value}
            or (
                task_type == TaskType.CODE.value
                and str(request_id or "").startswith(e2e_record.AFTER_MERGE_HOTFIX_REQUEST_PREFIX)
            )
            or activation_operation
            or disposition_operation
        )
        role = self._role(
            role,
            CREATE_ROLES | {Role.DISPATCHER} if dispatcher_creates else CREATE_ROLES,
            actor=actor,
        )
        project = project.strip()
        title = title.strip() if restoring else self._redact_for_board(title.strip())
        description = description if restoring else self._redact_for_board(description)
        target = target.strip()
        reference = reference.strip()
        blocked_by = blocked_by.strip()
        head = head.strip()
        review_head = review_head.strip()
        slug = slug.strip()
        base_branch = base_branch.strip()
        seed_ref = seed_ref.strip()
        supersedes = supersedes.strip()
        complexity = complexity.strip() or TaskComplexity.STANDARD.value
        family_preference = family_preference.strip() or FamilyPreference.AUTO.value
        codex_launch_mode = codex_launch_mode.strip()
        sprint = sprint.strip()
        priority = priority.strip()
        budget_event = budget_event.strip()
        touches_production = touches_production.strip()
        sprint_override_reason = (
            sprint_override_reason.strip()
            if restoring
            else self._redact_for_board(sprint_override_reason.strip())
        )
        if not project:
            raise TaskError("validation", "create requires a non-empty project", 2)
        try:
            task_type_value = TaskType(task_type)
        except ValueError:
            known = ", ".join(sorted(TASK_TYPE_VALUES))
            raise TaskError("validation", f"unknown task type {task_type!r} (known: {known})", 2) from None
        task_type = task_type_value.value
        # Whether review runs is the review choice; who reviews is the reviewer head and the sprint
        # pin. The two are resolved independently, and only a caller contradicting itself is refused.
        review = review.strip()
        explicit_review_head = review_head
        if review:
            try:
                review_value = TaskReview(review)
            except ValueError:
                known = ", ".join(sorted(member.value for member in TaskReview))
                raise TaskError("validation", f"review must be one of: {known}", 2) from None
        else:
            review_value = default_review(task_type_value)
        review = review_value.value
        # The PO turn this create runs in: only the PO has one, and only its environment names it.
        origin_record = _origin_request(origin)
        execution_record = None
        if po_execution is not None:
            try:
                execution_record = execution_field.PoExecution.from_document(po_execution)
            except (ValueError, TypeError) as exc:
                raise TaskError("validation", str(exc), 2) from None
            supported = (
                task_type == "decision" and execution_record.purpose == "e2e_budget"
                and (str(request_id or "").startswith(e2e_budget.decision_prefix(execution_record.sources[0]))
                     or str(request_id or "").startswith("dispatcher-e2e-budget-")
                     or bool(e2e_budget.batch_decision_cards(str(request_id or ""))))
            ) or (disposition_operation and execution_record.purpose == "e2e_disposition")
            if (role != "dispatcher" or not supported or sprint or origin_record
                    or execution_record.request != request_id or execution_record.initial
                    or execution_record.executor or execution_record.successors):
                raise TaskError("validation", "PO execution assignment is only for a dispatcher e2e question without sprint or origin", 2)
            for source in execution_record.sources:
                source_card = self.reader.show(source)
                if source_card.get("type") != "code" or source_card.get("project") != project:
                    raise TaskError("validation", "PO e2e assignment sources must be code cards of this project", 2)
                if str(request_id or "").startswith("dispatcher-e2e-budget-") and not str(request_id).startswith(
                    e2e_budget.decision_prefix(str(source_card.get("sprint") or ""))
                ):
                    raise TaskError("validation", "standalone sprint-budget disposition must name its actual source sprint", 2)
        # The dispatcher carries a card's origin onto the decision its spent e2e cap needs, so the
        # decision goes to the PO session that cut the card (secretary-1796), and onto the hotfix `code`
        # card a red after-merge e2e run needs outside every sprint (secretary-1807); it originates
        # nothing else.
        carried_origin = role == Role.DISPATCHER.value and task_type in {
            TaskType.DECISION.value,
            TaskType.CODE.value,
            TaskType.OPERATION.value,
        }
        if origin_record and role != Role.PO.value and not carried_origin:
            raise TaskError(
                "validation", f"only the PO records the PO session a card came from; {role} cannot", 2
            )
        po_executed = task_type_value in PO_EXECUTED_TYPES
        waits = task_type_value is TaskType.WAIT
        # No head runs either: nothing to pin, no reservation to take, no override for the PO.
        headless = po_executed or waits
        if headless:
            refusal = _po_card_create_refusal(
                task_type,
                role=role,
                sprint=sprint,
                head=head,
                review_head=review_head,
                review=review_value,
                live_impact=live_impact,
                seed_ref=seed_ref,
                base_branch=base_branch,
                origin=bool(origin_record),
                activation_operation=activation_operation or disposition_operation,
                execution=execution_record is not None,
            )
            if refusal:
                raise TaskError("validation", refusal, 2)
        if role == Role.DISPATCHER.value and activation_operation and touches_production != project:
            raise TaskError(
                "validation",
                "the dispatcher's activation operation touches the production of its own project "
                f"{project!r}, not {touches_production or 'nothing'!r}",
                2,
            )
        wait_request = _wait_request(wait)
        spec: wait_card.WaitSpec | None = None
        if waits:
            # Inside a PO turn a wait with no --wait-return reports to the session of that turn.
            returns = wait_request.get("returns") or (
                [wait_card.PO_SESSION_PREFIX + origin_record["session"]] if origin_record else []
            )
            try:
                spec = wait_card.build_wait_spec(
                    **{**wait_request, "returns": returns}, sprint=sprint, now=datetime.now(UTC)
                )
            except wait_card.WaitSpecError as exc:
                raise TaskError("validation", str(exc), 2) from None
            # `card:<ref>` is the dispatcher's own address for the code card whose e2e run it waits for.
            if role != Role.DISPATCHER.value and any(wait_card.returned_card(a) for a in spec.returns):
                raise TaskError(
                    "validation", "a card:<ref> return address is the dispatcher's own; it is not a --wait-return", 2
                )
        elif wait_request:
            raise TaskError(
                "validation", f"the --wait-* flags belong to a wait card; a {task_type} card takes none", 2
            )
        # The production an operation card touches (secretary-1764), checked against the registry
        # `sprint create --allow-production` reads, and refused on every other kind.
        registered = None
        if task_type_value is TaskType.OPERATION and touches_production not in ("", NO_PRODUCTION):
            from ummanu.product_issues import registered_projects

            registered = registered_projects(self.instance_dir)
        if refusal := production_create_refusal(task_type, touches_production, registered):
            raise TaskError("validation", refusal, 2)
        if live_impact and task_type_value is not TaskType.RESEARCH:
            raise TaskError(
                "validation", f"--live-impact is a research attribute; a {task_type} card cannot carry it", 2
            )
        if live_impact and (bounds_refusal := impact_bounds_refusal(description)):
            raise TaskError("validation", bounds_refusal, 2)
        if not title:
            raise TaskError("validation", "create requires a non-empty title", 2)
        if target not in {"ready", "issues", "in_progress"}:
            raise TaskError("validation", "create target must be ready, issues or in_progress", 2)
        if target == "in_progress" and not (role == "steward" and steward_report):
            raise TaskError("transition_forbidden", "only a steward report may be created In progress", 3)
        if steward_report and (role != "steward" or target != "in_progress"):
            raise TaskError("role_forbidden", "steward report creation requires steward In progress", 3)
        if steward_report and (task_type != TaskType.RESEARCH.value or not slug or reference or sprint):
            raise TaskError(
                "validation",
                "a steward report requires research, a slug, no explicit reference and no sprint",
                2,
            )
        # A steward report was validated above; every other steward create is a proposal.
        if role in PROPOSAL_CREATE_ROLES and not steward_report:
            if target != "issues":
                raise TaskError("role_forbidden", f"{role} may create only proposals in Issues", 3)
        elif target == "issues":
            raise TaskError("transition_forbidden", "execution tasks cannot be created in Issues", 3)
        try:
            complexity_value = TaskComplexity(complexity)
        except ValueError:
            raise TaskError(
                "validation", "complexity must be one of: " + ", ".join(sorted(TASK_COMPLEXITY_VALUES)), 2
            ) from None
        complexity = complexity_value.value
        try:
            family_preference_value = FamilyPreference(family_preference)
        except ValueError:
            raise TaskError(
                "validation", "family preference must be one of: " + ", ".join(sorted(FAMILY_PREFERENCE_VALUES)), 2
            ) from None
        family_preference = family_preference_value.value
        if codex_launch_mode and codex_launch_mode not in _CODEX_LAUNCH_MODES:
            # Refuse unknown launch modes before any board call.
            known = ", ".join(sorted(_CODEX_LAUNCH_MODES))
            raise TaskError("validation", f"codex launch mode must be {known}", 2)
        if priority:
            raise TaskError("validation", "tasks do not accept product priority", 2)
        if slug and not _SLUG_RE.match(slug):
            raise TaskError("validation", "slug must match [a-z0-9-]{1,30}", 2)
        # Seed and integration base are two different refs (secretary-1541). A restore reproduces
        # cards that were admitted under the older single-field contract, so it carries whatever
        # they hold; the dispatcher refuses such a base fast and typed when it next runs the card.
        if not restoring:
            base_refusal = integration_base_refusal(base_branch) if base_branch else ""
            if base_refusal:
                raise TaskError("base_branch_not_integration_target", base_refusal, 2)
            seed_refusal = seed_ref_refusal(seed_ref) if seed_ref else ""
            if seed_refusal:
                raise TaskError("validation", seed_refusal, 2)
            if supersedes and not seed_ref:
                raise TaskError(
                    "validation", "supersedes names the predecessor of a seed; it requires --seed-ref", 2
                )
            if seed_ref and not supersedes:
                # A seed is inherited content, and whose content it is has to be readable off the
                # card: without it nobody can tell an intentional reslice from a stray ref.
                raise TaskError(
                    "validation",
                    "a seed requires --supersedes naming the predecessor card it inherits from",
                    2,
                )
        linked_sprint: dict[str, Any] | None = None
        if sprint:
            from ummanu.sprints import SprintReader

            linked_sprint = SprintReader(self.client).show(sprint, include_cards=False)
            if linked_sprint["status"] != "open":
                raise TaskError("closed", "cannot link a new card to a closed or stopped sprint", 3)
            # A wait card touches no repository, so no reservation admits or refuses it.
            if project not in linked_sprint.get("reservations", []) and not waits:
                raise TaskError(
                    "sprint_project_unreserved",
                    f"project {project!r} is not reserved by sprint {sprint}",
                    3,
                )
            # A PO-executed or wait card has no head to pin: the PO service or the dispatcher runs it.
            if not restoring and not headless:
                pinned_head, pinned_review = self._sprint_executor_pins(
                    sprint_ref=sprint,
                    head=head,
                    review_head=review_head,
                    sprint=linked_sprint,
                )
                head, review_head = pinned_head or "", pinned_review or ""
            if review_value is TaskReview.SKIPPED:
                self._refuse_unpinned_reviewer_on_skipped(
                    sprint_ref=sprint, review_head=explicit_review_head, sprint=linked_sprint
                )
        elif review_value is TaskReview.SKIPPED:
            self._refuse_unpinned_reviewer_on_skipped(sprint_ref="", review_head=explicit_review_head)
        if budget_event not in {"", "recreated_task", "hotfix"}:
            raise TaskError("validation", "budget event must be recreated_task or hotfix", 2)
        if budget_event and not sprint:
            raise TaskError("validation", "budget event requires a linked sprint", 2)
        if spec is not None:
            # The session of the running turn is not asked about: it is the one creating the card.
            self._refuse_unknown_po_sessions(
                session
                for session in wait_card.po_sessions(spec.returns)
                if not origin_record or session != origin_record["session"]
            )

        request_id = request_id or str(uuid.uuid4())
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=project,
            card_sprint=sprint,
            linked_sprint=linked_sprint,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason,
            request_id=request_id,
            reference=reference,
            steward_report=steward_report,
            po_card=headless,
        )
        # Admission follows ownership; Issues proposals and restores are not new work.
        # The PO may cut a card outside every sprint; the dispatcher decides at admission whether it runs.
        # The dispatcher's e2e wait follows its code card, which may belong to no sprint either.
        if (
            target == "ready"
            and not sprint
            and not restoring
            and not override_payload
            and role not in {"po", "dispatcher"}
        ):
            raise TaskError("validation", "task creation requires an open sprint", 2)
        payload: dict[str, Any] = {
            "project": project,
            "task_type": task_type,
            "target": target,
            "reference": reference or None,
            "blocked_by": blocked_by or None,
            "head": head or None,
            "review_head": review_head or None,
            "slug": slug or None,
            "base_branch": base_branch or None,
            "seed_ref": seed_ref or None,
            "supersedes": supersedes or None,
            "complexity": complexity,
            "family_preference": family_preference,
            "codex_launch_mode": codex_launch_mode or None,
            "sprint": sprint or None,
            "budget_event": budget_event or None,
            "review": review,
            **({"live_impact": True} if live_impact else {}),
            **({"touches_production": touches_production} if touches_production else {}),
            # The flags as given: a retry recomputes the same request, never the same clock.
            **({"wait_request": wait_request} if waits else {}),
            # Where a delegated card came from: part of the identity, so a replay is the same turn's.
            **({"po_origin": origin_record} if origin_record else {}),
            **({"po_execution": execution_record.to_document()} if execution_record else {}),
            **({"steward_report": True} if steward_report else {}),
            **override_payload,
            "title_sha256": _digest(title),
            "description_sha256": _digest(description),
        }
        # Allocate and stage automatic references under the board lock.
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind="created", reference=None, identity=payload)
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {
                "action": "created",
                "task": self.reader.show(str(committed["ref"])),
                "event_id": event_id,
                "replayed": True,
            }
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind="created", reference=None, identity=payload)
            try:
                self._finish_pending_cleanup(pending, None)
                task = self._pending_create_task(pending)
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {"action": "created", "task": task, "event_id": event_id, "replayed": True}

        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": "created",
            "outcome": "success",
            "task_id": "",
            "ref": reference,
            "backend": {
                "kind": BOARD_STORE_KIND,
                "task_id": None,
                "revision": "pending",
                "reference_assignment": "atomic",
            },
            "request_id": request_id,
            # The spec rides in the event beside the identity, so a pending create is repaired whole.
            "payload": {**payload, "wait_spec": spec.text()} if spec is not None else payload,
        }
        # One transaction from the claim to the committed record, where the backend has
        # transactions (§7.1).  The claim used to be committed on its own before the card was
        # written and the record committed after it, so the reference this create allocates was
        # named by a *later* transaction than the one that claimed the request id: on
        # PostgreSQL `requests.ref` stayed NULL for the whole life of the row.  Under one
        # transaction the claim, the card effect and the event stand or fall together, which is
        # what `docs/BOARD_STORE.md` §7.3 already says this backend does.  For a client without
        # transactions `_mutation` is nothing at all, so the behaviour there is unchanged.
        with self._mutation():
            self.audit.stage(request_id, event)
            try:
                created_ref = self._create_backend(
                    project=project,
                    task_type=task_type,
                    title=title,
                    description=description,
                    target=target,
                    reference=reference,
                    blocked_by=blocked_by,
                    head=head,
                    review_head=review_head,
                    slug=slug,
                    base_branch=base_branch,
                    seed_ref=seed_ref,
                    supersedes=supersedes,
                    complexity=complexity,
                    family_preference=family_preference,
                    codex_launch_mode=codex_launch_mode,
                    sprint=sprint,
                    review=review,
                    live_impact=live_impact,
                    touches_production=touches_production,
                    wait_spec=spec.text() if spec is not None else "",
                    po_origin=(
                        origin_field.origin_text(origin_record["session"], origin_record["request"])
                        if origin_record
                        else ""
                    ),
                    po_execution=execution_record.text() if execution_record else "",
                    steward_report=steward_report,
                    event=event,
                    request_id=request_id,
                )
                if supersedes:
                    try:
                        previous = self.reader.show(supersedes)
                    except TaskError as exc:
                        if exc.code != "not_found":
                            raise
                        previous = {}
                    if carries_mark_fields(previous) or attention_record(previous, OWNER_ESCALATION):
                        owner_events.settle_required_wait(supersedes, to=self.client)
                        self.client.call("saveTaskMetadata", task_id=_task_number(previous),
                                         values={**CLEAR_MARK, OWNER_ESCALATION: ""})
            except _CommittedWriteError:
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            except Exception:
                self.audit.discard(request_id)
                raise
            try:
                task = self.reader.show(created_ref)
            except Exception:  # noqa: BLE001 - any post-create read failure is an ambiguous commit.
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            event["task_id"] = task["id"]
            event["ref"] = created_ref
            event["backend"]["revision"] = _revision(task)
            self.audit.stage(request_id, event)
            try:
                event_id = self.audit.append(request_id, event)
            except OSError:
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": "created", "task": task, "event_id": event_id, "replayed": False}

    def create_steward_report(
        self,
        *,
        actor: str,
        project: str,
        title: str,
        slug: str,
        description: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create the steward's accounting artifact directly in In progress.

        The generic create transaction owns its reference reservation, staged
        identity and staged-claim recovery.  This narrow facade only supplies
        the invariant report shape; it never creates a temporary Ready card and
        deliberately does not require a sprint.
        """
        return self._create(
            role="steward",
            actor=actor,
            project=project,
            task_type=TaskType.RESEARCH.value,
            title=title,
            description=description,
            target="in_progress",
            slug=slug,
            steward_report=True,
            request_id=request_id,
        )

    def _create_backend(
        self,
        *,
        project: str,
        task_type: str,
        title: str,
        description: str,
        target: str,
        reference: str,
        blocked_by: str,
        head: str,
        review_head: str,
        slug: str,
        base_branch: str,
        seed_ref: str,
        supersedes: str,
        complexity: str,
        family_preference: str,
        codex_launch_mode: str,
        sprint: str,
        review: str,
        live_impact: bool,
        touches_production: str,
        steward_report: bool,
        event: dict[str, Any],
        request_id: str,
        wait_spec: str = "",
        po_origin: str = "",
        po_execution: str = "",
    ) -> str:
        # The board accepts duplicate references, so holding this lock from the high-water
        # read through createTask prevents two local task-create processes assigning one ref.
        with reference_allocation_lock(self.data_dir), self._mutation():
            board_id, columns, swimlanes = self.reader._board()
            created_ref = reference or next_project_reference(self.client, board_id, project)
            # One question for both paths. A caller-supplied reference may name a card that
            # already exists, and an allocated one is only as free as the enumeration it was
            # counted from, so neither is written before the backend is asked about that exact
            # reference. Archived rows answer too: they hold their reference for good.
            if project_card_by_reference(self.client, board_id, created_ref):
                raise TaskError("validation", f"task reference {created_ref} is already claimed", 2)
            column_id = _target_column_id(columns, target)
            if column_id is None:
                raise TaskError("backend_error", "board schema is invalid", 1)
            swimlane_id = _matching_swimlane(swimlanes, project)
            # Persist the allocation before the atomic backend write. A process that dies
            # after createTask still leaves a recoverable, already-reserved reference.
            event["ref"] = created_ref
            self.audit.stage(request_id, event)
            task_id = _positive_int(
                self.client.call(
                    "createTask",
                    project_id=board_id,
                    title=title,
                    description=description,
                    column_id=column_id,
                    swimlane_id=swimlane_id or 0,
                    reference=created_ref,
                )
            )
            if task_id is None:
                raise TaskError("backend_error", "board store rejected the write", 1)
            event["task_id"] = entity_id("task", task_id)
            event["backend"]["task_id"] = task_id
            try:
                self.audit.stage(request_id, event)
            except OSError as exc:
                raise _CommittedWriteError() from exc
            try:
                reference_persisted = self.reader.show_id(task_id)["ref"] == created_ref
            except Exception as exc:
                raise _CommittedWriteError() from exc
            if not reference_persisted:
                raise _CommittedWriteError()
            try:
                values = {
                    "record_type": "task",
                    "task_type": task_type,
                    "project": project,
                    "complexity": complexity,
                    "family_preference": family_preference,
                    "review": review,
                }
                if live_impact:
                    values["live_impact"] = "1"
                if touches_production:
                    # Not a column: a typed field of the extension bag (board/production_rights.py).
                    values[TOUCHES_PRODUCTION] = touches_production
                if wait_spec:
                    # Not a column either: the wait card's spec (board/wait_card.py).
                    values[wait_card.WAIT_SPEC] = wait_spec
                if po_origin:
                    # Nor the PO turn a delegated card came from (board/po_origin.py), written only here.
                    values[origin_field.PO_ORIGIN] = po_origin
                if po_execution:
                    values[execution_field.PO_EXECUTION] = po_execution
                if blocked_by:
                    values["blocked_by"] = blocked_by
                if head:
                    values["head"] = head
                if review_head:
                    values["review_head"] = review_head
                if slug:
                    values["slug"] = slug
                if base_branch:
                    values["base_branch"] = base_branch
                if seed_ref:
                    values["seed_ref"] = seed_ref
                if supersedes:
                    values["supersedes"] = supersedes
                if codex_launch_mode:
                    values["codex_launch_mode"] = codex_launch_mode
                if sprint:
                    values["sprint_ref"] = sprint
                if steward_report:
                    values.update({"record_type": "task", "claim": slug, "steward_report": "1"})
                self.client.call("saveTaskMetadata", task_id=task_id, values=values)
                if steward_report:
                    created = self.reader.show_id(task_id)
                    if not (
                        created["state"] == "in_progress"
                        and created["project"] == project
                        and created["type"] == "research"
                        and created.get("record_type") == "task"
                        and created["claim"]["worker"] == slug
                        and _is_steward_report(created)
                    ):
                        raise _CommittedWriteError()
            except Exception as exc:
                raise _CommittedWriteError() from exc
            return created_ref

    def comment(
        self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None
    ) -> dict[str, Any]:
        """One role comment on a card. `owner` comments too, on any card, always as actor `owner`.

        The owner is not a board role (it moves nothing and creates nothing); its comment is how it
        answers a card the PO handed to it (`handover`), and the dispatcher reads it back by its
        `owner` marker.
        """
        if str(role) == OWNER_ROLE:
            role, actor = OWNER_ROLE, OWNER_ROLE
        else:
            role = self._role(role, COMMENT_ROLES, actor=actor)
        body = self._redact_for_board(body)
        payload = {"marker": role, "body_sha256": _digest(body)}

        def mutation(task: dict[str, Any]) -> None:
            self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{body}")
            if role == OWNER_ROLE and body.strip() and waiting_owner(task) is not None:
                handover = current_handover(self.audit.events(reference))
                if handover is None:
                    raise TaskError("validation", "owner mark has no audited handover", 2)
                occurrence = self.audit.pending_event(request_id)
                self._store_owner_answer(task, handover, body, occurrence)

        # Fix the request identity before staging so the answer can name its own audit event.
        request_id = request_id or str(uuid.uuid4())
        return self._write(
            "commented",
            role,
            actor,
            reference,
            request_id,
            payload,
            mutation,
            identity=payload,
        )

    def _store_owner_answer(self, task: dict[str, Any], handover: Any, quotation: str, occurrence: Any,
                            *, comments: Any = None) -> None:
        mark = waiting_owner(task)
        if not quotation.strip():
            raise TaskError("empty_owner_answer", "empty owner comment is not an answer", 2)
        if (mark is None or occurrence is None or task.get("state") != "in_progress"
                or task.get("closed") or self._card_superseded(task["ref"])):
            raise TaskError("validation", "answer requires a current unanswered handover", 2)
        if (handover.get("payload") or {}).get("waiting_owner") != mark["since"]:
            raise TaskError("validation", "owner mark does not match the audited handover epoch", 2)
        answer = {"handover_event": handover["event_id"], "event_id": occurrence["event_id"],
                  "quotation": quotation, "mark": mark,
                  "po_session": (handover.get("payload") or {}).get(PO_SESSION_KEY, ""),
                  "at": occurrence["occurred_at"],
                  "channel": "comment" if occurrence["kind"] == "commented" else "conversation"}
        if comments is not None:
            answer["comments"] = comments
        owner_events.settle_required_wait(task["ref"], to=self.client)
        self.client.call("saveTaskMetadata", task_id=_task_number(task),
                         values={**CLEAR_MARK, OWNER_ESCALATION: "", OWNER_ANSWER: json.dumps(answer)})

    def record_owner_answer(self, *, role: str, actor: str, reference: str, handover_event: str,
                            quotation: str, request_id: str | None = None) -> dict[str, Any]:
        """PO records a verbatim conversation answer to one handover; it grants nothing itself."""
        role = self._role(role, {Role.PO}, actor=actor)
        quotation = self._redact_for_board(quotation)
        if not quotation.strip() or not handover_event.strip():
            raise TaskError("validation", "answer needs a non-empty owner quotation and handover event ID", 2)
        request_id = request_id or str(uuid.uuid4())
        identity = {"handover_event": handover_event, "quotation_sha256": _digest(quotation)}

        def mutation(task: dict[str, Any]) -> None:
            events = self.audit.events(reference)
            handover = current_handover(events)
            if owner_answer_event_ids(events) and any(comment["body"].strip() for comment in
                    owner_comments_since_handover(task.get("comments") or [])):
                raise TaskError("validation", "this handover already has an owner answer; accept that comment before a new handover", 2)
            if (task.get("closed") or task.get("state") != "in_progress" or handover is None
                    or handover["event_id"] != handover_event):
                raise TaskError("validation", "answer must name this card's current handover", 2)
            self._store_owner_answer(task, handover, quotation, self.audit.pending_event(request_id))
            self.client.call("createComment", task_id=_task_number(task), user_id=0,
                             content=f"[po]\n[owner-answer:{handover_event}]\n\n{quotation}")

        return self._write(OWNER_ANSWER_RECORDED, role, actor, reference, request_id, identity,
                           mutation, identity=identity)

    def accept_owner_comment(self, *, actor: str, reference: str, event_id: str) -> dict[str, Any]:
        """Released owner-comment follow-ups: settle their real answer before delivery.

        Only the dispatcher's recovery path uses this adapter. The owner audit marker and
        exact body digest must match a comment after the current audited handover.
        """
        role = self._role("dispatcher", {Role.DISPATCHER}, actor=actor)
        identity = {"owner_comment": event_id}
        request_id = f"owner-comment-answer-{event_id}"

        def mutation(task: dict[str, Any]) -> None:
            events = self.audit.events(reference)
            handover = current_handover(events)
            if handover is None:
                raise TaskError("validation", "no audited handover for this owner comment", 2)
            after = False
            event = None
            for item in events:
                if item.get("event_id") == handover["event_id"]:
                    after = True
                elif after and item.get("event_id") == event_id and item.get("kind") == "commented" and (item.get("payload") or {}).get("marker") == OWNER_ROLE:
                    event = item
            if event is None:
                raise TaskError("validation", "comment is not an owner answer after this handover", 2)
            for comment in task.get("comments") or []:
                text = str(comment.get("body") or "")
                if comment.get("marker") == OWNER_ROLE and text.startswith("[owner]\n"):
                    text = text[len("[owner]\n"):]
                    if _digest(text) == event["payload"].get("body_sha256"):
                        self._store_owner_answer(task, handover, text, event,
                            comments=owner_comments_since_handover(task.get("comments") or []))
                        return
            raise TaskError("validation", "audited owner quotation is unavailable", 2)

        return self._write("owner_comment_accepted", role, actor, reference, request_id, identity,
                           mutation, identity=identity)

    def _card_superseded(self, reference: str) -> bool:
        return bool(getattr(self.client, "credentials", None) and self.client._query(
            "SELECT 1 FROM task_supersessions WHERE supersedes = %s", (reference,)))

    def escalate_po_card(self, *, actor: str, reference: str, episode: str, reason: str) -> dict[str, Any]:
        """Dispatcher escalates an unresolved persisted PO episode, atomically with its bell."""
        role = self._role("dispatcher", {Role.DISPATCHER}, actor=actor)
        if not reason.strip() or not episode.strip():
            raise TaskError("validation", "escalation requires an episode and explicit reason", 2)
        identity = {"episode": episode}
        request_id = f"po-escalation-{episode}"

        def mutation(task: dict[str, Any]) -> None:
            claim = po_episode(self.audit.events(reference))
            answer = attention_record(task, OWNER_ANSWER)
            expected = answer["event_id"] if answer else (claim["event_id"] if claim else "")
            if (self._card_superseded(reference) or not is_po_executed(task) or task.get("closed") or task.get("state") != "in_progress"
                    or waiting_owner(task) is not None or expected != episode):
                raise TaskError("attention_resolved", "PO episode was resolved or replaced", 3)
            owner_events.record_required_wait(owner_events.PO_CARD_ESCALATED, reference, reason,
                                             f"po_card_escalated:{reference}:{episode}", to=self.client)
            self.client.call("saveTaskMetadata", task_id=_task_number(task),
                             values={OWNER_ESCALATION: json.dumps({"episode": episode, "reason": reason})})

        return self._write("po_card_escalated", role, actor, reference, request_id,
                           {**identity, "reason": reason}, mutation, identity=identity)

    def post_merge_ci(
        self,
        *,
        actor: str,
        reference: str,
        body: str,
        fact: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Record a release's post-merge CI result as one dispatcher comment on the card.

        The comment carries the fact in its event payload, which is what the observer wake predicate
        reads (`post_merge_ci_fact`); the body is the same fact for a human reading the card.
        """
        role = self._role("dispatcher", COMMENT_ROLES, actor=actor)
        if str(fact.get("result") or "") not in POST_MERGE_CI_RESULTS:
            raise TaskError("validation", "post-merge CI result must be one of " + ", ".join(POST_MERGE_CI_RESULTS), 2)
        body = self._redact_for_board(body)
        payload = {"marker": role, "body_sha256": _digest(body), POST_MERGE_CI_KEY: dict(fact)}
        return self._write(
            "commented",
            role,
            actor,
            reference,
            request_id,
            payload,
            lambda task: self.client.call(
                "createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{body}"
            ),
            identity=payload,
        )

    def _research_report_refusal(self) -> str:
        """The research report directory check, over the checkout the worker reports from."""
        if self.workspace is not None:
            return research_report_refusal(self.workspace)
        try:
            return research_report_refusal(Path.cwd())
        except OSError:
            return ""

    def _require_committed_workspace(self) -> None:
        """Refuse a done report from a dirty checkout.

        The worker runs the protocol from its own workspace, so failing here lets it commit and retry
        inside the same session instead of learning from the dispatcher that its card went to blocked.
        """
        if self.workspace is not None:
            workspace: Path = self.workspace
        else:
            try:
                workspace = Path.cwd()
            except OSError:
                return
        dirt = workspace_dirt(workspace)
        if not dirt:
            return
        shown = [line[3:].strip().strip('"') for line in dirt[:10]]
        files = ", ".join(shown)
        if len(dirt) > len(shown):
            files += f", +{len(dirt) - len(shown)} more"
        raise TaskError(
            "uncommitted", f"workspace has uncommitted changes: {files}; commit them and retry", 3
        )

    def report(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        kind: str,
        body: str,
        classification: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """A worker's report, and for a blocked one the kind of blocker it hit.

        The classification is required rather than offered: an external fact and a wrong task definition
        are repaired by different people. Two values and no free text, so repeated blocks from one head
        are countable. Its payload is staged as one typed Card occurrence which renders the
        `classification:` line, deliberately not card metadata that could disagree with the event.
        """
        role = self._role(role, {Role.WORKER}, actor=actor)
        body = self._redact_for_board(body)
        if kind not in {"done", "blocked"} or not body.strip():
            raise TaskError("validation", "reports require a non-empty body", 2)
        classification = classification.strip()
        if kind == "blocked" and classification not in BLOCK_CLASSIFICATION_VALUES:
            raise TaskError(
                "validation",
                "blocked reports require --classification, one of " + ", ".join(BLOCK_CLASSIFICATION_VALUES),
                2,
            )
        if kind == "done" and classification:
            raise TaskError("validation", "a done report carries no classification", 2)
        classification_value = BlockClassification(classification) if classification else None
        request_id = request_id or str(uuid.uuid4())
        # Resolve immutable ownership before either fresh admission or a card
        # read.  A replay must stay a pure replay, including when its worker
        # checkout has since become dirty or the card is no longer readable.
        legacy_owned = False
        try:
            owned = self.board_host.canon.event(request_id)
        except ValueError:
            owned = None
            legacy_owned = (
                self.audit.committed_event(request_id) is not None
                or self.audit.pending_event(request_id) is not None
            )
            if not legacy_owned:
                raise
        marker_data = owned.data if owned is not None else {}
        if owned is None and not legacy_owned:
            # This is the writer boundary for a worker report.  Bind the
            # report to the specification it actually answered now, rather
            # than asking a later terminal projection to guess from a mutable
            # card description.
            current = self.reader.show(reference)
            if kind == "done":
                if has_candidate(current):
                    self._require_committed_workspace()
                elif current.get("type") == "infra":
                    # A research/infra card has no candidate, so its checkout may hold uncommitted
                    # artifacts; an infra report carries its completion record instead.
                    _fields, refusal = infra_report_fields(body)
                    if refusal:
                        raise TaskError("validation", refusal, 2)
                elif current.get("type") == "research":
                    refusal = self._research_report_refusal()
                    if refusal:
                        raise TaskError("validation", refusal, 2)
            revision = specification_revision(self.audit.events(reference), current["description"])
            specification_data = {
                "description_sha256": _digest(current["description"]),
                "specification_revision": revision or None,
            }
        else:
            # Released marker records remain replayable without being
            # rewritten into the forward-lineage shape.
            specification_data = {
                name: marker_data[name]
                for name in ("description_sha256", "specification_revision")
                if name in marker_data
            }
        return self._marker_write(
            action="reported",
            event_kind=EventKind.CARD_REPORTED,
            role=role,
            actor=actor,
            reference=reference,
            reason=body,
            request_id=request_id,
            data={
                "marker": f"report:{kind}",
                "status": kind,
                "body": body,
                "body_sha256": _digest(body),
                **specification_data,
                "classification": classification_value.value if classification_value is not None else None,
            },
            fresh_admission=None,
        )

    def complete(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        kind: str,
        body: str,
        request_id: str | None = None,
        po_session: str = "",
    ) -> dict[str, Any]:
        """The PO completes a `decision`/`operation` card it answered in its turn.

        `po_session` is the PO session whose turn runs the command (the CLI reads it from
        `UMMANU_PO_SESSION`), recorded in the completion transition's data as `po_session`. It
        permits nothing: it is the proof a delegated card's result return reads (secretary-1792).

        One Card transition In progress -> Done whose reason is the rendered completion record; the
        transition writes that record as the PO's comment inside its own transaction, so the
        `[completion:<kind>]` comment and the Done land together or not at all. The request id makes
        it idempotent: a repeat answers the recorded transition and writes nothing, and the same id
        with another record is a conflict. Every refusal is decided before anything is written.

        The sprint guard is not asked: this is the PO executing the card its sprint's dispatcher
        submitted to it, not the PO's escape-hatch move of a sprint's card.
        """
        role = self._role(role, {Role.PO}, actor=actor)
        if kind not in PO_COMPLETION_MARKERS:
            known = ", ".join(sorted(PO_COMPLETION_MARKERS))
            raise TaskError("validation", f"task complete takes --kind {known}, not {kind!r}", 2)
        body = self._redact_for_board(body)
        fields, refusal = po_completion_fields(kind, body)
        if refusal:
            raise TaskError("validation", refusal, 2)
        record = render_po_completion_record(kind, fields)
        request_id = request_id or str(uuid.uuid4())
        task = self.reader.show(reference)
        existing = self._typed_event(request_id)
        if existing is not None:
            if str(existing.ref) != reference or existing.reason != record:
                raise TaskError(
                    "request_conflict",
                    f"request id {request_id!r} already completed {existing.ref} with another record; "
                    "repeat a completion only with the same card and body",
                    3,
                )
            result = self._transition_card(
                reference=reference,
                target=CardState.DONE,
                role=role,
                actor=actor,
                reason=record,
                request_id=request_id,
                # A replay repeats the recorded completion, whichever session repeats it.
                po_session=str((getattr(existing, "data", None) or {}).get(PO_SESSION_KEY) or ""),
                finish=self._transition_cleanup(
                    task, source=str(existing.source_state or ""), target="done", reason="", role=role
                ),
            )
            return {
                "action": "completed",
                "task": self.reader.show(reference),
                "event_id": result.event.event_id,
                "replayed": True,
            }
        _check_execution_record(task)
        if str(task.get("type") or "") != kind:
            raise TaskError(
                "validation",
                f"{reference} is a {task.get('type') or 'typeless'} card; task complete --kind {kind} "
                f"completes only a {kind} card",
                2,
            )
        if task["state"] != CardState.IN_PROGRESS.value:
            raise TaskError(
                "transition_forbidden",
                f"task complete needs the card In progress, where the dispatcher put it when it "
                f"submitted it to the PO; {reference} is {task['state']}",
                3,
            )
        result = self._transition_card(
            reference=reference,
            target=CardState.DONE,
            role=role,
            actor=actor,
            reason=record,
            request_id=request_id,
            po_session=po_session.strip(),
            finish=self._transition_cleanup(
                task, source=task["state"], target="done", reason=record, role=role
            ),
        )
        return {
            "action": "completed",
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": False,
        }

    def handover(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        to: str,
        reason: str,
        request_id: str | None = None,
        po_session: str = "",
    ) -> dict[str, Any]:
        """The PO hands an In progress `decision`/`operation` card to the owner (secretary-1761).

        `po_session`, the session whose turn runs the command, is recorded in the handover record's
        payload as `po_session`, as `complete` records it (secretary-1792); it permits nothing.

        One write: the `waiting_owner` mark on the card (`board.owner_handover`) and a PO comment
        `[handover:owner]` with the reason, the required unread owner event and predecessor PO-wait
        settlement, in the transaction of one `handed_to_owner` audit record. They land together
        or not at all. The card stays In progress. The request id makes it
        idempotent: a repeat answers the recorded handover and writes nothing, and the same id with
        another card or reason is refused. Every refusal (role, recipient, reason, kind, column, a
        mark already there) is decided before anything is written; the card ones are decided on the
        card as read inside that transaction.
        """
        role = self._role(role, {Role.PO}, actor=actor)
        if to != OWNER:
            raise TaskError("validation", f"a card is handed to the {OWNER}, not to {to!r}", 2)
        reason = self._redact_for_board(reason).strip()
        if not reason:
            raise TaskError("validation", "a handover needs a reason: what the owner has to decide or do", 2)
        request_id = request_id or str(uuid.uuid4())
        since = _now()
        identity = {"to": OWNER, "reason_sha256": _digest(reason)}

        def payload(task: dict[str, Any]) -> dict[str, Any]:
            _check_execution_record(task)
            kind = str(task.get("type") or "")
            if not is_po_executed(task):
                raise TaskError(
                    "validation",
                    f"{reference} is a {kind or 'typeless'} card; only a decision or operation card is "
                    "handed to the owner",
                    2,
                )
            if task["state"] != CardState.IN_PROGRESS.value:
                raise TaskError(
                    "transition_forbidden",
                    f"task handover needs the card In progress, where the PO answers it; {reference} is "
                    f"{task['state']}",
                    3,
                )
            if carries_mark_fields(task):
                held = waiting_owner(task) or {}
                raise TaskError(
                    "already_handed_over",
                    f"{reference} is already handed to the owner"
                    + (f" since {held['since']}: {held['reason']}" if held else ""),
                    3,
                )
            return {
                "marker": role.value,
                **identity,
                "kind": kind,
                "sprint": task.get("sprint"),
                "waiting_owner": since,
                **({PO_SESSION_KEY: po_session.strip()} if po_session.strip() else {}),
            }

        def mutation(task: dict[str, Any]) -> None:
            number = _task_number(task)
            if self._card_superseded(reference):
                raise TaskError("attention_resolved", "superseded card cannot open a new owner turn", 3)
            occurrence = self.audit.pending_event(request_id)
            if occurrence is None:
                raise TaskError("backend_error", "handover has no staged occurrence", 1)
            owner_events.record_required_wait(
                owner_events.CARD_HANDED_TO_OWNER,
                reference,
                f"{reference} is handed to the owner: {reason}",
                f"{owner_events.CARD_HANDED_TO_OWNER}:{reference}:{occurrence['event_id']}",
                to=self.client,
            )
            owner_events.settle_required_wait(reference, to=self.client, kind=owner_events.PO_CARD_ESCALATED)
            self.client.call("saveTaskMetadata", task_id=number, values={**mark_values(since, reason, actor), OWNER_ANSWER: "", OWNER_ESCALATION: ""})
            self.client.call(
                "createComment",
                task_id=number,
                user_id=0,
                content=f"[{role.value}]\n{render_handover_comment(reason)}",
            )

        return self._write(
            HANDED_TO_OWNER, role, actor, reference, request_id, payload, mutation, identity=identity
        )

    def cancel(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a pending wait card: the PO, or the observer of the card's own sprint (secretary-1790).

        One write: the `wait_cancel` field (who, when, why) and a `[wait:cancel]` comment with the
        reason, in the transaction of one `wait_cancelled` audit record. The card does not move here:
        the dispatcher freezes `cancelled` as the wait's result on its next tick, delivers it to every
        return address and then Blocks the card, as it does every other outcome. A wait whose result
        is already frozen is not cancelled; that result stands. Idempotent under the request id.
        """
        role = self._role(role, {Role.PO, Role.OBSERVER}, actor=actor)
        reason = self._redact_for_board(reason).strip()
        if not reason:
            raise TaskError("validation", "a cancel needs a non-empty reason (--reason-file)", 2)
        identity = {"reason_sha256": _digest(reason)}
        current = self.reader.show(reference)
        if role is Role.OBSERVER:
            if not str(current.get("sprint") or ""):
                raise TaskError("role_forbidden", "the observer cancels only a wait card of its own sprint", 3)
            self._guard_observer_identity(
                role=role.value,
                actor=actor,
                project=str(current.get("project") or ""),
                card_sprint=str(current.get("sprint") or ""),
                request_id=request_id or "",
                reference=reference,
            )
        at = _now()

        def payload(task: dict[str, Any]) -> dict[str, Any]:
            _check_execution_record(task)
            if not is_wait(task):
                raise TaskError(
                    "validation", f"{reference} is a {task.get('type') or 'typeless'} card; only a wait card is cancelled", 2
                )
            if task["state"] not in {CardState.READY.value, CardState.IN_PROGRESS.value}:
                raise TaskError(
                    "transition_forbidden", f"a wait card is cancelled while it waits; {reference} is {task['state']}", 3
                )
            if (held := wait_card.wait_cancel(task)) is not None:
                raise TaskError(
                    "already_cancelled", f"{reference} was cancelled at {held['at']}: {held['reason']}", 3
                )
            if (result := wait_card.wait_state(task).result) is not None:
                raise TaskError(
                    "already_settled", f"{reference} already has its result ({result.get('outcome')}); it stands", 3
                )
            return {"marker": role.value, **identity, "sprint": task.get("sprint"), "wait_cancel": at}

        def mutation(task: dict[str, Any]) -> None:
            number = _task_number(task)
            self.client.call(
                "saveTaskMetadata",
                task_id=number,
                values={wait_card.WAIT_CANCEL: wait_card.cancel_text(at, actor, role.value, reason)},
            )
            self.client.call(
                "createComment", task_id=number, user_id=0, content=f"[{role.value}]\n[wait:cancel]\n\n{reason}\n"
            )

        return self._write(WAIT_CANCELLED, role, actor, reference, request_id, payload, mutation, identity=identity)

    def record_wait_state(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """The dispatcher's one write of a wait card's `wait_state` (secretary-1790).

        A state field, not an event: the dispatcher rewrites it only when what it knows changed (a
        new observation or error, the frozen result, a delivery record), and it carries no audit
        record of its own. The result it freezes and every delivery it records are what the audit
        records elsewhere (the terminal move, the dependents' comments, the PO input).
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        if not is_wait(task):
            raise TaskError("validation", f"{reference} is not a wait card; it carries no wait state", 2)
        with self._mutation():
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values={wait_card.WAIT_STATE: state})

    def record_po_return(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """The dispatcher's one write of a delegated card's `po_return` (secretary-1792).

        A state field like `wait_state`: the session it handed a decision/operation card to, the
        successors of the origin's line, and one record per terminal transition whose result it
        returned. The delivery itself is recorded elsewhere (the PO input, the owner event). The
        origin (`po_origin`) is not touched here or anywhere after create.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        if origin_field.po_origin(task) is None:
            raise TaskError("validation", f"{reference} names no PO origin; it carries no return state", 2)
        with self._mutation():
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={origin_field.PO_RETURN: state}
            )

    def record_po_execution(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """Persist service resolution on an explicitly assigned PO card; preserve create identity."""
        self._role(role, {Role.DISPATCHER}, actor=actor)
        offered = execution_field.PoExecution.from_document(json.loads(state))
        with self._mutation():
            if getattr(self.client, "credentials", None):
                self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (reference,))
            task = self.reader.show(reference)
            current = execution_field.assignment(task)
            if current is None or (current.request, current.purpose, current.sources) != (
                offered.request, offered.purpose, offered.sources
            ):
                raise TaskError("validation", "PO execution resolution cannot change assignment identity", 2)
            # Resolution is monotone: a stale tick cannot lose another tick's committed
            # session/choice or a successor already accepted under its stable service ID.
            def resolved(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
                return {**new, **{key: value for key, value in old.items() if value or key not in new}}
            offered.initial = resolved(current.initial, offered.initial)
            for closed, row in current.successors.items():
                offered.successors[closed] = resolved(row, offered.successors.get(closed, {}))
            if current.executor and current.executor != offered.executor:
                # Preserve a newer executor if the offered session precedes it in this line.
                from ummanu.board.po_origin import ReturnState, line_head
                offered.executor = line_head(offered.executor or current.executor,
                                             ReturnState(successors=offered.successors))
            self.client.call("saveTaskMetadata", task_id=_task_number(task),
                             values={execution_field.PO_EXECUTION: offered.text()})

    def record_e2e_state(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """The dispatcher's one write of a code card's `e2e` run records (secretary-1795).

        A state field like `wait_state`: the intent of each dispatch (written before the call), the
        run it identified, its wait card and the wait's frozen result. The effects it leads to (the
        wait card's create, the card's rework or Blocked move, the dispatcher's comments) carry their
        own audit records.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        # A card of unknown kind reads as code, as the stage reads it.
        if str(task.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
            raise TaskError("validation", f"{reference} is not a code card; it carries no e2e runs", 2)
        with self._mutation():
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={e2e_record.E2E_FIELD: state}
            )

    def record_e2e_intent(
        self, *, role: str, actor: str, reference: str, state: str, sprint: str, dispatch_id: str
    ) -> dict[str, Any]:
        """Write a dispatch intent, charging the run to the card's sprint first (secretary-1796).

        One transaction: the conditional charge on the sprint row (`e2e_used = e2e_used + 1` only while
        `e2e_used < e2e_budget`) and the card's `e2e` field with the intent in it. When the budget has
        no run left nothing is written, and the answer says so (`charged: false`, with the budget and
        the runs used). A card outside every sprint (`sprint` empty) is not charged here: its per-card
        cap is counted from its own records, and the intent is written as by `record_e2e_state`.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        if str(task.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
            raise TaskError("validation", f"{reference} is not a code card; it carries no e2e runs", 2)
        with self._mutation():
            charge: dict[str, Any] = {"charged": True}
            if sprint:
                charge = self.client.call(
                    "chargeSprintE2e",
                    sprint_ref=sprint,
                    task_ref=reference,
                    dispatch_id=dispatch_id,
                    at=datetime.now(UTC).isoformat(),
                )
                if not charge.get("charged"):
                    return charge
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={e2e_record.E2E_FIELD: state}
            )
            return charge

    def record_after_merge_intent(
        self,
        *,
        role: str,
        actor: str,
        states: dict[str, str],
        sprint: str,
        dispatch_id: str,
        carrier: str,
    ) -> dict[str, Any]:
        """Write an after-merge e2e run's intent on every card it covers, charged first (secretary-1807).

        One transaction for the whole run: the charge, then each covered card's `e2e` field (`states`,
        the new text per card; the carrier's holds the run record). With `sprint` the run is charged to
        that sprint as `record_e2e_intent` charges one, under the carrier's name. Without it the run is
        charged to every covered card's own cap: each is read here, and when any of them has no run
        left nothing at all is written and the answer names them (`charged: false`, `spent`). All
        charged together, or none.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        if carrier not in states:
            raise TaskError("validation", f"the carrier {carrier} is not among the covered cards", 2)
        tasks = {reference: self.reader.show(reference) for reference in states}
        for reference, task in tasks.items():
            if str(task.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
                raise TaskError("validation", f"{reference} is not a code card; it carries no e2e runs", 2)
        with self._mutation():
            charge: dict[str, Any] = {"charged": True}
            if sprint:
                charge = self.client.call(
                    "chargeSprintE2e",
                    sprint_ref=sprint,
                    task_ref=carrier,
                    dispatch_id=dispatch_id,
                    at=datetime.now(UTC).isoformat(),
                )
                if not charge.get("charged"):
                    return charge
            else:
                spent = [
                    reference
                    for reference, task in tasks.items()
                    if e2e_record.e2e_state(task).dispatched >= e2e_budget.card_cap(task)
                ]
                if spent:
                    return {"charged": False, "spent": spent}
            for reference, text in states.items():
                self.client.call(
                    "saveTaskMetadata", task_id=_task_number(tasks[reference]), values={e2e_record.E2E_FIELD: text}
                )
            return charge

    def _sprint_open(self, sprint: str) -> bool:
        """Whether a card's sprint is open: a card of a closed sprint spends its own e2e cap after the
        merge (secretary-1807)."""
        from ummanu.sprints import SprintReader

        try:
            return str(SprintReader(self.client).show(sprint, include_cards=False).get("status") or "") == "open"
        except TaskError as exc:
            if exc.code == "not_found":
                return False
            raise

    def raise_e2e_cap(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        authorized_by: str,
        add: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Raise the e2e cap of one code card outside every sprint, on the owner's word (secretary-1796).

        The sprint's `sprint e2e-budget`, for a card no open sprint budgets: role `po` only, and only with
        `authorized_by`, the event id of an owner-role comment on this card's e2e budget decision card
        made after its handover whose one answer line is `e2e budget: raise <N>`
        (`e2e_budget.authorized_raise`): the raise is that N, and `add`, when given, has to equal it. It
        is appended to the
        card's `e2e_cap` field in the transaction of one `e2e_cap_raised` audit record naming that
        event. One authorizing comment raises once; everything is refused before anything is written.
        """
        role = self._role(role, {Role.PO}, actor=actor)
        if add is not None and (isinstance(add, bool) or not isinstance(add, int) or add < 1):
            raise TaskError("validation", f"--add is a whole number of runs, 1 or more; not {add!r}", 2)
        authorized_by = str(authorized_by or "").strip()
        current = self.reader.show(reference)
        if str(current.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
            raise TaskError("validation", f"{reference} is not a code card; it has no e2e cap", 2)
        if str(current.get("sprint") or "") and self._sprint_open(str(current["sprint"])):
            raise TaskError(
                "validation",
                f"{reference} belongs to {current['sprint']}, whose e2e budget it spends: raise that with "
                "`sprint e2e-budget`",
                2,
            )
        decision, add = e2e_budget.authorized_raise(self.audit, self.reader, authorized_by, reference, add)
        request_id = request_id or f"e2e-cap-raise-{authorized_by}"
        identity = {"add": add, "authorized_by": authorized_by}
        if self.audit.event(request_id) is None:
            for event in self.audit.events(reference, kind=e2e_budget.CARD_CAP_RAISED):
                if (event.get("payload") or {}).get("authorized_by") == authorized_by:
                    raise TaskError(
                        "authorization_refused",
                        f"the owner's comment {authorized_by} already authorized a raise of {reference}'s e2e "
                        f"cap ({event.get('request_id')}); each answer raises once",
                        3,
                    )
        at = _now()

        def payload(task: dict[str, Any]) -> dict[str, Any]:
            _check_execution_record(task)
            return {**identity, "decision": decision, "cap": e2e_budget.card_cap(task) + add}

        def mutation(task: dict[str, Any]) -> None:
            raises = [
                *e2e_budget.cap_raises(task),
                {"add": add, "authorized_by": authorized_by, "decision": decision, "at": at},
            ]
            self.client.call(
                "saveTaskMetadata",
                task_id=_task_number(task),
                values={e2e_budget.E2E_CAP_FIELD: e2e_budget.cap_text(raises)},
            )

        return self._write(
            e2e_budget.CARD_CAP_RAISED, role, actor, reference, request_id, payload, mutation, identity=identity
        )

    def verdict(
        self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None
    ) -> dict[str, Any]:
        role = self._role(role, {Role.REVIEWER}, actor=actor)
        body = self._redact_for_board(body)
        if kind not in {"green", "red"} or not body.strip():
            raise TaskError("validation", "verdicts require a non-empty body", 2)
        current = self.reader.show(reference)
        revision = specification_revision(self.audit.events(reference), current["description"])
        return self._marker_write(
            action="verdict",
            event_kind=EventKind.CARD_VERDICTED,
            role=role,
            actor=actor,
            reference=reference,
            reason=body,
            request_id=request_id,
            data={
                "marker": f"review:{kind}",
                "status": kind,
                "body": body,
                "body_sha256": _digest(body),
                "description_sha256": _digest(current["description"]),
                "specification_revision": revision or None,
            },
        )

    def decide(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        kind: str,
        body: str,
        protocol_prerequisites: Iterable[str] = (),
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Record what to do with a parked card, apart from the move that does it.

        The decision and its effect are two facts. Recording the decision first is what makes it
        checkable, because the move out of Assessment refuses to carry one that is not on the card.

        The observer decides, and nobody else; a PO that has to intervene moves the card with
        `--sprint-override` and a reason. The same sprint reservation guard `move` carries applies here
        and answers about the caller as well as the card: the caller's sprint is the one the dispatcher
        launched its head for, carried in the head's environment, so the binding rather than the actor
        id distinguishes one sprint's observer from another's.
        """
        role = self._role(role, {Role.OBSERVER}, actor=actor)
        body = self._redact_for_board(body)
        if kind not in DECISION_VALUES:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(DECISION_VALUES))}", 2)
        decision_kind = TaskDecision(kind)
        kind = decision_kind.value
        if not body.strip():
            raise TaskError("validation", "a decision requires a non-empty reason", 2)
        declared_prerequisites = tuple(protocol_prerequisites)
        request_id = request_id or str(uuid.uuid4())
        with assessment_decision_lock(self.data_dir, reference):
            # Resolve immutable request ownership before mutable decision state.
            try:
                owned = self.board_host.canon.event(request_id)
            except ValueError as exc:
                message = str(exc)
                if "released generic audit record" in message:
                    message = "request id belongs to another operation or payload"
                raise TaskError("validation", message, 2) from None
            if owned is not None:
                # A retry must carry the immutable binding which the first decision committed.
                # Do not re-derive it from mutable board state or drop it from the marker identity.
                owned_data = owned.data if isinstance(owned.data, dict) else {}
                if tuple(owned_data.get("protocol_prerequisites") or ()) != declared_prerequisites:
                    raise TaskError("validation", "request id belongs to another operation or payload", 2)
                replay_data = {
                    "marker": f"decision:{kind}",
                    "decision": kind,
                    "body": body,
                    "body_sha256": _digest(body),
                }
                for field_name in ("description_sha256", "specification_revision", "protocol_prerequisites"):
                    if field_name in owned_data:
                        replay_data[field_name] = owned_data[field_name]
                return self._marker_write(
                    action="decided",
                    event_kind=EventKind.CARD_DECIDED,
                    role=role,
                    actor=actor,
                    reference=reference,
                    reason=body,
                    request_id=request_id,
                    data=replay_data,
                )
            current = self.reader.show(reference)
            # Authorization before anything about the card: which sprint holds the project is the
            # question of whether this observer may write here at all.
            self._guard_sprint_write(
                role=role,
                actor=actor,
                project=current["project"],
                card_sprint=str(current.get("sprint") or ""),
                linked_sprint=None,
                sprint_override=False,
                sprint_override_reason="",
                request_id=request_id,
                reference=reference,
            )
            if not self._sprint_holds_project(current["project"]):
                raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
            committed_events = self.audit.events(reference)
            pending_decisions = [
                event
                for event in self.audit.pending_events()
                if str(event.get("ref") or "") == reference and _event_action(event) == "decided"
            ]
            visit, existing = assessment_resolution([*committed_events, *pending_decisions])
            marker_data = {
                "marker": f"decision:{kind}",
                "decision": kind,
                "body": body,
                "body_sha256": _digest(body),
                "assessment_visit": visit or None,
                "description_sha256": _digest(current["description"]),
                "specification_revision": specification_revision(committed_events, current["description"])
                or None,
                "protocol_prerequisites": list(declared_prerequisites),
            }
            if current["state"] != "assessment":
                raise TaskError(
                    "transition_forbidden", "a decision is only recorded on a card in Assessment", 3
                )
            if existing is not None:
                existing_payload = _event_payload(existing)
                existing_kind = str(existing_payload.get("decision") or "")
                existing_request = str(existing.get("request_id") or "")
                if self.audit.pending_event(existing_request) is not None:
                    raise TaskError(
                        "decision_pending",
                        f"Assessment visit {visit} has an unfinished {existing_kind} decision; reconcile request {existing_request}",
                        4,
                    )
                if existing_kind != kind:
                    raise TaskError(
                        "decision_already_recorded",
                        f"Assessment visit {visit} already has a {existing_kind} decision",
                        3,
                    )
                return {
                    "action": "decided",
                    "task": current,
                    "event_id": str(existing.get("event_id") or existing.get("request_id") or ""),
                    "replayed": True,
                }
            if decision_kind is TaskDecision.REWORK:
                try:
                    validate_rework_prerequisites(
                        declared_prerequisites,
                        specification_revision=marker_data["specification_revision"],
                    )
                except ValueError as exc:
                    raise TaskError("validation", str(exc), 2) from None
                except ArtifactOwnershipViolation as violation:
                    self._deny_rework_artifact_ownership(
                        violation=violation,
                        role=role,
                        actor=actor,
                        reference=reference,
                        request_id=request_id,
                        protocol_prerequisites=declared_prerequisites,
                    )
            elif declared_prerequisites:
                raise TaskError("validation", "protocol prerequisites are only supported for rework", 2)
            return self._marker_write(
                action="decided",
                event_kind=EventKind.CARD_DECIDED,
                role=role,
                actor=actor,
                reference=reference,
                reason=body,
                request_id=request_id,
                data=marker_data,
                require_assessment=True,
            )

    def routing(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one routing telemetry record for the card.

        Journal-only: the board holds no per-attempt routing history, so this write has no backend
        mutation. The event still goes through the normal pending/commit path, which makes it idempotent
        per request id and carries it into the recovery checkpoint.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        phase = _text(payload.get("phase"))
        if phase not in ROUTING_PHASE_VALUES:
            known = ", ".join(sorted(ROUTING_PHASE_VALUES))
            raise TaskError("validation", f"unknown routing phase {phase!r} (known: {known})", 2)
        routing_phase = RoutingPhase(phase)
        normalized_payload = dict(payload)
        normalized_payload["phase"] = routing_phase.value
        heads = payload.get("heads")
        if not isinstance(heads, list) or not heads:
            raise TaskError("validation", "routing requires at least one head record", 2)
        return self._write(
            "routing",
            role,
            actor,
            reference,
            request_id,
            normalized_payload,
            lambda task: None,
            identity=normalized_payload,
        )

    def outcome_round_context(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: OutcomeRoundContext | Mapping[str, object],
        request_id: str,
    ) -> dict[str, Any]:
        """Persist one exact forward source identity before its consumer runs.

        This is journal-only. The dispatcher carries the typed value; the
        historical dictionary exists only at this audit compatibility boundary.
        Raw mappings remain accepted for released callers and are normalized
        once through the same model.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        try:
            context = (
                data
                if isinstance(data, OutcomeRoundContext)
                else OutcomeRoundContext.from_data(data)
            )
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        if not request_id.strip():
            raise TaskError("validation", "outcome round context needs the request id it owns", 2)
        payload = context.to_data()
        return self._write(
            "outcome_round_context",
            role,
            actor,
            reference,
            request_id,
            payload,
            lambda task: None,
            identity=payload,
        )

    def attempt_usage(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: dict[str, Any],
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Append one durable ``attempt.usage`` occurrence for a finished worker or review phase.

        Journal-only, like routing telemetry: what a phase cost is not a board mutation, and the
        card carries no field it could disagree with. Unlike routing it is a typed protocol event,
        so the schema is checked at this boundary and the audit export exposes it without any
        marker prose to parse.

        The request id names one occurrence and owns it. A replay — a re-entered tick, a dispatcher
        recovering the same acceptance — commits the event that already owns the id rather than a
        freshly computed one, so a later read of a changed session file can neither add a second
        occurrence nor overwrite the first.

        Two durability steps, and the caller is told which one it reached. The exact occurrence is
        staged first, so a card cannot advance past a finished phase with nothing owed for it; the
        append then publishes it. An append that fails leaves the staged obligation, which is what
        ``finish_attempt_usage`` completes later — nothing is recomputed from a session file that has
        moved on. A stage that fails is an audit failure, and the caller has to treat it as one.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        if not request_id.strip():
            raise TaskError("validation", "an attempt usage event needs the request id it owns", 2)
        canon = self.board_host.canon
        if canon is None:
            raise TaskError("backend_unavailable", "board event canon is unavailable", 1)
        try:
            existing = canon.event(request_id)
        except (OSError, ValueError) as exc:
            raise TaskError(
                "audit_unavailable", f"attempt usage occurrence is unreadable: {exc}", 4
            ) from None
        if existing is not None:
            return self._commit_attempt_usage(canon, request_id, existing, replayed=True)
        try:
            event = Event(
                event_id="evt_" + uuid.uuid4().hex,
                kind=EventKind.ATTEMPT_USAGE,
                entity_kind=EntityKind.CARD,
                ref=reference,
                actor=Actor(role, actor),
                reason=reason,
                occurred_at=datetime.now(UTC),
                data=dict(data),
            )
            canon.stage(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError("audit_unavailable", "attempt usage occurrence could not be staged", 4) from None
        return self._commit_attempt_usage(canon, request_id, event, replayed=False)

    def _commit_attempt_usage(
        self,
        canon: BoardEventCanon,
        request_id: str,
        event: Event,
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        """Publish the staged occurrence, or report that its obligation is still owed."""
        try:
            canon.commit(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError(
                "audit_pending",
                "attempt usage occurrence is staged and awaits its journal append",
                4,
            ) from None
        return {"action": "attempt_usage", "event_id": event.event_id, "replayed": replayed}

    def finish_attempt_usage(self, *, role: str, reference: str = "") -> int:
        """Publish staged ``attempt.usage`` occurrences: this card's, or every card's.

        The recovery half of the durability order above. It finishes the exact staged record rather
        than a re-derived one, so a session file that has grown since cannot change what the phase
        was accounted for, and it is idempotent: a record already appended is simply gone from the
        pending set.

        Without a ``reference`` it takes the whole pending set. That is the form the production tick
        calls, because the card whose phase is owed an account may have gone Blocked or Done and be
        nowhere the tick would otherwise look. A record that cannot be published is left exactly
        where it is and stays owed.
        """
        role = self._role(role, {Role.DISPATCHER}, actor="")
        canon = self.board_host.canon
        if canon is None:
            return 0
        finished = 0
        for occurrence in canon.attempt_usage_occurrences(ref=reference):
            if not occurrence.pending:
                continue
            try:
                canon.commit(occurrence.request_id, occurrence.event)
            except (OSError, TypeError, ValueError, TaskError):
                # The obligation stays exactly where it is: still staged, still owed, still exact.
                continue
            finished += 1
        return finished

    def attempt_outcome(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: dict[str, Any],
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Stage then append one immutable observational terminal occurrence.

        This writer intentionally has no board effect.  Its caller is required
        to call it only after the lifecycle owner has confirmed the terminal
        move; a retry can only append the exact staged object.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        if not request_id.strip():
            raise TaskError("validation", "an attempt outcome needs the request id it owns", 2)
        canon = self.board_host.canon
        if canon is None:
            raise TaskError("backend_unavailable", "board event canon is unavailable", 1)
        try:
            occurrences = canon.attempt_outcome_occurrences(ref=reference)
            key = (reference, data.get("attempt_id"), data.get("report_generation"))
            existing_occurrence = next(
                (
                    occurrence
                    for occurrence in occurrences
                    if (
                        occurrence.event.ref,
                        occurrence.event.data.get("attempt_id"),
                        occurrence.event.data.get("report_generation"),
                    )
                    == key
                ),
                None,
            )
            if existing_occurrence is not None:
                if existing_occurrence.event.data != data:
                    raise AnalyticsOutcomeConflict(
                        f"attempt outcome natural key {key!r} has conflicting payloads"
                    )
                return self._commit_attempt_outcome(
                    canon, existing_occurrence.request_id, existing_occurrence.event, replayed=True
                )
            event = Event(
                event_id="evt_" + uuid.uuid4().hex,
                kind=EventKind.ATTEMPT_OUTCOME,
                entity_kind=EntityKind.CARD,
                ref=reference,
                actor=Actor(role, actor),
                reason=reason,
                occurred_at=datetime.now(UTC),
                data=dict(data),
            )
            canon.stage(request_id, event)
        except AnalyticsOutcomeConflict as exc:
            raise TaskError("analytics_outcome_conflict", str(exc), 3) from None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError("audit_unavailable", "attempt outcome could not be staged", 4) from None
        return self._commit_attempt_outcome(canon, request_id, event, replayed=False)

    def _commit_attempt_outcome(
        self, canon: BoardEventCanon, request_id: str, event: Event, *, replayed: bool
    ) -> dict[str, Any]:
        try:
            canon.commit(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError(
                "audit_pending", "attempt outcome is staged and awaits its journal append", 4
            ) from None
        return {"action": "attempt_outcome", "event_id": event.event_id, "replayed": replayed}

    def finish_attempt_outcomes(self, *, role: str, reference: str = "") -> int:
        """Append staged outcomes only; it never derives or changes lifecycle facts."""
        role = self._role(role, {Role.DISPATCHER}, actor="")
        canon = self.board_host.canon
        if canon is None:
            return 0
        finished = 0
        for occurrence in canon.attempt_outcome_occurrences(ref=reference):
            if not occurrence.pending:
                continue
            try:
                canon.commit(occurrence.request_id, occurrence.event)
            except (OSError, TypeError, ValueError, TaskError):
                continue
            finished += 1
        return finished

    @serialized
    def claim(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        worker: str,
        resolved_head: str = "",
        resolved_review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        cap: int = 3,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        worker = worker.strip()
        if not worker:
            raise TaskError("validation", "claim requires a non-empty worker id", 2)
        if cap < 1:
            raise TaskError("validation", "claim cap must be positive", 2)
        request_id = request_id or str(uuid.uuid4())
        if self._legacy_record(request_id) is not None:
            # Legacy generic claims retain their replay path and cannot move non-Cards.
            _check_execution_record(self.reader.show(reference))
            payload = {
                "worker": worker,
                "resolved_head": resolved_head or None,
                "resolved_review_head": resolved_review_head or None,
                "slug": slug or None,
                "base_branch": base_branch or None,
                "cap": cap,
            }
            return self._write(
                "claimed",
                role,
                actor,
                reference,
                request_id,
                payload,
                lambda task: None,
                identity=payload,
            )
        existing = self._typed_event(request_id)
        task = self.reader.show(reference)
        # Same guard as move: a product or an issue is not an execution task, so it never takes a
        # claim even if someone dragged it into Ready by hand.  It runs on the replay too, because
        # a card can have been retyped between the two attempts.
        _check_execution_record(task)
        if existing is None:
            cleanup_refusal = CleanupJournal(self.data_dir).admission_refusal(reference)
            if cleanup_refusal:
                raise TaskError("live_work", cleanup_refusal, 3)
            # Admission is the fresh request's job alone, and every part of it binds: a claim is
            # admitted only from Ready, only when nobody holds the card, and only inside the
            # predecessor and capacity rules.  A retrying claimant does not get past them by
            # having tried before, because a failed attempt leaves no claim behind to recognize.
            if task["state"] != "ready":
                raise TaskError("claim_conflict", "claim requires a Ready task", 3)
            if task["claim"]["worker"] is not None:
                raise TaskError("claim_conflict", "task is already claimed", 3)
            blocked_by = task.get("blocked_by")
            if blocked_by:
                predecessor = self.reader.show(str(blocked_by))
                if predecessor["state"] != "done":
                    raise TaskError("predecessor_open", "blocked_by task is not Done", 3)
            # A decision/operation/wait card runs no head, so it neither takes nor counts against the
            # capacity, which is how many heads the installation runs at once.
            headed = [
                active
                for active in self.reader.list(states=set(ACTIVE_STATES))
                if active["id"] != task["id"] and not _is_steward_report(active) and not is_headless(active)
            ]
            for active in headed:
                if (
                    active["type"] == "code"
                    and task["type"] == "code"
                    and active["project"] == task["project"]
                ):
                    raise TaskError(
                        "capacity_reached", "one active code task per project is already claimed", 3
                    )
            if len(headed) >= cap and not is_headless(task):
                raise TaskError("capacity_reached", "active task capacity is reached", 3)

        values = {
            "claim": worker,
            "resolved_head": resolved_head or task["routing"]["head_override"] or "",
        }
        if resolved_review_head:
            values["resolved_review_head"] = resolved_review_head
        if slug:
            values["slug"] = slug
        if base_branch:
            values["base_branch"] = base_branch
        # Write claim metadata only after the transition effect is proven.
        result = self._transition_card(
            reference=reference,
            target=CardState.IN_PROGRESS,
            role=role,
            actor=actor,
            reason=f"claimed by {worker}",
            request_id=request_id,
            finish=lambda _card: self.client.call(
                "saveTaskMetadata",
                task_id=_task_number(task),
                values=values,
            ),
        )
        return {
            "action": "claimed",
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": existing is not None,
        }

    def move(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        decision: str = "",
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        request_id: str | None = None,
        outcome_owed: dict[str, Any] | None = None,
        terminal_taxonomy: dict[str, Any] | None = None,
        release_merge: dict[str, Any] | None = None,
        wait_outcome: str | None = None,
    ) -> dict[str, Any]:
        role = self._role(role, BOARD_ROLES, actor=actor)
        reason = self._redact_for_board(reason)
        sprint_override_reason = self._redact_for_board(sprint_override_reason)
        request_id = request_id or str(uuid.uuid4())
        if outcome_owed is not None and not isinstance(outcome_owed, dict):
            raise TaskError("validation", "attempt outcome obligation must be an object", 2)
        if terminal_taxonomy is not None and not isinstance(terminal_taxonomy, dict):
            raise TaskError("validation", "terminal taxonomy must be an object", 2)
        if release_merge is not None and (
            not isinstance(release_merge, dict) or role != "dispatcher" or target != "done"
        ):
            raise TaskError("validation", "a release merge marker belongs on a dispatcher Done", 2)
        task = self.reader.show(reference)
        if role == "observer" and task.get("record_type") in _TYPED_RECORD_TYPES:
            # The observer files issues and never promotes, moves or closes one.
            raise TaskError("role_forbidden", "the observer may file an issue but not move one", 3)
        if (
            role == "steward"
            and (task["state"], target) == ("in_progress", "done")
            and not _is_steward_report(task)
        ):
            raise TaskError(
                "transition_forbidden",
                "steward may close In progress only for its own report card",
                3,
            )
        if self._legacy_record(request_id) is not None:
            # A move recorded before Card transitions migrated is a generic audit operation, and
            # it stays one: its retry replays that record and its pending form is finished by the
            # released cleanup, exactly as they were on the version that wrote it.
            return self._legacy_move(
                role=role,
                actor=actor,
                reference=reference,
                target=target,
                reason=reason,
                decision=decision,
                sprint_override=sprint_override,
                sprint_override_reason=sprint_override_reason,
                request_id=request_id,
                task=task,
            )
        existing = self._typed_event(request_id)
        # Resolve replays before guards: their request id already owns a typed event.
        if existing is not None:
            # A transition written before outcome obligations were introduced
            # remains a valid lifecycle replay. It cannot be rewritten to add
            # telemetry, but the caller still finishes its observation
            # best-effort after the confirmed effect.
            if outcome_owed is not None:
                stored_obligation = existing.data.get("attempt_outcome_owed")
                # The committed lifecycle fact owns the telemetry identity on
                # a replay. A later dispatcher record may already describe
                # the next generation, so re-deriving it here would turn an
                # exact effect retry into a conflicting payload.
                outcome_owed = dict(stored_obligation) if isinstance(stored_obligation, dict) else None
            if terminal_taxonomy is not None:
                stored_taxonomy = existing.data.get("terminal_taxonomy")
                terminal_taxonomy = dict(stored_taxonomy) if isinstance(stored_taxonomy, dict) else None
            if release_merge is not None:
                # Same rule: the committed Done owns whether it said a merge landed.
                stored_merge = existing.data.get(RELEASE_MERGE_KEY)
                release_merge = dict(stored_merge) if isinstance(stored_merge, dict) else None
            try:
                target_state = CardState(target)
            except ValueError:
                raise TaskError(
                    "transition_forbidden",
                    _forbidden_move_message(role, task["state"], target),
                    3,
                ) from None
            # A replay repeats no admission check and no backend move, but it does finish the
            # idempotent board cleanup the first attempt may have lost. The comment is the one
            # follow-up it never repeats: it is not idempotent, and the released reconciliation
            # never recreated one either.
            result = self._transition_card(
                reference=reference,
                target=target_state,
                role=role,
                actor=actor,
                reason=_transition_reason(reason, target),
                request_id=request_id,
                outcome_owed=outcome_owed,
                terminal_taxonomy=terminal_taxonomy,
                release_merge=release_merge,
                wait_outcome=wait_outcome,
                finish=self._transition_cleanup(
                    task,
                    source=str(existing.source_state or ""),
                    target=target,
                    reason="",
                    role=role,
                ),
            )
            replay = {
                "action": "moved",
                "task": self.reader.show(reference),
                "event_id": result.event.event_id,
                "replayed": True,
            }
            if outcome_owed is not None:
                replay["outcome_owed"] = outcome_owed
            self._steward_needs_human(task, role=role, target=target, reason=reason, moved=replay)
            return replay
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=task["project"],
            card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(),
            request_id=request_id,
            reference=reference,
            steward_report=_is_steward_report_card(task),
        )
        source = task["state"]
        _check_execution_record(task)
        if role == "observer" and not override_payload and not self._sprint_holds_project(task["project"]):
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
        try:
            target_state = CardState(target)
            card_transition(role, source, target_state)
        except (ValueError, CardTransitionForbidden):
            raise TaskError(
                "transition_forbidden", _forbidden_move_message(role, source, target), 3
            ) from None
        if (
            role == "steward"
            and (target == "blocked" or (source, target) == ("blocked", "done"))
            and not reason.strip()
        ):
            raise TaskError("validation", "this steward transition requires a non-empty reason", 2)
        # A Blocked exit records the observer's disposition of its classification.
        if role == "observer" and source == "blocked" and not reason.strip():
            raise TaskError("validation", "moving a card out of Blocked requires a non-empty reason", 2)
        if role == "dispatcher" and (refusal := self._dispatcher_wait_edge_refusal(task, source, target)):
            raise TaskError("transition_forbidden", refusal, 3)
        self._check_decision(task, source, target, decision, role)
        result = self._transition_card(
            reference=reference,
            target=target_state,
            role=role,
            actor=actor,
            reason=_transition_reason(reason, target),
            request_id=request_id,
            outcome_owed=outcome_owed,
            terminal_taxonomy=terminal_taxonomy,
            release_merge=release_merge,
            wait_outcome=wait_outcome,
            finish=self._transition_cleanup(
                task,
                source=source,
                target=target,
                reason=reason,
                role=role,
            ),
        )
        moved = {
            "action": "moved",
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": False,
        }
        if outcome_owed is not None:
            moved["outcome_owed"] = outcome_owed
        self._steward_needs_human(task, role=role, target=target, reason=reason, moved=moved)
        return moved

    def _dispatcher_wait_edge_refusal(self, task: dict[str, Any], source: str, target: str) -> str:
        """Why the dispatcher may not take one of its two wait edges here, or `""` (secretary-1790).

        In progress -> Done is a wait card's `target_reached`, once its result is frozen; Ready ->
        Blocked is a card held by a wait card (`blocked_by`) that ended another way, or the hotfix
        card the dispatcher itself cut for a red after-merge e2e run that no open sprint and no PO
        origin owns (secretary-1807). The dispatcher takes neither edge for any other card.
        """
        if (source, target) == ("in_progress", "done"):
            result = wait_card.wait_state(task).result if is_wait(task) else None
            if result is None or result.get("outcome") != wait_card.TARGET_REACHED:
                return "the dispatcher moves an In progress card to Done only as a wait card's target_reached"
        if (source, target) == ("ready", "blocked"):
            created = self.audit.events(str(task.get("ref") or ""), kind="created")
            if any(
                str(event.get("request_id") or "").startswith(e2e_record.AFTER_MERGE_HOTFIX_REQUEST_PREFIX)
                for event in created
            ):
                return ""
            for blocker in _blocker_refs(task):
                try:
                    if is_wait(self.reader.show(blocker)):
                        return ""
                except TaskError:
                    continue
            return "the dispatcher Blocks a Ready card only when a wait card it is blocked by ended unreached"
        return ""

    def _steward_needs_human(
        self, task: dict[str, Any], *, role: str, target: str, reason: str, moved: dict[str, Any]
    ) -> None:
        """The steward's report card went Blocked carrying "Needs a human": one `needs_owner` event.

        This move is the one place that fact is decided (the steward skill, step 5, moves its report
        card to Blocked with that section as the reason), so the event is written here, once per move.
        """
        if role != Role.STEWARD.value or target != CardState.BLOCKED.value or not _is_steward_report(task):
            return
        section = owner_events.needs_human_section(reason)
        if section is None:
            return
        reference = str(task.get("ref") or "")
        owner_events.record(
            owner_events.STEWARD_NEEDS_HUMAN,
            reference,
            f"The steward's report {reference} needs a human:\n{section}",
            f"{owner_events.STEWARD_NEEDS_HUMAN}:{reference}:{moved.get('event_id')}",
            to=self.client,
        )

    def _legacy_move(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        decision: str,
        sprint_override: bool,
        sprint_override_reason: str,
        request_id: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        """Replay a move this request id already recorded as a released generic operation."""
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=task["project"],
            card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(),
            request_id=request_id,
            reference=reference,
            steward_report=_is_steward_report_card(task),
        )
        return self._write(
            "moved",
            role,
            actor,
            reference,
            request_id,
            lambda task: {
                "from": task["state"],
                "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                **({"decision": decision} if decision else {}),
                **override_payload,
            },
            lambda task: None,
            # `from` is the column the move already left, so it is the one field a retry cannot
            # recompute. Everything the caller asked for is compared.
            identity={
                "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                "decision": decision or None,
                "sprint_override_reason": override_payload.get("sprint_override_reason"),
            },
        )

    def _legacy_record(self, request_id: str) -> dict[str, Any] | None:
        """The released generic audit record this request id owns, if it owns one.

        The two representations are told apart by the record's own discriminator, never by guessing from
        a payload.
        """
        record = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if record is None or is_protocol_event(record):
            return None
        return record

    def _transition_cleanup(
        self,
        task: dict[str, Any],
        *,
        source: str,
        target: str,
        reason: str,
        role: str,
    ) -> Callable[[Any], None]:
        """The board work a migrated Card state edge still owes once its column effect lands.

        Handed to the adapter so it runs inside the transition's transaction: it completes before the
        event commits, and an incomplete one leaves the exact pending typed record that both a retry and
        :meth:`reconcile` know how to finish.
        """

        def finish(_entity: Any) -> None:
            self._reset_transition_metadata(task, source=source, target=target)
            if reason.strip():
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[{role}]\n{reason}",
                )

        return finish

    def _reset_transition_metadata(self, task: dict[str, Any], *, source: str, target: str) -> None:
        """Apply the board metadata a Card state edge resets.

        Kept idempotent on purpose: a retry or :meth:`reconcile` may repeat it after the column effect
        and its typed event are durable.
        """
        # A card handed to the owner waits for the owner only while it is In progress: whatever
        # moves it on (`task complete` above all) takes the mark off in the same transaction.
        clear_mark = CLEAR_MARK if source == "in_progress" and carries_mark_fields(task) else {}
        if attention_record(task, OWNER_ESCALATION):
            clear_mark = {**clear_mark, OWNER_ESCALATION: ""}
        if target == "ready" and attention_record(task, OWNER_ANSWER):
            clear_mark = {**clear_mark, OWNER_ANSWER: ""}
        if clear_mark:
            owner_events.settle_required_wait(str(task.get("ref") or ""), to=self.client)
        if target in {"ready", "done"}:
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={**_READY_RESET_METADATA, **clear_mark}
            )
        elif clear_mark:
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values=clear_mark)
        if source == "validate" and target not in {"ready", "done"}:
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={"resolved_review_head": ""}
            )

    @serialized
    def _transition_card(
        self,
        *,
        reference: str,
        target: CardState,
        role: str,
        actor: str,
        reason: str,
        request_id: str,
        outcome_owed: dict[str, Any] | None = None,
        terminal_taxonomy: dict[str, Any] | None = None,
        release_merge: dict[str, Any] | None = None,
        wait_outcome: str | None = None,
        po_session: str | None = None,
        finish: Callable[[Any], None] | None = None,
    ) -> MutationResult:
        """Run one state edge through the typed adapter and its shared journal.

        The sprint the card belongs to is not passed here: the adapter reads the live card to authorize
        the edge anyway. `finish` carries this writer's remaining board work into the same transaction.

        `_mutation()` is that transaction, and this is the single place both edges cross it: `move`
        and `claim` reach the adapter only through here, so the staged request row, the column
        effect, the caller's `finish` work and the committed event are one transaction wherever the
        backend has transactions (§7.1).  Without transactions `_mutation()` is nothing at all and every
        half-applied state below stays exactly as it is, with its `recover_*` entry point; on
        PostgreSQL a failure after the move rolls the move back with the claim, which is why §7.3
        lists that class of state as one this backend does not have.
        """
        try:
            with self._mutation():
                def finish_with_wait(entity: Any) -> None:
                    if finish is not None:
                        finish(entity)
                    card = self.reader.show(reference)
                    state = e2e_record.e2e_state(card)
                    if state.budget_decline and (target != "blocked" or state.budget_decline.get("request_id") != request_id):
                        state.budget_decline = None
                        self.client.call("saveTaskMetadata", task_id=_task_number(card), values={e2e_record.E2E_FIELD: state.text()})
                        card = self.reader.show(reference)
                    owner_events.record_person_wait(card, request_id, to=self.client)

                return self.board_host.transition(
                    TransitionRequest(
                        EntityKind.CARD,
                        reference,
                        target,
                        Actor(role, actor),
                        reason,
                        RelatedRefs(()),
                        request_id,
                        data={
                            **(
                                {"attempt_outcome_owed": dict(outcome_owed)}
                                if outcome_owed is not None
                                else {}
                            ),
                            **(
                                {"terminal_taxonomy": dict(terminal_taxonomy)}
                                if terminal_taxonomy is not None
                                else {}
                            ),
                            **({RELEASE_MERGE_KEY: dict(release_merge)} if release_merge is not None else {}),
                            # A wait card's outcome, and a dependent it Blocks: never a budget charge.
                            **({WAIT_OUTCOME_KEY: wait_outcome} if wait_outcome else {}),
                            # The PO session whose turn completed the card (`complete`).
                            **({PO_SESSION_KEY: po_session} if po_session else {}),
                        },
                    ),
                    finish=finish_with_wait,
                )
        except BoardEventPending:
            raise self._post_effect_refusal("the card transition") from None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except CardTransitionForbidden as exc:
            raise TaskError("transition_forbidden", str(exc), 3) from None
        except BoardProtocolError as exc:
            raise TaskError("backend_error", str(exc), 1) from None

    def _typed_event(self, request_id: str) -> Any | None:
        if self.board_host.canon is None:
            return None
        try:
            return self.board_host.canon.event(request_id)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None

    def _check_decision(
        self,
        task: dict[str, Any],
        source: str,
        target: str,
        decision: str,
        role: str,
    ) -> None:
        """A card leaves Assessment on a decision somebody recorded, or it does not leave.

        Two rules binding different callers. A supplied decision has to be real and has to agree with
        where the card is going, whoever passes it: each decision has exactly one destination. Needing a
        decision at all is the dispatcher's rule, because the dispatcher performs decisions; the PO's
        move is the escape hatch, already recorded as a sprint override.

        `blocked` without a decision stays open even for the dispatcher: the steward's stale escalation
        and the dispatcher's own failure paths reach it without anyone deciding, and a card that cannot
        be blocked is a card nothing can rescue. The observer has no exit from Assessment at all.
        """
        if decision and decision not in DECISION_VALUES:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(DECISION_VALUES))}", 2)
        decision_kind = TaskDecision(decision) if decision else None
        if decision_kind is not None and source != "assessment":
            raise TaskError("validation", "a decision is only carried by a move out of Assessment", 2)
        if decision_kind is not None and DECISION_TARGETS[decision_kind].value != target:
            raise TaskError(
                "decision_mismatch",
                f"a {decision} decision moves the card to {DECISION_TARGETS[decision_kind].value}, not {target}",
                3,
            )
        if (
            source == "assessment"
            and target in DECIDED_TARGETS
            and decision_kind is None
            and role in _DECISION_BOUND_ROLES
        ):
            raise TaskError(
                "decision_required",
                "a card leaves Assessment only on a recorded decision: record one with "
                "`task decide` and pass it as --decision",
                3,
            )
        if source == "assessment" and target in UNDECIDED_EXITS and role in _DECISION_BOUND_ROLES:
            raise TaskError(
                "decision_required",
                f"{role} may not move a parked card to {target}: that leaves Assessment with "
                "nothing decided. Decide the card, or have the PO move it",
                3,
            )
        if decision_kind is not None and not self._decision_recorded(task["ref"], decision_kind.value):
            raise TaskError(
                "decision_required",
                f"no {decision} decision is recorded on this card since it entered Assessment",
                3,
            )

    def _decision_recorded(self, reference: str, decision: str) -> bool:
        return standing_decision(self.audit.events(reference)) == decision

    def edit(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        title: str | None = None,
        description: str | None = None,
        head: str | None = None,
        review_head: str | None = None,
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Revise a card's spec in place instead of piling corrections into comments.

        The audit event chains old and new content digests. Cards with an active attempt (In progress /
        Validate) are not editable: the running head works from a TASK.md snapshot, so a mid-flight
        revision must go through preempt/requeue, not a silent spec swap.
        """
        role = self._role(role, EDIT_ROLES, actor=actor)
        title = self._redact_for_board(title) if title is not None else None
        description = self._redact_for_board(description) if description is not None else None
        sprint_override_reason = self._redact_for_board(sprint_override_reason)
        request_id = request_id or str(uuid.uuid4())
        if title is not None and not title.strip():
            raise TaskError("validation", "edit title must be non-empty", 2)
        if title is None and description is None and head is None and review_head is None:
            raise TaskError("validation", "edit requires a new title, description, head or review head", 2)
        current = self.reader.show(reference)
        if role == "observer" and current.get("record_type") in _TYPED_RECORD_TYPES:
            raise TaskError("role_forbidden", "the observer may file an issue but not edit one", 3)
        # The bounds are what makes a live-impact card admissible; an edit cannot remove them.
        if (
            description is not None
            and current.get("live_impact")
            and (bounds_refusal := impact_bounds_refusal(description))
        ):
            raise TaskError("validation", bounds_refusal, 2)
        # Nothing edits a head onto a card the PO service executes or the dispatcher advances.
        if is_headless(current) and ((head or "").strip() or (review_head or "").strip()):
            runner = "the dispatcher advances it" if is_wait(current) else "the PO service runs it"
            raise TaskError(
                "validation", f"a {current.get('type')} card takes no head or reviewer: {runner}", 2
            )
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=current["project"],
            card_sprint=str(current.get("sprint") or ""),
            linked_sprint=None,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(),
            request_id=request_id,
            reference=reference,
        )
        if (
            role in {"observer", "dispatcher"}
            and not override_payload
            and not self._sprint_holds_project(current["project"])
        ):
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
        # A card can be revised until it is claimed, so an edit is the second door onto the same
        # two fields and goes through the same guard. Only the fields this edit writes are asked
        # about: an edit of the title alone says nothing about the executors and is left alone.
        head, review_head = self._sprint_executor_pins(
            sprint_ref=str(current.get("sprint") or ""),
            head=head,
            review_head=review_head,
        )
        # A legacy card with no stored choice reads as required and is never refused here.
        if review_head is not None and current.get("review") == TaskReview.SKIPPED.value:
            self._refuse_unpinned_reviewer_on_skipped(
                sprint_ref=str(current.get("sprint") or ""), review_head=review_head.strip()
            )
        payload = {
            "title_sha256": _digest(title.strip()) if title is not None else None,
            "title_sha256_was": _digest(current["title"]) if title is not None else None,
            "description_sha256": _digest(description) if description is not None else None,
            "description_sha256_was": _digest(current["description"]) if description is not None else None,
            "head": head.strip() or None if head is not None else None,
            "head_was": current["routing"]["head_override"] if head is not None else None,
            "review_head": review_head.strip() or None if review_head is not None else None,
            "review_head_was": current["routing"]["review_head_override"]
            if review_head is not None
            else None,
            **override_payload,
        }

        def mutation(task: dict[str, Any]) -> Any:
            if task["state"] not in EDITABLE_STATES:
                raise TaskError("edit_forbidden", "edit requires a Ready or Blocked card", 3)
            number = _task_number(task)
            update: dict[str, Any] = {}
            if title is not None:
                update["title"] = title.strip()
            if description is not None:
                update["description"] = description
            committed = False
            if update:
                if not self.client.call("updateTask", id=number, **update):
                    raise TaskError("backend_error", "board store rejected the write", 1)
                committed = True
            values = {}
            if head is not None:
                values["head"] = head.strip()
            if review_head is not None:
                values["review_head"] = review_head.strip()
            if values:
                try:
                    self.client.call("saveTaskMetadata", task_id=number, values=values)
                except Exception as exc:
                    if committed:
                        raise _CommittedWriteError() from exc
                    raise

        # Replay compares both replaced-text digests and requested values.
        identity = {key: value for key, value in payload.items() if not key.endswith("_was")}
        return self._write("edited", role, actor, reference, request_id, payload, mutation, identity=identity)

    def _sprint_holds_project(self, project: str) -> bool:
        """Whether an open sprint reserves this card's project."""
        from ummanu.sprints import active_sprint_projects

        return bool(active_sprint_projects(self.data_dir).get(project))

    def _sprint_executor_pins(
        self,
        *,
        sprint_ref: str,
        head: str | None,
        review_head: str | None,
        sprint: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        """The single place a card's worker and reviewer profile is held to its sprint's pins.

        Every write of those two card fields comes through here, so the constraint has one door:
        `create` cuts a card (a first card, a later one, or one recreated after a rework or a
        reslice) and `edit` revises one before it is claimed. Both pass what they are about to
        write and use what comes back; a guard the second door walked past would not be a
        constraint at all. What the dispatcher later records in `resolved_head` is not a third
        door: it launches what the card declares here, and records which profile it launched.

        `None` is a field this call does not write and is returned untouched. `""` is a caller
        that asks for no profile of its own, and under a pin it becomes the pinned profile rather
        than something a default resolves later — the card carries the profiles it runs on exactly
        as it always has. A different profile is refused by name.

        A role the sprint pins nothing on is untouched, which is every sprint opened until now. A
        field that is there but unreadable is corruption, and a card is not written under a
        constraint nobody can read.
        """
        from ummanu.sprint_observer import EXECUTOR_FIELDS, EXECUTOR_PINNED, EXECUTOR_UNSET

        requested: dict[str, str | None] = {"worker": head, "reviewer": review_head}
        if not sprint_ref or (head is None and review_head is None):
            return requested["worker"], requested["reviewer"]
        entity = sprint if sprint is not None else self._sprint_entity(sprint_ref)
        states = entity.get("executors") or {}
        for role in EXECUTOR_FIELDS:
            asked = requested[role]
            if asked is None:
                continue
            state = states.get(role) or {"state": EXECUTOR_UNSET}
            if state.get("state") == EXECUTOR_UNSET:
                continue
            if state.get("state") != EXECUTOR_PINNED:
                raise TaskError(
                    "sprint_executor_unreadable",
                    f"sprint {sprint_ref} carries a {role} pin that is not a head profile; repair "
                    "the sprint entity before writing its cards",
                    3,
                )
            profile = str(state.get("profile") or "")
            if asked and asked != profile:
                raise TaskError(
                    "sprint_executor_pinned",
                    f"sprint {sprint_ref} pins its {role} to head profile {profile!r}; this card "
                    f"asks for {asked!r}",
                    3,
                )
            requested[role] = profile
        return requested["worker"], requested["reviewer"]

    def _po_session_state(self, session_id: str) -> str:
        """The PO session's state (`open`/`closed`), `""` when the PO store has no such session.

        Raises `TaskError` when the store cannot answer: an unverifiable address is not admitted.
        """
        from ummanu.po.store import PoStore, SessionNotFound

        try:
            return str(PoStore.for_instance(self.instance_dir).session(session_id).state)
        except SessionNotFound:
            return ""
        except Exception as exc:  # noqa: BLE001 - a store error, or credentials that cannot be read
            raise TaskError(
                "po_store_unavailable",
                f"cannot verify PO session {session_id}: the PO store did not answer ({type(exc).__name__})",
                1,
            ) from None

    def _refuse_unknown_po_sessions(self, session_ids: Iterable[str]) -> None:
        """A wait card's `po-session:<id>` must name a session the PO store holds and that is open."""
        from ummanu.po.store import SESSION_CLOSED

        for session_id in session_ids:
            state = self._po_session_state(session_id)
            if not state:
                raise TaskError("validation", f"--wait-return po-session:{session_id} names no PO session", 2)
            if state == SESSION_CLOSED:
                raise TaskError(
                    "validation",
                    f"--wait-return po-session:{session_id} names a closed PO session; it takes no input",
                    2,
                )

    def _refuse_unpinned_reviewer_on_skipped(
        self, *, sprint_ref: str, review_head: str, sprint: dict[str, Any] | None = None
    ) -> None:
        """Refuse a caller-named reviewer on a card whose review is skipped, unless the sprint pins it.

        The review choice decides whether review runs and the sprint pin decides who reviews, so a
        skipped card may carry the pinned reviewer, applied by the pin or named explicitly. Only a
        reviewer the caller chose on its own contradicts `skipped`. The pin is read through
        `_sprint_executor_pins`, the one door for it.
        """
        if not review_head:
            return
        _, pinned = self._sprint_executor_pins(
            sprint_ref=sprint_ref, head=None, review_head="", sprint=sprint
        )
        if review_head != (pinned or ""):
            raise TaskError(
                "validation",
                f"--review-head {review_head!r} names a reviewer for a card whose review is skipped",
                2,
            )

    def _sprint_entity(self, reference: str) -> dict[str, Any]:
        """The sprint a card names, read here only to answer what it pins.

        A sprint that cannot be read fails closed: the alternative is writing a profile onto a card
        whose constraint nobody could check, which is the one outcome the pin exists to prevent.
        """
        from ummanu.sprints import SprintReader

        try:
            return SprintReader(self.client).show(reference, include_cards=False)
        except TaskError as exc:
            raise TaskError(
                "sprint_executor_unreadable",
                f"sprint {reference} cannot be read, so its executor pins cannot be checked: {exc.message}",
                3,
            ) from None

    def open_sprints_reserving(
        self, project: str, *, linked_sprint: dict[str, Any] | None = None
    ) -> list[str]:
        """The open sprints that reserve `project`, each verified against the sprint board.

        One answer for the write guard and the dispatcher's admission. The local index says which
        sprints to ask about and is seeded from the board when it has never been written; every
        sprint it names is then read live, and the index follows what was read. Anything that
        cannot be read raises `SprintReservationUnverifiable` naming the sprint (`""` for the
        seeding read): both callers fail closed on it, each in its own words.
        """
        from ummanu.sprints import (
            SprintReader,
            active_sprint_projects,
            refresh_active_sprint_projects,
            sprint_guard_index_initialized,
            update_active_sprint_projects,
        )

        if not sprint_guard_index_initialized(self.data_dir):
            try:
                refresh_active_sprint_projects(self.data_dir, SprintReader(self.client))
            except TaskError as exc:
                raise SprintReservationUnverifiable("", exc) from exc

        refs = set(active_sprint_projects(self.data_dir).get(project, []))
        if linked_sprint is not None and project in linked_sprint.get("reservations", []):
            refs.add(str(linked_sprint["ref"]))
        held: list[str] = []
        for sprint_ref in sorted(refs):
            try:
                sprint = (
                    linked_sprint
                    if linked_sprint and sprint_ref == linked_sprint.get("ref")
                    else SprintReader(self.client).show(sprint_ref, include_cards=False)
                )
            except TaskError as exc:
                raise SprintReservationUnverifiable(sprint_ref, exc) from exc
            update_active_sprint_projects(self.data_dir, sprint)
            if sprint.get("status") == "open" and project in sprint.get("reservations", []):
                held.append(sprint_ref)
        return held

    def _guard_sprint_write(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        card_sprint: str,
        linked_sprint: dict[str, Any] | None,
        sprint_override: bool,
        sprint_override_reason: str,
        request_id: str,
        reference: str,
        steward_report: bool = False,
        po_card: bool = False,
    ) -> dict[str, str]:
        """Authorize one create/move/edit against the caller and the open-sprint reservation index.

        Two questions, in this order. Who is writing: a caller of role `observer` names the sprint it
        was launched for, and a write about any other sprint's card is refused as the identity failure
        it is. Then what is being written: which open sprint reserves the card's project. A PO write
        of a card linked to no sprint is not the holding sprint's and passes once the index is
        verified; the dispatcher's admission decides whether such a card runs. So does a steward
        write of its own report card (`steward_report`, the caller's reading of the create or of
        the card's recorded marker), which is the steward's accounting, never a sprint's work. A PO
        create of a `decision` or `operation` card (`po_card`) is the sprint's own channel to its PO
        and needs no override either: it touches no branch the sprint owns.

        The identity half is fail-closed. A head that carries no binding cannot prove which sprint it is
        the observer of, and an unprovable caller is refused rather than admitted.
        """
        self._guard_observer_identity(
            role=role,
            actor=actor,
            project=project,
            card_sprint=card_sprint,
            request_id=request_id,
            reference=reference,
        )
        try:
            held = self.open_sprints_reserving(project, linked_sprint=linked_sprint)
        except SprintReservationUnverifiable as exc:
            message = (
                f"cannot verify sprint {exc.sprint_ref} reserving project {project}; write it through the sprint entity"
                if exc.sprint_ref
                else f"cannot verify open sprints for project {project}; write it through the sprint entity"
            )
            self._deny_sprint_write(
                code="sprint_guard_unavailable",
                message=message,
                role=role,
                actor=actor,
                project=project,
                sprint=exc.sprint_ref,
                request_id=request_id,
                reference=reference,
            )
            raise AssertionError("unreachable") from exc
        if not held:
            return {}
        sprint_ref = card_sprint if card_sprint in held else held[0]
        if role == "po" and sprint_override:
            if not sprint_override_reason:
                self._deny_sprint_write(
                    code="validation",
                    message="sprint override requires a non-empty reason",
                    role=role,
                    actor=actor,
                    project=project,
                    sprint=sprint_ref,
                    request_id=request_id,
                    reference=reference,
                    exit_code=2,
                )
            self._grant_sprint_override(
                role=role,
                actor=actor,
                project=project,
                sprint=sprint_ref,
                reason=sprint_override_reason,
                request_id=request_id,
                reference=reference,
            )
            return {"sprint_override_reason": sprint_override_reason}
        # A PO card linked to no sprint is not the holding sprint's work, whatever its kind: whether
        # it runs on a reserved project is the dispatcher's admission (`open_sprints_reserving`
        # asked before the claim), not this guard. A card linked to a sprint, and a create that
        # links one, keep the override rule above.
        if role == "po" and not card_sprint and linked_sprint is None:
            return {}
        if role == "po" and po_card:
            return {}
        # The observer's own headless card (a wait) touches no branch any sprint owns; its identity
        # as this card's sprint's observer was proven above.
        if role == "observer" and po_card and card_sprint:
            return {}
        # The steward's own report card is its tick's accounting, created In progress as research
        # and linked to no sprint; the dispatcher never claims it. Its proposals and every other
        # card it touches stay the holding sprint's to refuse.
        if role == "steward" and steward_report and not card_sprint and linked_sprint is None:
            return {}
        # The caller was already proven to be this card's sprint's observer above; what is left is
        # that the sprint holding the project is the one the card is linked to.
        if role == "observer" and card_sprint in held:
            return {}
        if role == "dispatcher":
            return {}
        self._deny_sprint_write(
            code="sprint_write_forbidden",
            message=f"project {project} is reserved by open sprint {sprint_ref}; write it through the sprint entity {sprint_ref}",
            role=role,
            actor=actor,
            project=project,
            sprint=sprint_ref,
            request_id=request_id,
            reference=reference,
        )
        raise AssertionError("unreachable")

    def _guard_observer_identity(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        card_sprint: str,
        request_id: str,
        reference: str,
    ) -> None:
        """Refuse a write of role `observer` that is not about the caller's own sprint.

        The binding is the launcher's, carried in the head's own environment: the reservation index says
        which sprint holds the card, and this says which sprint the caller is.

        The two refusals are separate codes because they are separate failures — a missing binding is a
        head nobody bound, a mismatch is a bound head reaching outside its sprint — and neither is
        `role_forbidden`. A card that names no sprint is left to the reservation guard below.
        """
        if role != "observer":
            return
        from ummanu.runtime.role_env import declared_observer_sprint

        declared = declared_observer_sprint()
        if not declared:
            self._deny_sprint_write(
                code="observer_identity_unbound",
                message="this observer names no sprint, so its writes cannot be authenticated; "
                "it has to be launched by the dispatcher for one sprint",
                role=role,
                actor=actor,
                project=project,
                sprint="",
                request_id=request_id,
                reference=reference,
            )
        if card_sprint and card_sprint != declared:
            self._deny_sprint_write(
                code="observer_sprint_mismatch",
                message=f"this observer belongs to sprint {declared} and the card is linked to "
                f"{card_sprint}; write it as that sprint's observer",
                role=role,
                actor=actor,
                project=project,
                sprint=declared,
                request_id=request_id,
                reference=reference,
            )

    def _grant_sprint_override(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        sprint: str,
        reason: str,
        request_id: str,
        reference: str,
    ) -> None:
        """Record the granted single-writer override before the operation it authorizes runs.

        A generic control-plane record like the denial, not part of the Card's typed event: the event
        describes the lifecycle edge, this describes the authority the writer used. Written before the
        operation stages or effects anything, so an override that could not be recorded does not happen.
        The derived request id keeps it off the operation's own retry key.
        """
        override_request_id = _sprint_guard_override_request_id(request_id)
        if self.audit.committed_event(override_request_id) is not None:
            return
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": "sprint_guard_override",
            "outcome": "granted",
            "task_id": "",
            "ref": reference,
            "backend": {"kind": BOARD_STORE_KIND, "task_id": None, "revision": "not_written"},
            "request_id": override_request_id,
            "payload": {
                "project": project,
                "sprint": sprint,
                "sprint_override_reason": reason,
                "operation_request_id": request_id,
            },
        }
        self.audit.stage(override_request_id, event)
        try:
            self.audit.append(override_request_id, event)
        except OSError:
            raise TaskError(
                "audit_pending",
                "sprint override was granted but audit repair is required",
                4,
            ) from None

    def _deny_sprint_write(
        self,
        *,
        code: str,
        message: str,
        role: str,
        actor: str,
        project: str,
        sprint: str,
        request_id: str,
        reference: str,
        exit_code: int = 3,
    ) -> None:
        denial_request_id = _sprint_guard_denial_request_id(request_id)
        event = self.audit.committed_event(denial_request_id)
        if event is None:
            event = {
                "event_id": "evt_" + uuid.uuid4().hex,
                "schema_version": 1,
                "occurred_at": _now(),
                "actor": {"role": role, "id": actor},
                "kind": "sprint_guard_denied",
                "outcome": "denied",
                "task_id": "",
                "ref": reference,
                "backend": {"kind": BOARD_STORE_KIND, "task_id": None, "revision": "not_written"},
                "request_id": denial_request_id,
                "payload": {
                    "code": code,
                    "message": message,
                    "project": project,
                    "sprint": sprint,
                    "operation_request_id": request_id,
                },
            }
            self.audit.stage(denial_request_id, event)
            try:
                self.audit.append(denial_request_id, event)
            except OSError:
                raise TaskError(
                    "audit_pending", "sprint write was denied but audit repair is required", 4
                ) from None
        payload = event.get("payload") if isinstance(event, dict) else {}
        raise TaskError(str(payload.get("code") or code), str(payload.get("message") or message), exit_code)

    def _deny_rework_artifact_ownership(
        self,
        *,
        violation: ArtifactOwnershipViolation,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        protocol_prerequisites: tuple[str, ...],
    ) -> None:
        """Persist the denied rework without mutating its parked card.

        The derived audit key lets the observer correct the instruction and reuse the decision
        request identity: a refusal is evidence, not an authoritative decision or worker outcome.
        """
        refusal_request_id = _artifact_ownership_refusal_request_id(request_id)
        data = {
            "decision": "rework",
            "code": "artifact_ownership_violation",
            "artifact": violation.artifact.name,
            "artifact_owner": violation.artifact.owner.value,
            "requested_role": violation.requested_role.value,
            "specification_revision": violation.specification_revision,
            "protocol_prerequisites": list(protocol_prerequisites),
        }
        existing = self.board_host.canon.event(refusal_request_id)
        if existing is None:
            event = Event(
                "evt_artifact_ownership_" + hashlib.sha256(refusal_request_id.encode("utf-8")).hexdigest(),
                EventKind.CARD_DECISION_REFUSED,
                EntityKind.CARD,
                reference,
                Actor(role, actor),
                violation.message,
                datetime.now(UTC),
                data=data,
            )
        else:
            if (
                existing.kind is not EventKind.CARD_DECISION_REFUSED
                or existing.ref != reference
                or existing.data != data
            ):
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            event = existing
        try:
            self.board_host.canon.commit(refusal_request_id, event)
        except (OSError, ValueError) as exc:
            raise TaskError("audit_pending", "artifact ownership refusal requires audit repair", 4) from exc
        raise ArtifactOwnershipTaskError(violation)

    def archive(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        reason: str,
        request_id: str | None = None,
        sprint_close: str = "",
    ) -> dict[str, Any]:
        """Archive one card: the PO's write, and a step of a sprint close in the closer's name.

        `sprint_close` names the sprint whose close this archive is a step of. Only there is the
        observer admitted, and only as the observer that sprint's close was admitted for: the card
        guard checks that binding again against the sprint the close names.
        """
        role = self._role(role, {Role.PO, Role.OBSERVER} if sprint_close else {Role.PO}, actor=actor)
        reason = self._redact_for_board(reason)
        if not reason.strip():
            raise TaskError("validation", "archive requires a non-empty reason", 2)
        request_id = request_id or str(uuid.uuid4())
        if role == "observer":
            self._guard_observer_identity(
                role=role,
                actor=actor,
                project="",
                card_sprint=sprint_close,
                request_id=request_id,
                reference=reference,
            )

        def mutation(task: dict[str, Any]) -> Any:
            if task.get("record_type") in {"issue", "product"}:
                raise TaskError(
                    "transition_forbidden",
                    "Product issues must be closed with ummanu issue close; products cannot be archived",
                    3,
                )
            self._check_archivable(task)
            self._check_dispatcher_archivable(reference)
            self._request_workspace_cleanup(task, "close" if sprint_close else "archive")
            try:
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[archive]\n{reason}",
                )
                if not self.client.call("closeTask", task_id=_task_number(task)):
                    raise TaskError("backend_error", "board store rejected the archive", 1)
            except Exception as exc:
                raise _CommittedWriteError() from exc

        return self._write(
            "archived",
            role,
            actor,
            reference,
            request_id,
            {"reason_sha256": _digest(reason)},
            mutation,
            retry_payload={"reason": reason},
            identity={"reason_sha256": _digest(reason)},
        )

    def retire_done(
        self,
        *,
        reference: str,
        expected_date_moved: int,
        cutoff: float,
        retention_days: int,
        actor: str = "retro-retention",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one proven-old Done episode without using the PO archive path.

        ``date_moved`` identifies the episode, rather than merely the card.  A
        reopen or a move away and back to Done therefore turns an old candidate
        into a harmless skip before a close can be sent.  Pending records retain
        that proof and can retry a lost reply only for the same episode.
        """
        expected_date_moved = _positive_int(expected_date_moved) or 0
        if not expected_date_moved:
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        try:
            cutoff_value = float(cutoff)
        except (TypeError, ValueError):
            raise TaskError("validation", "Done retention requires a numeric cutoff", 2) from None
        if retention_days < 0:
            raise TaskError("validation", "Done retention days cannot be negative", 2)

        initial = self._retention_card(reference, task_id=None)
        if initial is None:
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        task_id, raw, metadata, done_id = initial
        self._check_retention_record(metadata)
        request_id = request_id or _done_retention_request_id(task_id, expected_date_moved)
        identity = {
            "expected_date_moved": expected_date_moved,
            "cutoff": cutoff_value,
            "retention_days": retention_days,
            "task_id": task_id,
        }
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind="retired", reference=reference, identity=identity)
            return {"action": "retired", "reference": reference, "retired": True, "replayed": True}
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind="retired", reference=reference, identity=identity)
            try:
                self._finish_pending_retired(pending)
                self._prove_retired_closed(pending)
                self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError, ValueError):
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {"action": "retired", "reference": reference, "retired": True, "replayed": True}

        # Do not stage a successful-looking occurrence for a candidate that has
        # already aged out of eligibility between the list and this write.
        if not self._retention_matches(raw, metadata, expected_date_moved, cutoff_value, done_id):
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": "retro", "id": actor},
            "kind": "retired",
            "outcome": "success",
            "task_id": entity_id("task", task_id),
            "ref": reference,
            "backend": {"kind": BOARD_STORE_KIND, "task_id": task_id, "revision": "pending"},
            "request_id": request_id,
            "payload": identity,
        }
        # The same boundary the card protocol's other mutations stand on (§7.1): the freshness
        # guard, the destructive close, its proof and the record are one transaction where the
        # backend has one.  Retention is a Card protocol mutation with a board effect, not an
        # effect outside the board, so leaving it at `_depth == 0` left exactly the half-applied
        # state §7.3 says this backend does not have: a closed card beside a staged request.  For a
        # client without transactions `_mutation()` is nothing at all, so the ambiguity below — and the pending
        # record `reconcile` settles from it — is untouched.
        with self._mutation():
            self.audit.stage(request_id, event)
            try:
                # This is the final guard immediately before the destructive call.
                guarded = self._retention_card(reference, task_id=task_id)
                if guarded is None:
                    self.audit.discard(request_id, event)
                    return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
                _guarded_id, latest, latest_metadata, latest_done_id = guarded
                self._check_retention_record(latest_metadata)
                if not self._retention_matches(
                    latest, latest_metadata, expected_date_moved, cutoff_value, latest_done_id
                ):
                    self.audit.discard(request_id, event)
                    return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
                if not self.client.call("closeTask", task_id=task_id):
                    raise TaskError("backend_error", "board store rejected Done retention", 1)
            except _CommittedWriteError:
                raise self._post_effect_refusal("the Done retention close") from None
            except TaskError as exc:
                # A transport error after close is ambiguous; leave its pending
                # evidence.  A definite local guard/validation failure is not.
                if exc.code == "backend_unavailable":
                    raise self._post_effect_refusal("the Done retention close") from None
                current = self.audit.pending_event(request_id)
                if current == event:
                    try:
                        self.audit.discard(request_id, event)
                    except (OSError, TaskError):
                        pass
                raise
            except Exception:  # noqa: BLE001 - an unknown close reply is deliberately ambiguous.
                # A lost reply can arrive after the board applied the
                # close, so reconciliation must prove or safely retry this episode.
                raise self._post_effect_refusal("the Done retention close") from None
            try:
                self._finish_pending_retired(event)
                self._prove_retired_closed(event)
                self.audit.append(request_id, event)
            except (TaskError, OSError, KeyError, TypeError, ValueError):
                raise self._post_effect_refusal("the Done retention close") from None
            return {"action": "retired", "reference": reference, "retired": True, "replayed": False}

    def restore_card(
        self,
        *,
        reference: str,
        metadata: dict[str, str],
        target: str,
        position: int | None = None,
        swimlane: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        from ummanu.task_restore import restore_card

        return restore_card(self, reference, metadata, target, position, swimlane, request_id)

    def restore_comment(
        self, *, reference: str, body: str, occurrence: int, request_id: str | None = None
    ) -> dict[str, Any]:
        from ummanu.task_restore import restore_comment

        return restore_comment(self, reference, body, occurrence, request_id)

    def reconcile_restore_order(
        self,
        *,
        column: str,
        swimlane: str,
        references: list[str],
        request_id: str,
    ) -> None:
        from ummanu.task_restore import reconcile_restore_order

        reconcile_restore_order(self, column, swimlane, references, request_id)

    def _move_raw(self, task: dict[str, Any], target: str, *, position: int = 1, swimlane_id: int) -> None:
        board_id, columns, _ = self.reader._board()
        column_id = _target_column_id(columns, target)
        if column_id is None:
            raise TaskError("backend_error", "board schema is invalid", 1)
        ok = self.client.call(
            "moveTaskPosition",
            project_id=board_id,
            task_id=_task_number(task),
            column_id=column_id,
            position=position,
            swimlane_id=swimlane_id,
        )
        if not ok:
            raise TaskError("backend_error", "board store rejected the write", 1)

    def _current_swimlane_id(self, task: dict[str, Any]) -> int:
        board_id, _, _ = self.reader._board()
        raw = project_card_by_reference(self.client, board_id, task["ref"])
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        return _positive_int(raw.get("swimlane_id")) or 0

    def _marker_write(
        self,
        *,
        action: str,
        event_kind: EventKind,
        role: str,
        actor: str,
        reference: str,
        reason: str,
        request_id: str | None,
        data: dict[str, Any],
        require_assessment: bool = False,
        fresh_admission: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Send a control-plane marker through the typed host transaction.

        The command supplies its semantic fields once; the host stages them as an immutable event and
        derives the board comment from that event, so a retry never has a second representation to
        drift from.
        """
        request_id = request_id or str(uuid.uuid4())
        if require_assessment:
            current = self.reader.show(reference)
            if current["state"] != "assessment":
                raise TaskError(
                    "transition_forbidden", "a decision is only recorded on a card in Assessment", 3
                )
        try:
            with self._mutation():
                result = self.board_host.marker_comment(
                    MarkerComment(
                        reference,
                        event_kind,
                        Actor(role, actor),
                        reason,
                        data,
                        request_id=request_id,
                        fresh_admission=fresh_admission,
                    )
                )
        except BoardEventPending:
            raise TaskError(
                "audit_pending",
                "backend write committed; audit repair is required",
                4,
            ) from None
        except ValueError as exc:
            message = str(exc)
            if "released generic audit record" in message:
                message = "request id belongs to another operation or payload"
            raise TaskError("validation", message, 2) from None
        except BoardProtocolError as exc:
            raise TaskError("backend_error", str(exc), 1) from None
        return {
            "action": action,
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": result.replayed,
        }

    @serialized
    def _write(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None,
        payload: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
        mutation: Any,
        *,
        identity: dict[str, Any],
        retry_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Every request id explicitly declares the operation identity it owns.
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind=kind, reference=reference, identity=identity)
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {
                "action": kind,
                "task": self.reader.show(reference),
                "event_id": event_id,
                "replayed": True,
            }
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind=kind, reference=reference, identity=identity)
            try:
                self._finish_pending_cleanup(pending, retry_payload)
                task = self.reader.show(str(pending["ref"]))
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {
                "action": kind,
                "task": self.reader.show(reference),
                "event_id": event_id,
                "replayed": True,
            }
        with self._mutation():
            return self._write_effect(
                kind, role, actor, reference, request_id, payload, mutation, identity=identity
            )

    @property
    def _transactional(self) -> bool:
        """Whether `_mutation()` is a real transaction on this backend, asked in one place.

        The boundary and the refusal it produces have to agree about this, so they read the same
        predicate rather than each deciding for itself.
        """
        return getattr(self.client, "transaction", None) is not None

    @contextlib.contextmanager
    def _mutation(self) -> Iterator[None]:
        """One transaction per protocol mutation, where the backend has transactions (§7.1).

        For a client without transactions this is nothing at all: stage a record,
        apply one effect, confirm it, commit the record.  On PostgreSQL the claim, the card effect
        and the event are statements of one transaction, which is why `BoardEventPending` and the
        `recover_*` entry points have nothing to do there (§7.3).
        """
        scope = getattr(self.client, "transaction", None)
        try:
            with scope() if scope is not None else contextlib.nullcontext():
                yield
        except (owner_events.OwnerEventError, BoardEventPending) as exc:
            cause = exc.__cause__ if isinstance(exc, BoardEventPending) else exc
            if not isinstance(cause, owner_events.OwnerEventError):
                raise
            outcome = "mutation rolled back" if scope is not None else "mutation refused"
            raise TaskError(
                "backend_error",
                f"{cause}; {outcome}; restore owner-event availability and retry the same request ID",
                1,
            ) from cause

    def _post_effect_refusal(self, subject: str) -> TaskError:
        """The refusal a mutation inside `_mutation()` owes when it fails after its board effect.

        One sentence used to carry two different facts, and only one of them can be true at a time.
        Where `_mutation()` is nothing — a client without transactions — the effect may well have landed while its
        record did not, and *"backend write committed; audit repair is required"* with exit status
        4 is exactly that fact: it is what `BoardEventPending`, the pending record and the
        `recover_*` entry points exist for, and none of it changes.

        Where `_mutation()` is a real transaction, the same failure has already rolled the effect
        back together with the staged request (§7.1), so that sentence would be false in both
        halves: nothing was committed and nothing is owed.  A caller that believed it would wait
        for a repair `SqlTaskAudit.reconcile` can never perform — it answers `(0, 0)` because there
        is nothing staged — and would treat a retry with the same request id as a resumption when
        the rollback has made it a first attempt.  So there the caller gets an ordinary refusal
        that carries no repair obligation, which is what §7.3 means by the state not existing.
        """
        if not self._transactional:
            return TaskError("audit_pending", "backend write committed; audit repair is required", 4)
        return TaskError(
            "backend_error",
            f"{subject} did not happen: it was rolled back together with its record, "
            "so nothing was written and no repair is owed",
            1,
        )

    def _write_effect(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        payload: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
        mutation: Any,
        *,
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        task = self.reader.show(reference)
        event_payload = payload(task) if callable(payload) else payload
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": kind,
            "outcome": "success",
            "task_id": task["id"],
            "ref": reference,
            "backend": {
                "kind": BOARD_STORE_KIND,
                "task_id": _task_number(task),
                "revision": _revision(task),
            },
            "request_id": request_id,
            "payload": event_payload,
        }
        self.audit.stage(request_id, event)
        try:
            mutation(task)
        except _CommittedWriteError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        except Exception:
            self.audit.discard(request_id)
            raise
        try:
            task = self.reader.show(reference)
        except Exception:  # noqa: BLE001 - any post-write read failure is an ambiguous commit.
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        event["backend"]["revision"] = _revision(task)
        self.audit.stage(request_id, event)
        try:
            event_id = self.audit.append(request_id, event)
        except OSError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        return {"action": kind, "task": task, "event_id": event_id, "replayed": False}

    def reconcile(
        self, *, defer_restore_comments: bool = False, defer_bulk_restore: bool = False
    ) -> tuple[int, int]:
        repaired = 0
        unresolved = 0
        for event in self.audit.pending_events():
            try:
                if defer_restore_comments and event.get("kind") == "restored_comment":
                    continue
                if defer_bulk_restore and event.get("kind") == "restored_bulk":
                    continue
                if event.get("kind") == "restored_bulk":
                    # Normalized restore owns the canon and the whole-board evidence needed to
                    # finish this obligation. Generic per-card reconciliation must not publish it.
                    unresolved += 1
                    continue
                if is_protocol_event(event):
                    subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
                    if str(event.get("kind") or "") in _MARKER_EVENT_ACTIONS:
                        self.board_host.recover_marker_comment(str(event["request_id"]))
                        repaired += 1
                        continue
                    if subject.get("kind") == "sprint":
                        self.board_host.recover_sprint(str(event["request_id"]))
                        repaired += 1
                        continue
                    self._finish_pending_transition(event)
                    repaired += 1
                    continue
                if str(event.get("backend", {}).get("kind") or "") == "dispatcher":
                    # An observer lifecycle event describes a head, not a backend row: there is
                    # nothing to re-read and enrich, and it must repair even when the sprint it
                    # names has already left the board.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") in {
                    "sprint_guard_denied",
                    "sprint_guard_override",
                    "outcome_round_context",
                }:
                    # A guard decision records itself, not a backend row: there is nothing to
                    # re-read, and the decision it names was made whether or not the operation
                    # it authorized went on to succeed.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") == "reference_repaired":
                    from ummanu.board.reference_repair import finish_pending_reference_repair

                    finish_pending_reference_repair(self, event)
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") in {"product_created", "issue_created", "issue_closed"}:
                    # Product/Issue writes have ordered backend cleanup.  Only their supported
                    # command, retried with the original request id, can prove that cleanup.
                    unresolved += 1
                    continue
                if event.get("kind") == "restored_comment":
                    from ummanu.task_restore import finish_pending_restore_comment

                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    finish_pending_restore_comment(self, event, payload)
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") == "restored_order":
                    from ummanu.task_restore import finish_pending_restore_order

                    finish_pending_restore_order(self, event)
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    event["backend"]["revision"] = f"order:{payload.get('references_sha256', '')}"
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if str(event.get("ref") or "").startswith("sprint:"):
                    from ummanu.sprints import SprintWriter

                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if event.get("kind") == "budget_recorded" and payload.get("hard_limit_stop") is True:
                        SprintWriter(self.client, data_dir=self.data_dir).record_budget(
                            role=str(event.get("actor", {}).get("role") or ""),
                            actor=str(event.get("actor", {}).get("id") or ""),
                            reference=str(event["ref"]),
                            event_type=str(payload.get("event_type") or ""),
                            request_id=str(event["request_id"]),
                            source_event_id=str(payload.get("source_event_id") or ""),
                        )
                        repaired += 1
                        continue
                    SprintWriter(self.client, data_dir=self.data_dir)._pending(
                        str(event.get("kind") or "updated"), event
                    )
                    repaired += 1
                    continue
                if event.get("kind") == "retired":
                    self._finish_pending_retired(event)
                    self._prove_retired_closed(event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                self._finish_pending_cleanup(event, None)
                task = (
                    self._pending_create_task(event)
                    if event.get("kind") == "created"
                    else self.reader.show(str(event["ref"]))
                )
                event["task_id"] = task["id"]
                event["backend"]["revision"] = _revision(task)
                self.audit.stage(str(event["request_id"]), event)
                self.audit.append(str(event["request_id"]), event)
                repaired += 1
            except (TaskError, BoardProtocolError, OSError, KeyError, TypeError, ValueError):
                unresolved += 1
        return repaired, unresolved

    def _finish_pending_transition(self, event: dict[str, Any]) -> None:
        """Finish one typed pending Card transition: prove it, clean up, then commit it.

        A typed pending record is read as the transition it declares rather than guessed from a payload.
        The adapter proves the exact target on the board and never repeats a move; the metadata reset
        runs only once that target is live, so a transition whose effect was lost cannot strip a card's
        claim, and the event is published only after the reset is complete.
        """
        transition = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        target = str(transition.get("target") or "")
        ref = str(event["ref"])
        card = self.reader.show(ref)
        if card["state"] == target:
            self._reset_transition_metadata(
                card,
                source=str(transition.get("source") or ""),
                target=target,
            )
            if target in {"ready", "done"}:
                normalized = self.reader.show(ref)
                if (
                    normalized["claim"]["worker"] is not None
                    or normalized["routing"]["resolved_worker_head"] is not None
                    or normalized["routing"]["resolved_review_head"] is not None
                    or normalized["retry"] != {"same": 0, "switched": 0, "heads": []}
                ):
                    raise TaskError("backend_error", "pending Ready cleanup remains incomplete", 1)
        self.board_host.recover_transition(str(event["request_id"]))

    def _finish_pending_cleanup(
        self,
        event: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Complete idempotent backend cleanup before recording a pending event."""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event.get("kind") == "created":
            self._finish_pending_create(event, payload)
            return
        if event.get("kind") == "claimed":
            self._finish_pending_claim(event, payload)
            return
        if event.get("kind") == "restored":
            self._finish_pending_restore(event, payload)
            return
        if event.get("kind") == "archived":
            self._finish_pending_archive(event, retry_payload)
            return
        if event.get("kind") == "retired":
            self._finish_pending_retired(event)
            return
        if event.get("kind") == "decided":
            self._finish_pending_decided(event, payload, retry_payload)
            return
        if event.get("kind") == "restored_comment":
            from ummanu.task_restore import finish_pending_restore_comment

            finish_pending_restore_comment(self, event, payload)
            return
        if event.get("kind") != "moved" or payload.get("to") != "ready":
            return
        task = self.reader.show(str(event["ref"]))
        if task["state"] != "ready":
            raise TaskError("backend_error", "pending move no longer matches task state", 1)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_READY_RESET_METADATA,
        )
        normalized = self.reader.show(str(event["ref"]))
        if (
            normalized["claim"]["worker"] is not None
            or normalized["routing"]["resolved_worker_head"] is not None
            or normalized["routing"]["resolved_review_head"] is not None
            or normalized["retry"] != {"same": 0, "switched": 0, "heads": []}
        ):
            raise TaskError("backend_error", "pending Ready cleanup remains incomplete", 1)

    def _retention_card(
        self, reference: str, *, task_id: int | None
    ) -> tuple[int, dict[str, Any], dict[str, str], int] | None:
        """Read an exact retention target, including archived rows when recovering."""
        board_id, columns, _swimlanes = self.reader._board()
        done_id = next((identifier for identifier, title in columns.items() if title == "Done"), None)
        if done_id is None:
            raise TaskError("backend_error", "board schema is invalid", 1)
        rows = all_project_cards(self.client, board_id)
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and _text(row.get("reference")) == reference
            and (task_id is None or _positive_int(row.get("id")) == task_id)
        ]
        if not matches:
            return None
        if task_id is None:
            active = [row for row in matches if _task_is_active(row)]
            if not active:
                return None
            matches = active
        if len(matches) != 1:
            raise TaskError("backend_error", "Done retention target is ambiguous", 1)
        raw = matches[0]
        number = _task_number(raw)
        return number, raw, _task_metadata(self.client.call("getTaskMetadata", task_id=number)), done_id

    @staticmethod
    def _check_retention_record(metadata: dict[str, str]) -> None:
        if metadata.get("record_type") in _TYPED_RECORD_TYPES:
            raise TaskError(
                "transition_forbidden", "Product issues and products cannot be retired as Done tasks", 3
            )

    def _retention_matches(
        self,
        raw: dict[str, Any],
        metadata: dict[str, str],
        expected_date_moved: int,
        cutoff: float,
        done_id: int,
    ) -> bool:
        return (
            _task_is_active(raw)
            and _positive_int(raw.get("column_id")) == done_id
            and _positive_int(raw.get("date_moved")) == expected_date_moved
            and expected_date_moved < cutoff
            and metadata.get("record_type") not in _TYPED_RECORD_TYPES
        )

    def _finish_pending_retired(self, event: dict[str, Any]) -> None:
        """Prove a retained close or repeat it only for its original Done episode."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        expected = _positive_int(payload.get("expected_date_moved"))
        task_id = _positive_int(payload.get("task_id"))
        try:
            cutoff = float(payload.get("cutoff"))
        except (TypeError, ValueError):
            cutoff = float("nan")
        ref = _text(event.get("ref"))
        if not ref or expected is None or task_id is None or cutoff != cutoff:
            raise TaskError("backend_error", "pending Done retention is incomplete", 1)
        target = self._retention_card(ref, task_id=task_id)
        if target is None:
            raise TaskError("backend_error", "pending Done retention target disappeared", 1)
        _number, raw, metadata, done_id = target
        self._check_retention_record(metadata)
        if _task_is_active(raw):
            if not self._retention_matches(raw, metadata, expected, cutoff, done_id):
                raise TaskError("backend_error", "pending Done retention no longer matches its episode", 1)
            if not self.client.call("closeTask", task_id=task_id):
                raise TaskError("backend_error", "pending Done retention remains incomplete", 1)
            target = self._retention_card(ref, task_id=task_id)
            if target is None:
                raise TaskError("backend_error", "pending Done retention target disappeared", 1)
            _number, raw, _metadata, _done_id = target
        if _task_is_active(raw):
            raise TaskError("backend_error", "pending Done retention remains incomplete", 1)

    def _prove_retired_closed(self, event: dict[str, Any]) -> None:
        """Last audit gate: a success event never names a live replacement episode."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = _positive_int(payload.get("task_id"))
        ref = _text(event.get("ref"))
        if task_id is None or not ref:
            raise TaskError("backend_error", "pending Done retention is incomplete", 1)
        target = self._retention_card(ref, task_id=task_id)
        if target is None or _task_is_active(target[1]):
            raise TaskError("backend_error", "pending Done retention is no longer closed", 1)

    def _finish_pending_claim(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        """Complete a claim whose metadata committed before the column move failed."""
        ref = str(event["ref"])
        worker = str(payload.get("worker") or "")
        if not worker:
            raise TaskError("backend_error", "pending claim is missing its worker id", 1)
        task = self.reader.show(ref)
        # A pending claim on a typed record can only come from before the claim guard existed (or
        # from a card typed after the claim). Finishing it would move a Product or an Issue into
        # In progress, so cleanup fails closed here as well and leaves the event for a PO.
        _check_execution_record(task)
        if task["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim no longer matches task claim", 1)
        if not _matches_optional(payload.get("resolved_head"), task["routing"]["resolved_worker_head"]):
            raise TaskError("backend_error", "pending claim worker head remains incomplete", 1)
        if not _matches_optional(
            payload.get("resolved_review_head"), task["routing"]["resolved_review_head"]
        ):
            raise TaskError("backend_error", "pending claim review head remains incomplete", 1)
        if task["state"] == "ready":
            self._move_raw(task, "in_progress", swimlane_id=self._current_swimlane_id(task))
        elif task["state"] != "in_progress":
            raise TaskError("backend_error", "pending claim no longer matches task state", 1)
        normalized = self.reader.show(ref)
        if normalized["state"] != "in_progress" or normalized["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim cleanup remains incomplete", 1)

    def _finish_pending_decided(
        self,
        event: dict[str, Any],
        payload: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Commit a decision only after its canonical marker and exact body exist on the card."""
        ref = str(event.get("ref") or "")
        marker = str(payload.get("marker") or "")
        expected = str(payload.get("body_sha256") or "")

        def matches(comment: dict[str, Any]) -> bool:
            if comment.get("marker") != marker:
                return False
            rendered = str(comment.get("body") or "")
            prefix = f"[{marker}]\n"
            body = rendered.removeprefix(prefix)
            return _digest(body) == expected

        task = self.reader.show(ref)
        matching = [comment for comment in task.get("comments", []) if matches(comment)]
        if matching:
            return
        body = str((retry_payload or {}).get("decision_body") or "")
        if not body or _digest(body) != expected:
            raise TaskError(
                "audit_pending",
                "pending decision has no verified board comment; retry its original request and body",
                4,
            )
        if task.get("state") != "assessment":
            raise TaskError("backend_error", "pending decision no longer matches Assessment", 1)
        self.client.call(
            "createComment",
            task_id=_task_number(task),
            user_id=0,
            content=f"[{marker}]\n{body}",
        )
        verified = self.reader.show(ref)
        if not any(matches(comment) for comment in verified.get("comments", [])):
            raise TaskError("backend_error", "pending decision comment could not be verified", 1)

    def _finish_pending_archive(
        self,
        event: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Complete an archive whose comment or close committed before the reply was lost."""
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending archive is missing its task ref", 1)
        event_payload = event.get("payload")
        expected_digest = _text(event_payload.get("reason_sha256")) if isinstance(event_payload, dict) else ""
        retry_reason = _text((retry_payload or {}).get("reason"))
        safe_retry_reason = self._redact_for_board(retry_reason)
        if retry_reason and safe_retry_reason != retry_reason:
            # The old pending record predates the board-boundary redactor.  Its
            # digest names the raw text, so silently replacing it would make
            # reconciliation believe a different archive reason was committed.
            # Leave it pending for an operator rather than publish a secret or
            # falsify the append-only history.
            raise TaskError(
                "audit_pending",
                "pending archive reason contains credential material; reissue the archive safely",
                4,
            )
        if retry_reason and _digest(retry_reason) != expected_digest:
            raise TaskError("validation", "archive retry reason does not match the pending request", 2)
        board_id, _, _ = self.reader._board()
        raw = project_card_by_reference(self.client, board_id, ref)
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        if _task_is_active(raw):
            task = self.reader.show(ref)
            self._check_archivable(task)
            self._check_dispatcher_archivable(ref)
            self._request_workspace_cleanup(task, "archive")
            if not _has_archive_reason(task, expected_digest):
                if not retry_reason:
                    raise TaskError("backend_error", "pending archive reason comment is missing", 1)
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[archive]\n{retry_reason}",
                )
                task = self.reader.show(ref)
                if not _has_archive_reason(task, expected_digest):
                    raise TaskError("backend_error", "pending archive reason comment remains incomplete", 1)
            if not self.client.call("closeTask", task_id=_task_number(task)):
                raise TaskError("backend_error", "pending archive remains incomplete", 1)
        elif expected_digest:
            task_id = _positive_int(raw.get("id"))
            if task_id is None:
                raise TaskError("backend_error", "board store returned an invalid task", 1)
            raw_comments = self.client.call("getAllComments", task_id=task_id) or []
            comments = [_normalize_comment(comment) for comment in raw_comments if isinstance(comment, dict)]
            if not _has_archive_reason({"comments": comments}, expected_digest):
                raise TaskError("backend_error", "pending archive reason comment is missing", 1)
        raw = project_card_by_reference(self.client, board_id, ref)
        if isinstance(raw, dict) and _task_is_active(raw):
            raise TaskError("backend_error", "pending archive remains incomplete", 1)
        self._request_workspace_cleanup(self.reader.show(ref), "archive")

    def _finish_pending_restore(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        from ummanu.task_restore import finish_pending_restore

        finish_pending_restore(self, event, payload)

    def _finish_pending_create(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending create is missing its task ref", 1)
        task = self._pending_create_task(event)
        if task["ref"] != ref:
            backend = event.get("backend")
            if isinstance(backend, dict) and backend.get("reference_assignment") == "atomic":
                raise TaskError("backend_error", "pending atomic create reference remains incomplete", 1)
            # Repair a pre-atomic create only if no other row acquired its reference.
            if task["ref"]:
                raise TaskError("backend_error", "pending create task reference does not match", 1)
            board_id, _, _ = self.reader._board()
            current = project_card_by_reference(self.client, board_id, ref)
            if current is not None:
                raise TaskError("backend_error", "pending create reference belongs to another task", 1)
            if not self.client.call("updateTask", id=_task_number(task), reference=ref):
                raise TaskError("backend_error", "pending create reference remains incomplete", 1)
            task = self._pending_create_task(event)
            if task["ref"] != ref:
                raise TaskError("backend_error", "pending create reference remains incomplete", 1)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_create_metadata_values(payload),
        )
        normalized = self._pending_create_task(event)
        expected_mode = _text(payload.get("codex_launch_mode"))
        if expected_mode not in _CODEX_LAUNCH_MODES:
            expected_mode = ""
        if expected_mode and normalized["routing"]["codex_launch_mode"] != expected_mode:
            raise TaskError("backend_error", "pending create metadata remains incomplete", 1)
        if payload.get("steward_report") is True and not (
            normalized["state"] == "in_progress"
            and normalized.get("record_type") == "task"
            and normalized["type"] == "research"
            and normalized["claim"]["worker"] == _text(payload.get("slug"))
            and _is_steward_report(normalized)
        ):
            raise TaskError("backend_error", "pending steward report metadata remains incomplete", 1)

    def _pending_create_task(self, event: dict[str, Any]) -> dict[str, Any]:
        backend = event.get("backend")
        task_id = _positive_int(backend.get("task_id")) if isinstance(backend, dict) else None
        if task_id is not None:
            return self.reader.show_id(task_id)
        raise TaskError(
            "backend_error",
            "pending create is missing its backend task id; reconcile it manually",
            1,
        )

    @staticmethod
    def _role(role: Role | str, allowed: Collection[Role], *, actor: str = "") -> Role:
        """`admit_role`, the one role check; every write entry point passes the actor it writes as."""
        return admit_role(role, actor, allowed)

    @staticmethod
    def _check_archivable(task: dict[str, Any]) -> None:
        state = task["state"]
        if state in ACTIVE_STATES:
            raise TaskError(
                "live_work",
                "archive refuses a card with live worker or reviewer work, or one parked in "
                "Assessment holding a retained worker",
                3,
            )
        if task["claim"]["worker"] is not None:
            raise TaskError("live_work", "archive refuses a card with an active claim", 3)

    def _check_dispatcher_archivable(self, reference: str) -> None:
        state_path = self.data_dir / "dispatcher" / "production-state.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3) from None
        if not isinstance(payload, dict):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
        records = payload.get("records") or {}
        if not isinstance(records, dict):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
        record = records.get(reference)
        if not isinstance(record, dict):
            return
        if _dispatcher_record_has_live_work(record):
            raise TaskError("live_work", "archive refuses a card with live dispatcher work", 3)

    def _request_workspace_cleanup(self, task: dict[str, Any], disposition: str) -> None:
        from ummanu.dispatch.cleanup import CleanupJournal
        from ummanu.dispatch.types import HostError
        try:
            CleanupJournal(self.data_dir).request(task, disposition)
        except HostError as exc:
            raise TaskError("live_work", str(exc), 3) from exc

    @serialized
    def settle_cleanup_claim(self, expected: dict[str, Any], worker: str) -> None:
        """Called only by the cleanup owner after verified head and Git settlement."""
        with self._mutation():
            task = self.reader.show(expected["ref"])
            if (task["id"] != expected["id"] or task.get("claim") != expected.get("claim")
                    or task.get("claim", {}).get("worker") != worker
                    or (not task.get("closed") and task["state"] != "done")):
                raise TaskError("live_work", "cleanup claim changed or remains admitted", 3)
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values={"claim": ""})
            if self.reader.show(expected["ref"]).get("claim", {}).get("worker"):
                raise TaskError("backend_error", "cleanup claim settlement remains pending", 1)




def _target_column_id(columns: dict[int, str], target: str) -> int | None:
    return next(
        (identifier for identifier, name in columns.items() if _STATE_BY_COLUMN.get(name) == target), None
    )


def _has_archive_reason(task: dict[str, Any], reason_sha256: str) -> bool:
    if not reason_sha256:
        return False
    comments = task.get("comments") or []
    if not isinstance(comments, list):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if comment.get("marker") != "archive":
            continue
        body = _text(comment.get("body"))
        reason = body.split("\n", 1)[1] if body.startswith("[archive]\n") else ""
        if _digest(reason) == reason_sha256:
            return True
    return False


def _dispatcher_record_has_live_work(record: dict[str, Any]) -> bool:
    return any(_text(record.get(key)) for key in ("workspace", "handle", "review_handle", "review_leaf"))


def _blocker_refs(task: Mapping[str, Any]) -> list[str]:
    """The card refs `blocked_by` names, in order (the board spells several comma-joined)."""
    return [part.strip() for part in str(task.get("blocked_by") or "").split(",") if part.strip()]


def _task_number(task: dict[str, Any]) -> int:
    """The backend's own number for a normalized card, read through the one identity parser."""
    value = entity_number("task", task.get("id"))
    if value is None:
        raise TaskError("backend_error", "board store returned an invalid task", 1)
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _revision(task: dict[str, Any]) -> str:
    return "updated_at:" + str(task.get("audit", {}).get("updated_at") or "unknown")














def _matching_swimlane(swimlanes: dict[int, str], project: str) -> int | None:
    exact = project.casefold()
    for identifier, name in swimlanes.items():
        if name.casefold() == exact:
            return identifier
    wanted = re.sub(r"[^a-z0-9]+", "", project.lower())
    for identifier, name in swimlanes.items():
        candidate = re.sub(r"[^a-z0-9]+", "", name.lower())
        if candidate == wanted:
            return identifier
    return None


def _is_steward_report(task: dict[str, Any]) -> bool:
    return task.get("extensions", {}).get(EXTENSION_BAG, {}).get("steward_report") == "1"


def _is_steward_report_card(task: dict[str, Any]) -> bool:
    """A card the report create wrote: its marker, research, and no sprint, all as recorded."""
    return _is_steward_report(task) and task.get("type") == "research" and not task.get("sprint")


def _matches_optional(expected: Any, actual: Any) -> bool:
    expected_text = _text(expected)
    return not expected_text or actual == expected_text


def _task_is_active(task: dict[str, Any]) -> bool:
    active = task.get("is_active", task.get("status", 1))
    try:
        return int(active) != 0
    except (TypeError, ValueError):
        return True


def _create_metadata_values(payload: dict[str, Any]) -> dict[str, str]:
    values = {
        "task_type": _text(payload.get("task_type")),
        "project": _text(payload.get("project")),
        "complexity": _text(payload.get("complexity")) or "standard",
        "family_preference": _text(payload.get("family_preference")) or "auto",
    }
    # A create recorded before the review choice was stored names none, and repairs it as none.
    if review := _text(payload.get("review")):
        values["review"] = review
    if payload.get("live_impact") is True:
        values["live_impact"] = "1"
    if wait_spec := _text(payload.get("wait_spec")):
        values[wait_card.WAIT_SPEC] = wait_spec
    if isinstance(origin := payload.get("po_origin"), Mapping) and _text(origin.get("session")):
        values[origin_field.PO_ORIGIN] = origin_field.origin_text(
            _text(origin.get("session")), _text(origin.get("request"))
        )
    if isinstance(execution := payload.get("po_execution"), Mapping):
        values[execution_field.PO_EXECUTION] = execution_field.PoExecution.from_document(execution).text()
    for payload_key, metadata_key in (
        ("blocked_by", "blocked_by"),
        ("head", "head"),
        ("review_head", "review_head"),
        ("slug", "slug"),
        ("base_branch", "base_branch"),
        ("seed_ref", "seed_ref"),
        ("supersedes", "supersedes"),
        ("codex_launch_mode", "codex_launch_mode"),
        ("sprint", "sprint_ref"),
    ):
        value = _text(payload.get(payload_key))
        if metadata_key == "codex_launch_mode" and value not in _CODEX_LAUNCH_MODES:
            # Drop retired launch modes when repairing legacy creates.
            continue
        if value:
            values[metadata_key] = value
    if payload.get("steward_report") is True:
        slug = _text(payload.get("slug"))
        # A recovered report must prove the whole accounting identity, not merely
        # that its row exists.  Keep these in the one metadata write so a retry
        # repairs a partial backend write as one unit.
        values.update({"record_type": "task", "claim": slug, "steward_report": "1"})
    return values


def _rfc3339(value: Any) -> str | None:
    seconds = _positive_int(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    text = _text(comment.get("comment"))
    first_line = text.splitlines()[0] if text else ""
    marker = first_line[1:-1] if first_line.startswith("[") and first_line.endswith("]") else None
    return {"created_at": _rfc3339(comment.get("date_creation")), "body": text, "marker": marker}
