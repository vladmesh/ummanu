"""Public task reader and writer over the Pipeline PostgreSQL board."""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu.board import (
    e2e_budget,
    e2e_record,
    owner_events,
    po_execution as execution_field,
    po_origin as origin_field,
    wait_card,
)
from ummanu.board.audit_contract import is_protocol_event
from ummanu.board.backend import BOARD_STORE_KIND, entity_id
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
from ummanu.board.events import BoardEventPending
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.host import MutationResult, TransitionRequest
from ummanu.board.legacy_codec import (
    TASK_KNOWN_METADATA as _KNOWN_METADATA,
    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,
    nonnegative_int as _nonnegative_int,
    positive_int as _positive_int,
    text as _text,
)
from ummanu.board.models import Actor, CardState, EntityKind, EventKind, RelatedRefs
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
    create_refusal as production_create_refusal,
    touches_production as card_production,
)
from ummanu.board.protocol_artifacts import ArtifactOwnershipViolation, validate_rework_prerequisites
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
    TASK_COMPLEXITY_VALUES,
    TASK_TYPE_VALUES,
    UNDECIDED_EXITS,
    BlockClassification,
    FamilyPreference,
    TaskComplexity,
    TaskDecision,
    TaskMetadata,
    TaskReview,
    TaskType,
    default_review,
    impact_bounds_refusal,
)
from ummanu.board.transitions import BoardProtocolError
from ummanu.dispatch.cleanup_journal import CleanupJournal as CleanupJournal
from ummanu.dispatch.ownership import (
    bulk_lane as bulk_lane,
    capacity_serialized,
    reference_lock as reference_lock,
)
from ummanu.kernel.capacity_policy import (
    DEFAULT_CODE_CONCURRENCY,
    DEFAULT_HEAD_CAPACITY,
    code_slot_wait,
    head_slot_wait,
)
from ummanu.kernel.errors import TaskError as TaskError
from ummanu.kernel.identifiers import is_machine_id, machine_id_refusal
from ummanu.projects.integration_base import integration_base_refusal, seed_ref_refusal
from ummanu.runtime.redact import redact
from ummanu.runtime.references import (
    reference_allocation_lock,
)
from ummanu.runtime.role_env import RUNTIME_ENV_FILE_ENVS, runtime_env_path

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient

