"""Shared task rows, events, request identities and admission errors."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
from collections.abc import Collection, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu._fsutil import file_lock
from ummanu.board import po_execution as execution_field, po_origin as origin_field, wait_card
from ummanu.board.audit_contract import card_transition_of, is_protocol_event
from ummanu.board.backend import entity_number
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.legacy_codec import (
    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,
    positive_int as _positive_int,
    text as _text,
)
from ummanu.board.models import EventKind
from ummanu.board.production_rights import TOUCHES_PRODUCTION
from ummanu.board.protocol_artifacts import ArtifactOwnershipViolation
from ummanu.board.roles import Role
from ummanu.board.sql_rows import now as utc_now, rfc3339
from ummanu.board.task_routing import ACTIVE_STATES, TaskReview, TaskType
from ummanu.kernel.errors import TaskError as TaskError
from ummanu.runtime.head import CODEX_LAUNCH_MODES
from ummanu.runtime.references import BoardRowsUnavailable, board_rows, next_reference

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient


MASQUERADE = (Role.PO, "observer")


def admit_role(role: Role | str, actor: str, allowed: Collection[Role | str]) -> Role:
    """The role check every role-taking sprint, task and issue write makes before
    anything else. Two refusals, in this order.
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
    """Return the durable event id of the description the card currently exposes. A
    digest identifies content, but not an edit that returns a card to earlier text.
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
    """Explain refusal to create a decision, operation or wait card, or return an empty
    reason. Decision/operation cards require a sprint PO session unless created within
    a PO turn (`origin`). Wait cards may be created by the observer for its own sprint,
    by PO, or by the dispatcher for e2e; these kinds have no worker, reviewer, checkout
    or live-impact declaration.
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
    """Whether an event is the dispatcher's create of a sprint `hotfix` card. The
    dispatcher cuts one for a red after-merge e2e run in the open sprint of the newest
    card that run covered; the sprint's observer wakes on it the way it wakes on a
    Blocked card of the sprint.
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
    """Decide whether a card transition wakes the observer or invalidates resume
    freshness. Ignore machine telemetry and observer decisions; a merged release's Done
    is not a wake, but its post-merge CI result is. Other Done transitions remain
    wakes.
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
    from ummanu.board.sql_cards import SqlCardClient

    card = client.call("getTaskByReference", project_id=project_id, reference=reference)
    if not isinstance(card, dict) or _task_is_active(card):
        return card if isinstance(card, dict) else None
    # The store's `task_ref` is the primary key: an archived row has no active duplicate,
    # so the full board read is only for a client without that guarantee.
    if isinstance(client, SqlCardClient):
        return card
    active_cards = client.call("getAllTasks", project_id=project_id, status_id=1)
    if not isinstance(active_cards, list):
        raise TaskError("backend_error", "board store returned an invalid task list", 1)
    for candidate in active_cards:
        if isinstance(candidate, dict) and candidate.get("reference") == reference:
            return candidate
    return card


def project_card_by_id(client: SqlCardClient, project_id: int, task_id: int) -> dict[str, Any] | None:
    """Return the exact board row named by a recorded board task id."""
    from ummanu.board.backend import card_transport_key
    from ummanu.board.sql_cards import SqlCardClient

    if isinstance(client, SqlCardClient) and card_transport_key(task_id) is not None:
        return client.call("getTaskById", project_id=project_id, task_id=task_id)
    for card in all_project_cards(client, project_id):
        if _positive_int(card.get("id")) == task_id:
            return card
    return None


def next_project_reference(client: SqlCardClient, project_id: int, project: str) -> str:
    """Allocate the reference immediately after this project's board-wide high-water mark."""
    from ummanu.board.sql_cards import SqlCardClient

    if isinstance(client, SqlCardClient):
        reference = client.call("getNextTaskReference", project=project)
        if not isinstance(reference, str) or not re.fullmatch(re.escape(project) + r"-[1-9][0-9]*", reference):
            raise TaskError("backend_error", "board store returned an invalid task reference", 1)
        return reference
    return next_reference(all_project_cards(client, project_id), f"{project}-")


@contextlib.contextmanager
def assessment_decision_lock(data_dir: Path, reference: str) -> Iterator[None]:
    """Serialize the complete decision transaction for one card, across observer processes."""
    name = hashlib.sha256(reference.encode("utf-8")).hexdigest() + ".lock"
    with file_lock(data_dir / "board" / "assessment-decisions" / name):
        yield




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


def _audit_repair_required() -> TaskError:
    """The backend write committed but its audit record did not: a later repair finishes it."""
    return TaskError("audit_pending", "backend write committed; audit repair is required", 4)