from ummanu.board.task_protocol.dispatcher import DispatcherTaskWrites
from ummanu.board.task_protocol.recovery import RecoveryMixin
from ummanu.board.task_protocol.rows import (
    _BATCH_CHUNK as _BATCH_CHUNK,
    _CODEX_LAUNCH_MODES as _CODEX_LAUNCH_MODES,
    _DECISION_BOUND_ROLES as _DECISION_BOUND_ROLES,
    _MARKER_EVENT_ACTIONS as _MARKER_EVENT_ACTIONS,
    _READY_RESET_METADATA as _READY_RESET_METADATA,
    _TYPED_RECORD_TYPES as _TYPED_RECORD_TYPES,
    _WAIT_FIELDS as _WAIT_FIELDS,
    MASQUERADE as MASQUERADE,
    PO_SESSION_KEY as PO_SESSION_KEY,
    POST_MERGE_CI_KEY as POST_MERGE_CI_KEY,
    POST_MERGE_CI_RESULTS as POST_MERGE_CI_RESULTS,
    RELEASE_MERGE_KEY as RELEASE_MERGE_KEY,
    RUNTIME_TAILS as RUNTIME_TAILS,
    WAIT_CANCELLED as WAIT_CANCELLED,
    WAIT_OUTCOME_KEY as WAIT_OUTCOME_KEY,
    ArtifactOwnershipTaskError as ArtifactOwnershipTaskError,
    SprintReservationUnverifiable as SprintReservationUnverifiable,
    _artifact_ownership_refusal_request_id as _artifact_ownership_refusal_request_id,
    _audit_repair_required as _audit_repair_required,
    _blocker_refs as _blocker_refs,
    _charge_time as _charge_time,
    _check_execution_record as _check_execution_record,
    _CommittedWriteError as _CommittedWriteError,
    _create_metadata as _create_metadata,
    _create_metadata_values as _create_metadata_values,
    _digest as _digest,
    _dispatcher_record_has_live_work as _dispatcher_record_has_live_work,
    _done_retention_request_id as _done_retention_request_id,
    _event_action as _event_action,
    _event_payload as _event_payload,
    _forbidden_move_message as _forbidden_move_message,
    _has_archive_reason as _has_archive_reason,
    _is_steward_report as _is_steward_report,
    _is_steward_report_card as _is_steward_report_card,
    _matches_optional as _matches_optional,
    _matching_swimlane as _matching_swimlane,
    _normalize_comment as _normalize_comment,
    _now as _now,
    _origin_request as _origin_request,
    _po_card_create_refusal as _po_card_create_refusal,
    _projection_slice as _projection_slice,
    _ready_cleanup_incomplete as _ready_cleanup_incomplete,
    _revision as _revision,
    _rfc3339 as _rfc3339,
    _sprint_guard_denial_request_id as _sprint_guard_denial_request_id,
    _sprint_guard_override_request_id as _sprint_guard_override_request_id,
    _target_column_id as _target_column_id,
    _task_is_active as _task_is_active,
    _task_metadata as _task_metadata,
    _task_number as _task_number,
    _transition_reason as _transition_reason,
    _wait_request as _wait_request,
    admit_role as admit_role,
    all_project_cards as all_project_cards,
    assessment_decision_lock as assessment_decision_lock,
    assessment_resolution as assessment_resolution,
    durability_dirt as durability_dirt,
    is_dispatcher_hotfix as is_dispatcher_hotfix,
    is_merged_release as is_merged_release,
    is_significant_card_event as is_significant_card_event,
    is_significant_observer_event as is_significant_observer_event,
    next_project_reference as next_project_reference,
    post_merge_ci_fact as post_merge_ci_fact,
    project_card_by_id as project_card_by_id,
    project_card_by_reference as project_card_by_reference,
    recorded_card_transition as recorded_card_transition,
    specification_revision as specification_revision,
    standing_decision as standing_decision,
    workspace_dirt as workspace_dirt,
)
from ummanu.board.task_protocol.sprint_guard import SprintGuardMixin


class TaskReader:
    def __init__(self, client: SqlCardClient, board_name: str = "Pipeline") -> None:
        self.client = client
        self.board_name = board_name

    def capacity_peers(self) -> list[dict[str, Any]]:
        """Live capacity keys, independent of a tick's selection snapshot."""
        from ummanu.board.sql_cards import SqlCardClient

        if isinstance(self.client, SqlCardClient):
            return [{"ref": reference} for reference in self.client.call("getCapacityReferences")]
        return self.list(states=set(ACTIVE_STATES))

    def list(
        self,
        *,
        states: set[str] | None = None,
        project: str | None = None,
        sprint: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List active cards, or explicitly opt into archived history before filtering."""
        project_id, columns, swimlanes = self._board()
        cards = (
            all_project_cards(self.client, project_id)
            if include_archived
            else self.client.call("getAllTasks", project_id=project_id, status_id=1) or []
        )
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

    def archived_after_merge_cards(self) -> list[dict[str, Any]]:
        """Archived e2e carriers/marks, without the archive's comments or unrelated rows."""
        project_id, columns, swimlanes = self._board()
        rows = self.client.call("getArchivedAfterMergeTasks", project_id=project_id)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise TaskError("backend_error", "board store returned invalid archived carriers", 1)
        metadata = self._metadata_of(rows)
        return [self._normalize(row, columns, swimlanes, metadata[_task_number(row)], comments=None) for row in rows]

    def _attach_origin_returns(self, cards: list[dict[str, Any]]) -> None:
        """Fill each delegated card's `origin.returns` from its outbox rows, in one
        read. The outbox (`board/origin_outbox.py`) is the one record of what a card
        owes its origin session and what was delivered.
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
        """Return the small, durable report view a steward dispatch needs. This
        intentionally is not a second public Card list: callers get only the identity,
        freshness timestamp and report marker needed to decide whether a steward sweep
        is already running.
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
        """Return the bounded operational card view used by steward anomaly reads. This
        is deliberately narrower than :meth:`list`: it exposes only the active-card
        fields a watchdog needs and keeps raw board rows private.
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
        """Return the deliberately small view used by Done-retention cleanup. The
        cleanup is allowed one active-board snapshot and one metadata batch.
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
        canonical Ummanu transport.
        """
        # The installed head registry remains the authority for legacy effective-head values;
        # this is deliberately not a dependency on pipeline board operations or its export CLI.
        from ummanu.board.sql_cards import SqlCardClient
        from ummanu.runtime.heads import HeadRegistryError, default_head, reviewer_head

        def role_default_or_blank(lookup: Callable[[], str]) -> str:
            # A read path: a registry with no role default for this role leaves the effective head
            # blank rather than refusing to list the board.
            try:
                return lookup()
            except HeadRegistryError:
                return ""

        project_id, columns, swimlanes = self._board()
        cards = (
            self.client.call("getBoardRows", project_id=project_id)
            if isinstance(self.client, SqlCardClient)
            else all_project_cards(self.client, project_id)
        )
        if not isinstance(cards, list):
            raise TaskError("backend_error", "board store returned an invalid task list", 1)
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
        Recovery uses this after its setup writes and again for final parity.
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
    """Use the card client's `requests`/`board_events` audit, not an independent file
    journal. `data_dir` is retained for caller compatibility but ignored; SQL-backed
    card events must never be read from an unrelated file journal.
    """
    from ummanu.board.sql_audit import SqlTaskAudit

    return SqlTaskAudit(client)


class TaskWriter(DispatcherTaskWrites, SprintGuardMixin, RecoveryMixin):

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
        """Remove credentials before they reach either board or audit history. An
        interrupted archive keeps its retry body locally and every board comment is
        exported into the checkpoint, so scrubbing happens at the protocol boundary and
        both copies receive the same safe text.
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
        """Create a task under admission rules. Only `wait` accepts wait-specific
        fields (`run`, `run_id`, `card`, `states`, `until`, `deadline`, `returns`,
        `transient_window`). PO-only `origin={session, request}` participates in create
        identity and is rejected for other roles.
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
        if slug and (not is_machine_id(slug) or len(slug) > 30):
            raise TaskError("validation", machine_id_refusal("slug", slug) + ", at most 30 characters", 2)
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
            if blocked_by:
                self._refuse_unreleasable_dependency(blocked_by, supersedes)
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
                raise _audit_repair_required() from None
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
                raise _audit_repair_required() from None
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
                raise _audit_repair_required() from None
            except Exception:
                self.audit.discard(request_id)
                raise
            try:
                task = self.reader.show(created_ref)
            except Exception:  # noqa: BLE001 - any post-create read failure is an ambiguous commit.
                raise _audit_repair_required() from None
            event["task_id"] = task["id"]
            event["ref"] = created_ref
            event["backend"]["revision"] = _revision(task)
            self.audit.stage(request_id, event)
            try:
                event_id = self.audit.append(request_id, event)
            except OSError:
                raise _audit_repair_required() from None
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
        """Create the steward's accounting artifact directly in In progress. The
        generic create transaction owns its reference reservation, staged identity and
        staged-claim recovery.
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
        with reference_allocation_lock(self.data_dir):
            board_id, columns, swimlanes = self.reader._board()
            created_ref = reference or next_project_reference(self.client, board_id, project)
            with reference_lock(self.data_dir, created_ref), self._mutation():
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
                    values = _create_metadata(
                        task_type=task_type,
                        project=project,
                        complexity=complexity,
                        family_preference=family_preference,
                        review=review,
                        live_impact=live_impact,
                        touches_production=touches_production,
                        wait_spec=wait_spec,
                        po_origin=po_origin,
                        po_execution=po_execution,
                        blocked_by=blocked_by,
                        head=head,
                        review_head=review_head,
                        slug=slug,
                        base_branch=base_branch,
                        seed_ref=seed_ref,
                        supersedes=supersedes,
                        codex_launch_mode=codex_launch_mode,
                        sprint=sprint,
                        steward_report=steward_report,
                    )
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

    def _refuse_unreleasable_dependency(self, blocked_by: str, supersedes: str) -> None:
        """A new card's `blocked_by` must name a card that can still reach Done."""
        if blocked_by == supersedes:
            raise TaskError(
                "dependency_unreleasable",
                f"blocked_by names {blocked_by}, the card this one supersedes; a successor does not wait for it",
                2,
            )
        dependency = self.reader.show(blocked_by)
        if dependency["closed"] or dependency["state"] == "blocked":
            held = "archived" if dependency["closed"] else "Blocked"
            raise TaskError(
                "dependency_unreleasable",
                f"blocked_by names {blocked_by}, which is {held}; nothing would release this card from Ready",
                2,
            )

    def comment(
        self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None
    ) -> dict[str, Any]:
        """One role comment on a card. `owner` comments too, on any card, always as
        actor `owner`. The owner is not a board role (it moves nothing and creates
        nothing); its comment is how it answers a card the PO handed to it
        (`handover`), and the dispatcher reads it back by its `owner` marker.
        On a sprint-reserved project, roles other than po, observer, dispatcher and
        owner comment only on their own card: an executor on a card of the holding
        sprint, the steward on its report.
        """
        if str(role) == OWNER_ROLE:
            role, actor = OWNER_ROLE, OWNER_ROLE
        else:
            role = self._role(role, COMMENT_ROLES, actor=actor)
        # Fix the request identity before staging so the answer can name its own audit event.
        request_id = request_id or str(uuid.uuid4())
        if role not in {OWNER_ROLE, "po", "observer", "dispatcher"} and self.audit.committed_event(request_id) is None:
            task = self.reader.show(reference)
            self._guard_sprint_write(
                role=role,
                actor=actor,
                project=task["project"],
                card_sprint=str(task.get("sprint") or ""),
                linked_sprint=None,
                sprint_override=False,
                sprint_override_reason="",
                request_id=request_id,
                reference=reference,
                steward_report=_is_steward_report_card(task),
                card_executor=role in {"worker", "reviewer"},
            )
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
        Only the dispatcher's recovery path uses this adapter.
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
        """Record a release's post-merge CI result as one dispatcher comment on the
        card. The comment carries the fact in its event payload, which is what the
        observer wake predicate reads (`post_merge_ci_fact`); the body is the same fact
        for a human reading the card.
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
        """Refuse a done report from a dirty checkout. The worker runs the protocol
        from its own workspace, so failing here lets it commit and retry inside the
        same session instead of learning from the dispatcher that its card went to
        blocked.
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
        """A worker's report, and for a blocked one the kind of blocker it hit. The
        classification is required rather than offered: an external fact and a wrong
        task definition are repaired by different people.
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
        check_data = {}
        if owned is None and not legacy_owned:
            # This is the writer boundary for a worker report.  Bind the
            # report to the specification it actually answered now, rather
            # than asking a later terminal projection to guess from a mutable
            # card description.
            current = self.reader.show(reference)
            if kind == "done":
                if has_candidate(current):
                    self._require_committed_workspace()
                    from ummanu.broad_check import BroadCheckError
                    from ummanu.check_commands import completion_check
                    from ummanu.config import ConfigError

                    try:
                        evidence = completion_check(str(current.get("project") or ""),
                                                    self.workspace or Path.cwd(), self.instance_dir)
                    except (BroadCheckError, ConfigError) as exc:
                        raise TaskError("done_receipt_required", str(exc), 3) from exc
                    if evidence is not None:
                        check_data = {"worker_check": evidence}
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
            if "worker_check" in marker_data:
                check_data = {"worker_check": marker_data["worker_check"]}
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
                **check_data,
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
        """Complete an In progress delegated card in one transaction: the
        `[completion:<kind>]` comment and Done transition commit together. `po_session`
        is recorded as return evidence, not permission. Request-id retries are
        idempotent; conflicting records are refused before writing. This delegated
        completion does not use the ordinary sprint move guard.
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
        """Hand a delegated card to the owner, preserving In progress. The
        `waiting_owner` mark, `[handover:owner]` comment, unread owner event, prior
        PO-wait settlement and `handed_to_owner` audit commit atomically. Record
        `po_session` as evidence, not authority. Retries are idempotent; conflicting
        requests and invalid state are rejected before writing.
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
        """Cancel an unfrozen wait as PO or its sprint-bound observer. Atomically
        record `wait_cancel`, `[wait:cancel]` and `wait_cancelled`; do not move the
        card. The dispatcher publishes `cancelled` and Blocks it on the next tick. A
        frozen result wins; request-id replay is idempotent.
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
        """Record the observer's decision for a parked card separately from its move;
        leaving Assessment requires the recorded decision. Only the sprint-bound
        observer may decide. PO intervention uses an explicit sprint override, and
        normal reservation/identity guards still apply.
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

    @capacity_serialized
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
        cap: int = DEFAULT_HEAD_CAPACITY,
        code_cap: int = DEFAULT_CODE_CONCURRENCY,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        worker = worker.strip()
        if not worker:
            raise TaskError("validation", "claim requires a non-empty worker id", 2)
        if cap < 1 or code_cap < 0:
            raise TaskError("validation", "claim cap must be positive and code cap non-negative", 2)
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
                for selected in self.reader.capacity_peers()
                if (active := self.reader.show(selected["ref"]))["state"] in ACTIVE_STATES
                and active["id"] != task["id"] and not _is_steward_report(active) and not is_headless(active)
            ]
            code_peers = [
                active["ref"]
                for active in headed
                if active["type"] == "code" and active["project"] == task["project"]
            ]
            if task["type"] == "code" and len(code_peers) >= code_cap:
                raise TaskError("capacity_reached", code_slot_wait(task["project"], code_cap, code_peers), 3)
            if len(headed) >= cap and not is_headless(task):
                raise TaskError("capacity_reached", head_slot_wait(cap, [active["ref"] for active in headed]), 3)

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
        release_claim: bool = False,
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
        if release_claim and (role != "dispatcher" or target != "blocked"):
            raise TaskError("validation", "a claim release belongs on a dispatcher Blocked", 2)
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
                    release_claim=release_claim,
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
                release_claim=release_claim,
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
        """Why the dispatcher may not take one of its two wait edges here, or `""`. In
        progress -> Done is a wait card's `target_reached` or `cancelled`, once its result is frozen;
        Ready -> Blocked is a card held by a wait card (`blocked_by`) that ended
        another way, or the hotfix card the dispatcher itself cut for a red after-merge
        e2e run that no open sprint and no PO origin owns.
        """
        if (source, target) == ("in_progress", "done"):
            result = wait_card.wait_state(task).result if is_wait(task) else None
            if result is None or result.get("outcome") not in (wait_card.TARGET_REACHED, wait_card.CANCELLED):
                return (
                    "the dispatcher moves an In progress card to Done only as a wait card's "
                    "target_reached or cancelled"
                )
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
        """The steward's report card went Blocked carrying "Needs a human": one
        `needs_owner` event. This move is the one place that fact is decided (the
        steward skill, step 5, moves its report card to Blocked with that section as
        the reason), so the event is written here, once per move.
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
        release_claim: bool = False,
    ) -> Callable[[Any], None]:
        """The board work a migrated Card state edge still owes once its column effect
        lands. Handed to the adapter so it runs inside the transition's transaction: it
        completes before the event commits, and an incomplete one leaves the exact
        pending typed record that both a retry and :meth:`reconcile` know how to
        finish.
        """

        def finish(_entity: Any) -> None:
            self._reset_transition_metadata(task, source=source, target=target, release_claim=release_claim)
            if reason.strip():
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[{role}]\n{reason}",
                )

        return finish

    def _reset_transition_metadata(
        self, task: dict[str, Any], *, source: str, target: str, release_claim: bool = False
    ) -> None:
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
        if target in {"ready", "done"} or release_claim:
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={**_READY_RESET_METADATA, **clear_mark}
            )
        elif clear_mark:
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values=clear_mark)
        if source == "validate" and target not in {"ready", "done"}:
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={"resolved_review_head": ""}
            )

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
        """Run claim/move through the typed adapter, combining request staging, board
        effect, `finish` work and committed audit inside `_mutation()` when the backend
        is transactional. The adapter reads the live sprint binding itself.
        Nontransactional backends retain partial-state recovery; PostgreSQL rolls all
        effects back together.
        """
        try:
            with reference_lock(self.data_dir, reference), (
                reference_lock(self.data_dir, "capacity", lane="admission")
                if target.value in ACTIVE_STATES else contextlib.nullcontext()
            ), self._mutation():
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
        """Require every supplied Assessment decision to exist and match the
        destination. Dispatcher exits require a decision; PO moves use sprint override.
        Blocked without a decision is allowed for failure/escalation; observers cannot
        move cards out of Assessment.
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
        The audit event chains old and new content digests.
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