def _ready_cleanup_incomplete(task: dict[str, Any]) -> bool:
    """A Ready or Done card that still carries a claim, a resolved head or retry history."""
    return (
        task["claim"]["worker"] is not None
        or task["routing"]["resolved_worker_head"] is not None
        or task["routing"]["resolved_review_head"] is not None
        or task["retry"] != {"same": 0, "switched": 0, "heads": []}
    )


def _task_number(task: dict[str, Any]) -> int:
    """The backend's own number for a normalized card, read through the one identity parser."""
    value = entity_number("task", task.get("id"))
    if value is None:
        raise TaskError("backend_error", "board store returned an invalid task", 1)
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return rfc3339(utc_now())


def _charge_time() -> str:
    """When an e2e run is charged to a sprint: the one written form, kept to the microsecond.

    Charges read back ordered by `charged_at` (`sql_sprints._e2e_charges`), so two in one second
    keep their order only at full precision.
    """
    return rfc3339(utc_now(), microseconds=True)


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


def _create_metadata(
    *,
    task_type: str,
    project: str,
    complexity: str,
    family_preference: str,
    review: str,
    live_impact: bool,
    touches_production: str,
    wait_spec: str,
    po_origin: str,
    po_execution: str,
    blocked_by: str,
    head: str,
    review_head: str,
    slug: str,
    base_branch: str,
    seed_ref: str,
    supersedes: str,
    codex_launch_mode: str,
    sprint: str,
    steward_report: bool,
) -> dict[str, str]:
    """The one metadata write of a create, shared by a fresh create and a pending-create repair."""
    values = {
        "record_type": "task",
        "task_type": task_type,
        "project": project,
        "complexity": complexity,
        "family_preference": family_preference,
    }
    if review:
        values["review"] = review
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
    if codex_launch_mode not in _CODEX_LAUNCH_MODES:
        # Drop retired launch modes when repairing legacy creates.
        codex_launch_mode = ""
    for metadata_key, value in (
        ("blocked_by", blocked_by),
        ("head", head),
        ("review_head", review_head),
        ("slug", slug),
        ("base_branch", base_branch),
        ("seed_ref", seed_ref),
        ("supersedes", supersedes),
        ("codex_launch_mode", codex_launch_mode),
        ("sprint_ref", sprint),
    ):
        if value:
            values[metadata_key] = value
    if steward_report:
        # A recovered report must prove the whole accounting identity, not merely
        # that its row exists.  Keep these in the one metadata write so a retry
        # repairs a partial backend write as one unit.
        values.update({"claim": slug, "steward_report": "1"})
    return values


def _create_metadata_values(payload: dict[str, Any]) -> dict[str, str]:
    """The create metadata a staged create payload records, for its repair."""
    origin = payload.get("po_origin")
    execution = payload.get("po_execution")
    return _create_metadata(
        task_type=_text(payload.get("task_type")),
        project=_text(payload.get("project")),
        complexity=_text(payload.get("complexity")) or "standard",
        family_preference=_text(payload.get("family_preference")) or "auto",
        # A create recorded before the review choice was stored names none, and repairs it as none.
        review=_text(payload.get("review")),
        live_impact=payload.get("live_impact") is True,
        touches_production=_text(payload.get("touches_production")),
        wait_spec=_text(payload.get("wait_spec")),
        po_origin=(
            origin_field.origin_text(_text(origin.get("session")), _text(origin.get("request")))
            if isinstance(origin, Mapping) and _text(origin.get("session"))
            else ""
        ),
        po_execution=(
            execution_field.PoExecution.from_document(execution).text()
            if isinstance(execution, Mapping)
            else ""
        ),
        blocked_by=_text(payload.get("blocked_by")),
        head=_text(payload.get("head")),
        review_head=_text(payload.get("review_head")),
        slug=_text(payload.get("slug")),
        base_branch=_text(payload.get("base_branch")),
        seed_ref=_text(payload.get("seed_ref")),
        supersedes=_text(payload.get("supersedes")),
        codex_launch_mode=_text(payload.get("codex_launch_mode")),
        sprint=_text(payload.get("sprint")),
        steward_report=payload.get("steward_report") is True,
    )


def _rfc3339(value: Any) -> str | None:
    seconds = _positive_int(value)
    if seconds is None:
        return None
    return rfc3339(datetime.fromtimestamp(seconds, UTC))


def _normalize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    text = _text(comment.get("comment"))
    first_line = text.splitlines()[0] if text else ""
    marker = first_line[1:-1] if first_line.startswith("[") and first_line.endswith("]") else None
    return {"created_at": _rfc3339(comment.get("date_creation")), "body": text, "marker": marker}
