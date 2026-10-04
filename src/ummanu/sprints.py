"""Sprint entities stored as rows of the dedicated sprint board in the board store."""

from __future__ import annotations

from ummanu.dispatch.cleanup import CleanupJournal, serialized

import fcntl
import functools
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu.board import e2e_budget as sprint_e2e
from ummanu.board import owner_events, owner_decisions
from ummanu.board.backend import (
    BoardIdentityError,
    entity_id,
    entity_number,
    sprint_reference_number,
)
from ummanu.board.card_transitions import CardTransitionForbidden, card_transition
from ummanu.board.e2e_budget import DEFAULT_E2E_BUDGET
from ummanu.board.local_run import (
    LOCAL_RUN_EXCEPTIONS_FIELD,
    parse_local_run_exceptions,
    stored_local_run_exceptions,
)
from ummanu.board.models import SprintState
from ummanu.board.roles import Role
from ummanu.board.sprint_admission import SprintAdmission, SprintReservationIndex
from ummanu.board.sprint_close import (
    SprintCloseConflict,
    SprintCloseDecisions,
    SprintCloseIntent,
    SprintCloseoutPlan,
    SprintCloseSnapshot,
    SprintCloseTargets,
)
from ummanu.board.sprint_read import (
    BUDGET_EVENT_TYPES,
    BUDGET_RECORDED_EVENT_TYPES,
    BUDGET_UNCHARGED_FIELD,
    RESUME_FIELDS,
    SprintBudget,
    SprintReadMetadata,
    SprintResume,
    SprintSourceAudit,
    sprint_string_list,
)
from ummanu.board.sprint_read import (
    BUDGET_UNCHARGED_EVENT_TYPES as BUDGET_UNCHARGED_EVENT_TYPES,
)
from ummanu.board.sprint_read import (
    BUDGET_UNCHARGED_INFRASTRUCTURE as BUDGET_UNCHARGED_INFRASTRUCTURE,
)
from ummanu.board.sprint_read import (
    budget_thresholds as _read_budget_thresholds,
)
from ummanu.board.sprint_write import (
    SprintCreateIntent,
    SprintMutationReceipt,
    SprintReopenIntent,
    SprintWriteSnapshot,
)
from ummanu.runtime.references import BoardRowsUnavailable, board_rows, next_reference
from ummanu.sprint_observer import (
    EXECUTOR_FIELDS,
    KIND_HEAD,
    NONE_SPELLING,
    OBSERVER_FIELD,
    ObserverMetadataError,
    check_observer_profile,
    encode_executor,
    encode_observer,
    installed_head_profiles,
    is_executable,
    parse_observer,
    stored_executors,
)
from ummanu.tasks import (
    BOARD_STORE_KIND,
    TaskError,
    TaskReader,
    TaskWriter,
    _digest,
    _now,
    _positive_int,
    _rfc3339,
    _text,
    admit_role,
    is_significant_observer_event,
    reference_allocation_lock,
    task_audit_for,
)

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient

SPRINT_BOARD_NAME = "Ummanu sprints"
SPRINT_REFERENCE_PREFIX = "sprint:"
#: The two fields a sprint records at create for the PO channel (0016): the PO session that opened
#: it, and the registered projects whose production its operations may touch.
PO_SESSION_FIELD = "sprint_po_session"
ALLOWED_PRODUCTIONS_FIELD = "sprint_allowed_productions"
SPRINT_METADATA = {
    "sprint_goal",
    "sprint_definition_of_done",
    "sprint_repositories",
    "sprint_product",
    "sprint_issues",
    "sprint_reservations",
    "sprint_status",
    "sprint_budget",
    "sprint_budget_uncharged",
    "sprint_current_task",
    "sprint_resume",
    "sprint_source_audit",
    "sprint_observer",
    *EXECUTOR_FIELDS.values(),
    PO_SESSION_FIELD,
    ALLOWED_PRODUCTIONS_FIELD,
    LOCAL_RUN_EXCEPTIONS_FIELD,
    owner_decisions.FIELD,
    sprint_e2e.SPRINT_E2E_BUDGET,
    sprint_e2e.SPRINT_E2E_USED,
    sprint_e2e.SPRINT_E2E_CHARGES,
}
DEFAULT_OPEN_SPRINT_LIMIT = 1
MAX_OPEN_SPRINT_LIMIT = 2
# Observer freshness is based on card transitions, not status-read time.
RESUME_FRESHNESS_GRACE_SECONDS = 5 * 60
_GUARD_INDEX = "sprints/active-repositories.json"
# Version 1 indexes are rebuilt: guards key by project, not repository path.
_GUARD_INDEX_VERSION = 2
_ADMISSION_LOCK = "sprints/admission.lock"
SPRINT_CREATED = "created"
SPRINT_REOPENED = "reopened"
SPRINT_CLOSED = "closed"
#: The step of a close that writes the sprint's knowledge closeout. Its own audit kind, because it
#: is its own step: the committed event under the derived request id is the only proof the document
#: was written, and a retry of the close reads it rather than the repository.
SPRINT_CLOSEOUT = "closeout_written"
SPRINT_STATUSES = {state.value for state in SprintState}
# Terminal sprint states reject semantic writes, so their resume freshness is stable.
SPRINT_TERMINAL_STATUSES = {SprintState.CLOSED.value, SprintState.STOPPED.value}


def public_current_task(status: str, stored: str | None) -> str | None:
    """The current card a read output shows for a sprint: none for a closed one, else the stored one.

    A closed sprint has no current card, whatever its row still holds (PO decision of 2026-09-26,
    issue:002bce88). The stored value is untouched and every writer, the dispatcher and restore keep
    reading it; only what `sprint show`, `sprint status` and `sprint list` print goes through here. A
    stopped sprint may be resumed, so it keeps its card.
    """
    return None if status == SprintState.CLOSED.value else (stored or None)


def active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """Return the local index of projects reserved by open sprints."""
    index = _read_guard_index(Path(data_dir) / _GUARD_INDEX)
    return index.to_projects_document() if index is not None else {}


def _read_guard_index(path: Path) -> SprintReservationIndex | None:
    """Return the typed index, or None when it is absent, unreadable or of an older version."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return SprintReservationIndex.from_document(raw, version=_GUARD_INDEX_VERSION)


def sprint_guard_index_initialized(data_dir: str | Path) -> bool:
    return _read_guard_index(Path(data_dir) / _GUARD_INDEX) is not None


def require_active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """The reserved-project index, or a refusal naming the file that could not answer.

    :func:`active_sprint_projects` answers `{}` for an index that is missing, unreadable or of
    another version, which is the right answer for the write guard -- nothing is reserved that
    cannot be proven reserved. A *read* that reports which reservations a close released cannot use
    it: "this sprint holds no project any more" and "nobody could read the index" are the opposite
    answers, so this one refuses instead of flattening them together.
    """
    index = _read_guard_index(Path(data_dir) / _GUARD_INDEX)
    if index is None:
        raise TaskError(
            "backend_error",
            f"the reserved-project index {_GUARD_INDEX} is missing, unreadable or of another version",
            1,
        )
    return index.to_projects_document()


def refresh_active_sprint_projects(data_dir: str | Path, reader: Any) -> None:
    """Seed the index from the live board without racing a sprint mutation."""
    with _sprint_guard_index_lock(data_dir):
        _replace_active_sprint_projects(data_dir, reader.list(statuses={"open"}, create=False))


def _replace_active_sprint_projects(data_dir: str | Path, sprints: list[dict[str, Any]]) -> None:
    admissions = [SprintAdmission.from_document(sprint) for sprint in sprints]
    _write_guard_index(data_dir, SprintReservationIndex.from_sprints(admissions))


def update_active_sprint_projects(data_dir: str | Path, sprint: SprintAdmission | dict[str, Any]) -> None:
    """Update one sprint's entries in the local reserved-project index."""
    with _sprint_guard_index_lock(data_dir):
        path = Path(data_dir) / _GUARD_INDEX
        index = _read_guard_index(path)
        if index is None and path.exists():
            # Rebuild stale index key spaces from the board.
            path.unlink()
            return
        admission = sprint if isinstance(sprint, SprintAdmission) else SprintAdmission.from_document(sprint)
        index = (index or SprintReservationIndex()).without_sprint(admission.ref)
        if admission.is_open:
            index = index.with_sprint(admission)
        _write_guard_index(data_dir, index)


@contextmanager
def _sprint_guard_index_lock(data_dir: str | Path):
    with _exclusive_lock((Path(data_dir) / _GUARD_INDEX).with_suffix(".lock")):
        yield


@contextmanager
def sprint_admission_lock(data_dir: str | Path):
    """Serialize every admission of an open sprint on this installation.

    The rules for opening a sprint are reads of live state, so two writers that both check before
    either writes would each see no open sprint and both create one. Held across the check and the
    backend write, and nothing after it.
    """
    with _exclusive_lock(Path(data_dir) / _ADMISSION_LOCK):
        yield


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_guard_index(data_dir: str | Path, index: SprintReservationIndex) -> None:
    path = Path(data_dir) / _GUARD_INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            index.to_document(version=_GUARD_INDEX_VERSION),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def budget_thresholds(config: dict[str, Any] | None = None) -> dict[str, int]:
    """Read installation budget limits, retaining safe defaults for old installations."""
    try:
        return _read_budget_thresholds(config)
    except ValueError as exc:
        raise TaskError("validation", str(exc), 2) from None


def open_sprint_limit(config: dict[str, Any] | None = None) -> int:
    """How many sprints this installation may hold open, refusing to widen on bad input.

    An absent, unreadable or malformed setting all answer one. This answers rather than raises, so
    a malformed value cannot stop admission either; `open_sprint_limit_invalid` reports the refusal.
    """
    raw = config.get("open_sprint_limit") if isinstance(config, dict) else None
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_OPEN_SPRINT_LIMIT
    if not DEFAULT_OPEN_SPRINT_LIMIT <= raw <= MAX_OPEN_SPRINT_LIMIT:
        return DEFAULT_OPEN_SPRINT_LIMIT
    return raw


def open_sprint_limit_invalid(config: dict[str, Any] | None = None) -> bool:
    """Whether the setting is present and holds something this installation cannot honour."""
    if not isinstance(config, dict) or "open_sprint_limit" not in config:
        return False
    raw = config["open_sprint_limit"]
    # `True` is an int to Python and would compare equal to the limit it falls back to,
    # so the type is judged before the value.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return True
    return not DEFAULT_OPEN_SPRINT_LIMIT <= raw <= MAX_OPEN_SPRINT_LIMIT


def instance_open_sprint_limit(instance: Path | None) -> int:
    """The limit of one named installation, read at the moment it is asked for.

    Nothing here can widen the limit, which is what makes a malformed setting harmless.
    """
    if instance is None:
        return DEFAULT_OPEN_SPRINT_LIMIT
    from ummanu.config import ConfigError, load_config

    path = instance / "instance.yaml" if instance.is_dir() else instance
    try:
        config = load_config(path)
    except ConfigError:
        return DEFAULT_OPEN_SPRINT_LIMIT
    return open_sprint_limit(config if isinstance(config, dict) else None)


def open_sprint_admission_error(rows: list[dict[str, Any]], *, limit: int) -> str | None:
    """Why this set of open sprints could not have been admitted, or None if it could.

    A whole set is judged by admitting it one row at a time, in reference order, against the rows
    already accepted: an export must not be a way to arrive at a pair `create` would have refused.
    """
    admissions = [SprintAdmission.from_document(row) for row in rows]
    admitted: list[SprintAdmission] = []
    for row in sorted(admissions, key=lambda row: row.ref):
        try:
            _refuse_open_sprint(row, admitted, limit=limit)
        except TaskError as exc:
            return f"{row.ref or '?'}: {exc.message}"
        admitted.append(row)
    return None


def _refuse_open_sprint(
    candidate: SprintAdmission,
    others: list[SprintAdmission],
    *,
    limit: int,
) -> None:
    """Refuse a sprint this installation has no room, or no disjoint room, for.

    Every collision the caller can act on is reported before the generic count refusal, because a
    resource refusal names the sprint holding the resource while the count refusal distinguishes
    none of them.
    """
    saturated = len(others) >= limit
    _refuse_shared_reservations(candidate.reservations, others)
    if limit > DEFAULT_OPEN_SPRINT_LIMIT:
        _refuse_shared_resources(candidate, others)
    if saturated:
        raise _open_sprint_count_error(others, limit)


def _open_sprint_count_error(others: list[SprintAdmission], limit: int) -> TaskError:
    refs = ", ".join(sorted(sprint.ref for sprint in others))
    if limit == DEFAULT_OPEN_SPRINT_LIMIT:
        return TaskError(
            "sprint_conflict",
            f"installation already has an open sprint: {refs}; close it before opening another",
            2,
        )
    return TaskError(
        "sprint_conflict",
        f"installation already holds its limit of {limit} open sprints: {refs}; "
        "close one before opening another",
        2,
    )


def _refuse_shared_reservations(reservations: tuple[str, ...], others: list[SprintAdmission]) -> None:
    """Refuse a project another open sprint already reserves, naming both."""
    held: dict[str, str] = {}
    for sprint in others:
        for project in sprint.reservations:
            held.setdefault(project, sprint.ref)
    clashes = [(project, held[project]) for project in reservations if project in held]
    if clashes:
        raise TaskError(
            "resource_conflict",
            "project(s) already reserved by an open sprint: "
            + ", ".join(f"{project} held by {ref}" for project, ref in sorted(clashes)),
            2,
        )


def _refuse_shared_resources(candidate: SprintAdmission, others: list[SprintAdmission]) -> None:
    """The invariants that make a second open sprint safe, above the reservations.

    Two open sprints may only exist while nothing they work on is shared: a different product, no
    shared project reservation, and no repository tree either contains. Overlap includes nesting
    and is judged on canonical paths; a stored root that is not already absolute is refused rather
    than resolved here, because the tree it names would depend on the resolving process's cwd.

    The candidate's own roots are judged before any pairwise comparison and whether or not another
    sprint is open.
    """
    product = candidate.product
    ordered = sorted(others, key=lambda row: row.ref)
    # Judge both sides so disjointness does not depend on iteration order.
    if ordered and not product:
        raise TaskError(
            "resource_conflict",
            "this sprint declares no product, so it cannot be proven disjoint from "
            f"open sprint {ordered[0].ref!s}",
            2,
        )
    roots = _scanned_roots(
        candidate.repositories,
        refusal=lambda text, why: TaskError(
            "resource_conflict",
            f"this sprint declares repository root {text!r}, which {why}, so it cannot be "
            "proven disjoint from another open sprint",
            2,
        ),
    )
    for sprint in ordered:
        reference = sprint.ref
        other_product = sprint.product
        if not other_product:
            raise TaskError(
                "resource_conflict",
                f"open sprint {reference} declares no product, so a second open sprint "
                "cannot be proven disjoint from it",
                2,
            )
        if other_product == product:
            raise TaskError(
                "resource_conflict",
                f"product {product} is already the product of open sprint {reference}; "
                "a second open sprint needs a different product",
                2,
            )
        held_roots = _scanned_roots(
            sprint.repositories,
            # `reference` is bound here rather than closed over: the callee calls this back
            # inside the same iteration, but a refusal that named the wrong sprint would be a
            # silent lie, and the binding costs nothing.
            refusal=lambda text, why, reference=reference: TaskError(
                "resource_conflict",
                f"open sprint {reference} declares repository root {text!r}, which {why}, "
                "so a second open sprint cannot be proven disjoint from it",
                2,
            ),
        )
        for held in held_roots:
            clash = next((root for root in roots if _roots_overlap(root, held)), None)
            if clash is not None:
                raise TaskError(
                    "resource_conflict",
                    f"repository root {clash} overlaps {held}, held by open sprint {reference}",
                    2,
                )


def ensure_sprint_board(client: SqlCardClient) -> int:
    """Return the dedicated sprint board, creating it once when absent."""
    board_id = _sprint_board(client, create=True)
    if board_id is None:
        raise TaskError("backend_error", "board store did not create the sprint board", 1)
    return board_id


def sprint_client(instance: str | Path):
    """Resolve the process-wide backend for a Sprint-only consumer."""
    from ummanu.board.backend import SPRINT, board_client

    return board_client(instance, serves=(SPRINT,))


def _sprint_board(client: SqlCardClient, *, create: bool) -> int | None:
    board = client.call("getProjectByName", name=SPRINT_BOARD_NAME)
    board_id = _positive_int(board.get("id")) if isinstance(board, dict) else None
    if board_id is None and create:
        board_id = _positive_int(client.call("createProject", name=SPRINT_BOARD_NAME))
    if board_id is None:
        return None
    return board_id


class _AuditOnce:
    """One committed-audit traversal shared by the sprint summaries of a single operation.

    It never opens a store of its own. Either the caller has already walked the committed audit and
    hands the records in (`events`), or it hands in the audit owner its own card client named
    (`audit`), which is what `SprintReader` does. A traversal built from a data directory would be
    the file journal whatever backend the reader is on, and beside a PostgreSQL client that journal
    holds none of the events a resume-freshness verdict is judged against (`docs/BOARD_STORE.md`
    §7.3) -- so there is no such construction to be made here.

    With neither given there is nothing to read, and `events()` says so with an empty traversal: a
    `SprintReader` built with no data directory has no audit at all, and
    that reader's summaries have always been the ones that state no freshness.
    """

    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        audit: Any | None = None,
    ) -> None:
        self._events: list[dict[str, Any]] | None = events
        self._audit = audit

    def events(self, references: set[str] | None = None) -> list[dict[str, Any]]:
        """The committed records of these refs; an audit owner is never read whole from here.

        A walked audit is narrowed here; an audit owner is asked for the slice itself, so a
        summary of one sprint reads that sprint's records and not the history (secretary-1658).
        """
        if self._events is not None:
            if references is None:
                return self._events
            return [event for event in self._events if event.get("ref") in references]
        if self._audit is not None:
            if references is None:
                raise TypeError("an audit owner is read by the refs of a sprint, never whole")
            return self._audit.events(references=references)
        return []


def audit_traversal(events: list[dict[str, Any]]) -> _AuditOnce:
    """A traversal over a committed audit somebody has already walked.

    The journal is a source of its own, and a caller that has to be able to say *the journal*
    refused -- rather than the board it is read beside -- walks it itself and passes the result to
    `status_views`, which then opens nothing. `ummanu.webproto.sprint_reads` is that caller.
    """
    return _AuditOnce(events=events)


def _task_id(raw: dict[str, Any]) -> int:
    """The board identifier of a sprint row, which every read of it needs."""
    task_id = _positive_int(raw.get("id"))
    if task_id is None:
        raise TaskError("backend_error", "board store returned an invalid sprint", 1)
    return task_id


def _sprint_metadata(answer: Any) -> dict[str, str]:
    """One sprint row's metadata, narrowed to the contract fields a sprint is made of."""
    if answer is not None and not isinstance(answer, dict):
        raise TaskError("backend_error", "board store returned invalid sprint metadata", 1)
    return {str(key): _text(value) for key, value in (answer or {}).items() if str(key) in SPRINT_METADATA}


class SprintReader:
    def __init__(
        self,
        client: SqlCardClient,
        *,
        data_dir: str | Path | None = None,
        thresholds: dict[str, int] | None = None,
    ) -> None:
        self.client = client
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.thresholds = (
            budget_thresholds({"sprint_budget": thresholds}) if thresholds else budget_thresholds()
        )
        self.audit = task_audit_for(client)

    def _sprint_rows(self, board_id: int) -> list[dict[str, Any]]:
        """Every row of the sprint board that carries a sprint reference."""
        rows = self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
        if not isinstance(rows, list):
            raise TaskError("backend_error", "board store returned an invalid sprint list", 1)
        return [raw for raw in rows if _is_sprint_row(raw)]

    def _metadata_of(self, rows: list[dict[str, Any]]) -> dict[int, dict[str, str]]:
        """The metadata of every given sprint row, keyed by task id, in one batched read."""
        task_ids = [_task_id(raw) for raw in rows]
        answers = self.client.call_batch(("getTaskMetadata", {"task_id": task_id}) for task_id in task_ids)
        return {task_id: _sprint_metadata(answer) for task_id, answer in zip(task_ids, answers, strict=True)}

    def list(self, *, statuses: set[str] | None = None, create: bool = True) -> list[dict[str, Any]]:
        board_id = _sprint_board(self.client, create=create)
        if board_id is None:
            return []
        rows = self._sprint_rows(board_id)
        metadata = self._metadata_of(rows)
        result = []
        for raw in rows:
            sprint = self._normalize(
                raw,
                metadata[_task_id(raw)],
                comments=None,
                include_resume_freshness=False,
            )
            # Without live cards this value would claim freshness based on incomplete data.  `show`
            # and `status` populate it after reading the linked cards instead.
            sprint.pop("resume_freshness", None)
            if statuses and sprint["status"] not in statuses:
                continue
            result.append(sprint)
        return sorted(result, key=lambda sprint: (sprint["status"], sprint["ref"], sprint["id"]))

    def export(self) -> list[dict[str, Any]]:
        """Every sprint entity with its records, in a deterministic order."""
        board_id = _sprint_board(self.client, create=False)
        if board_id is None:
            return []
        rows = self._sprint_rows(board_id)
        metadata = self._metadata_of(rows)
        all_comments = self.client.call_batch(("getAllComments", {"task_id": _task_id(raw)}) for raw in rows)
        result = []
        for raw, comments_raw in zip(rows, all_comments, strict=True):
            comments = [
                {"created_at": _rfc3339(comment.get("date_creation")), "body": _text(comment.get("comment"))}
                for comment in comments_raw or []
                if isinstance(comment, dict)
            ]
            sprint = self._normalize(
                raw,
                metadata[_task_id(raw)],
                comments=comments,
                include_resume_freshness=False,
            )
            # Freshness needs the linked cards this view deliberately does not read.
            sprint.pop("resume_freshness", None)
            result.append(sprint)
        return sorted(result, key=lambda sprint: (sprint["ref"], sprint["id"]))

    def show(
        self,
        reference: str,
        *,
        include_cards: bool = True,
        include_resume_freshness: bool = True,
        audit: _AuditOnce | None = None,
    ) -> dict[str, Any]:
        board_id = ensure_sprint_board(self.client)
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
        if not isinstance(raw, dict):
            raise TaskError("not_found", "sprint was not found", 2)
        task_id = _task_id(raw)
        comments = None
        if include_cards:
            comments_raw = self.client.call("getAllComments", task_id=task_id) or []
            comments = [
                {"created_at": _rfc3339(comment.get("date_creation")), "body": _text(comment.get("comment"))}
                for comment in comments_raw
                if isinstance(comment, dict)
            ]
        # Freshness requires linked cards, loaded after normalization.
        sprint = self._normalize(
            raw,
            _sprint_metadata(self.client.call("getTaskMetadata", task_id=task_id)),
            comments=comments,
            include_resume_freshness=False,
        )
        if include_cards:
            sprint["cards"] = TaskReader(self.client).list(sprint=reference)
        if include_resume_freshness:
            sprint["resume_freshness"] = self._resume_freshness(sprint, sprint.get("resume"), audit=audit)
        return sprint

    def _normalize(
        self,
        raw: dict[str, Any],
        meta: dict[str, str],
        *,
        comments: list[dict[str, Any]] | None,
        include_resume_freshness: bool = True,
    ) -> dict[str, Any]:
        task_id = _task_id(raw)
        read = SprintReadMetadata.from_legacy(meta, thresholds=self.thresholds, now=_now)
        repositories = list(read.repositories)
        budget = read.budget.to_document()
        try:
            local_run_exceptions = stored_local_run_exceptions(
                meta.get(LOCAL_RUN_EXCEPTIONS_FIELD), projects=_ownership(meta).get("reservations", [])
            )
        except ValueError as exc:
            raise TaskError("backend_error", f"malformed sprint local_run_exceptions: {exc}", 1) from None
        try:
            decisions = owner_decisions.stored_decisions(meta.get(owner_decisions.FIELD))
        except (ValueError, KeyError, TypeError) as exc:
            raise TaskError("backend_error", f"malformed sprint owner_decisions: {exc}", 1) from None
        result: dict[str, Any] = {
            "owner_decisions": decisions,
            "id": entity_id("sprint", task_id),
            "ref": _text(raw.get("reference")),
            "goal": meta.get("sprint_goal", ""),
            "definition_of_done": meta.get("sprint_definition_of_done", ""),
            "repositories": repositories,
            **_ownership(meta),
            **_observer(meta),
            # Always both roles, always a state: "the owner pinned nobody" is an answer this
            # reader gives, never a key it leaves out for the caller to interpret.
            "executors": stored_executors(meta),
            # Null and empty for a sprint opened before either was recorded, never inferred.
            "po_session": meta.get(PO_SESSION_FIELD) or None,
            "allowed_productions": owner_decisions.productions(_json_list(meta.get(ALLOWED_PRODUCTIONS_FIELD)), decisions),
            "local_run_exceptions": local_run_exceptions,
            # The e2e run budget (0023): `e2e: <used> of <budget>`, and the cards that spent the runs.
            "e2e": sprint_e2e.sprint_budget(meta),
            "status": read.state.value,
            "budget": budget,
            "current_task": meta.get("sprint_current_task") or None,
            "audit": {
                "created_at": _rfc3339(raw.get("date_creation")),
                "updated_at": _rfc3339(raw.get("date_modification")),
                "backend": {"kind": BOARD_STORE_KIND, "store_ref": _text(raw.get("reference"))},
                # A restored sprint sits on a fresh board row, so its own dates
                # describe the recovery, not the sprint. The dates it was restored
                # from stay readable here.
                "source": read.source_audit.to_document() if read.source_audit is not None else None,
            },
        }
        if comments is not None:
            result["comments"] = comments
        resume = read.resume.to_document() if read.resume is not None else None
        result["resume"] = resume
        if include_resume_freshness:
            result["resume_freshness"] = self._resume_freshness(result, resume)
        refusal = owner_decisions.e2e_refusal(decisions)
        if refusal:
            result["e2e"]["summary"] += f"; no more e2e (standing decision {result['ref']}/{refusal['id']})"
        return result

    def linked_cards(self) -> dict[str, list[dict[str, Any]]]:
        """Every Pipeline card grouped by the sprint it is linked to, in one listing.

        One pass for the whole installation, not one per sprint: `TaskReader.list` already reads the
        board once and batches the metadata of every row, so a caller that needs the cards of many
        sprints asks for this once and indexes it, exactly as `statuses` does. The Pipeline board is
        read and never created -- `TaskReader` has no `create` -- so this stays a read.
        """
        linked: dict[str, list[dict[str, Any]]] = {}
        for card in TaskReader(self.client).list():
            linked.setdefault(str(card.get("sprint") or ""), []).append(card)
        return linked

    def status_views(
        self,
        sprints: list[dict[str, Any]],
        linked: dict[str, list[dict[str, Any]]],
        *,
        observers: dict[str, dict[str, Any]] | None = None,
        headless: dict[str, dict[str, Any]] | None = None,
        audit: _AuditOnce | None = None,
    ) -> list[dict[str, Any]]:
        """The status view of sprints that have already been read, over cards already listed.

        No board call of its own: it is the assembling half of `statuses`, split out so a caller
        that has to keep the two reads apart -- a protocol layer marking one source unavailable
        without blanking the other -- can still get exactly this view rather than deriving a second
        one beside it. The committed audit is consumed at most once for the whole call.

        `audit` is that split taken one source further: a caller that has already walked the
        committed journal -- and that has to be able to say *the journal* refused rather than the
        board -- passes its own traversal in, and this call opens nothing. With none given the
        journal is walked here, lazily, exactly as `statuses` has always walked it.
        """
        audit = audit if audit is not None else _AuditOnce(audit=self.audit)
        result = []
        for listed in sprints:
            sprint = {**listed, "cards": linked.get(listed["ref"], [])}
            sprint["resume_freshness"] = self._resume_freshness(
                sprint,
                sprint.get("resume"),
                audit=audit,
            )
            result.append(self._status(sprint, (observers or {}).get(sprint["ref"]), headless or {}))
        return result

    def statuses(
        self,
        *,
        observers: dict[str, dict[str, Any]] | None = None,
        headless: dict[str, dict[str, Any]] | None = None,
        create: bool = False,
    ) -> list[dict[str, Any]]:
        """Every sprint's status, reading each part of the board once for the whole call.

        Asking `status` per sprint read the same things over and over: the sprint's metadata, its
        comments, and the entire Pipeline listing with the metadata of every card on it - once per
        sprint. That is what kept `ummanu status` around a minute on a live board with 72
        sprints and 1119 cards. Nothing here is per sprint but the assembling: the sprint rows and
        their metadata are one read each, the cards are one listing shared by every sprint, and the
        committed audit is consumed at most once.
        """
        return self.status_views(
            self.list(create=create),
            self.linked_cards(),
            observers=observers,
            headless=headless,
        )

    def status(
        self,
        reference: str,
        *,
        observer: dict[str, Any] | None = None,
        headless: dict[str, dict[str, Any]] | None = None,
        audit: _AuditOnce | None = None,
    ) -> dict[str, Any]:
        return self._status(self.show(reference, audit=audit), observer, headless or {})

    def _status(
        self,
        sprint: dict[str, Any],
        observer: dict[str, Any] | None,
        headless: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """One sprint's status view over a sprint that has already been read."""
        cards = sprint.get("cards") or []
        states: dict[str, list[str]] = {}
        for card in cards:
            if isinstance(card, dict):
                states.setdefault(str(card.get("state") or "unknown"), []).append(str(card.get("ref") or ""))
        return {
            "ref": sprint["ref"],
            "goal": sprint["goal"],
            "status": sprint["status"],
            **{field: sprint[field] for field in ("product", "issues", "reservations") if field in sprint},
            "current_task": sprint["current_task"],
            "cards": {key: sorted(value) for key, value in sorted(states.items())},
            "budget": sprint["budget"],
            # The last observer decision itself, beside the freshness verdict on it. Reading one
            # without the other is what made "what is this sprint doing" a second read: the entry
            # is already on the row every caller of this view has just read.
            "resume": sprint.get("resume"),
            "resume_freshness": sprint["resume_freshness"],
            "stop_reason": "budget_hard_limit" if sprint["status"] == "stopped" else None,
            "observer": observer or {"state": "unknown"},
            # The declared executor context, beside the live observer state: which profiles this
            # sprint's cards are pinned to, and where the observer is free to choose.
            "executors": sprint.get("executors") or stored_executors({}),
            # The PO session this sprint answers to and the productions it may touch.
            "po_session": sprint.get("po_session"),
            "allowed_productions": list(sprint.get("allowed_productions") or []),
            "owner_decisions": list(sprint.get("owner_decisions") or []),
            "local_run_exceptions": sprint.get("local_run_exceptions", []),
            # The e2e run budget: runs used of the budget, and the cards that spent them.
            "e2e": sprint.get("e2e") or sprint_e2e.sprint_budget({}),
            # This sprint's own cards that owe a worker no dispatcher record can name
            # (secretary-1544). Any column of this sprint, not only In progress: the column is what
            # cannot say it, and a sprint whose only visible signal is "3 in progress" reads as
            # moving while nothing is.
            "degraded_cards": {
                reference: detail
                for reference, detail in sorted((headless or {}).items())
                if any(reference in refs for refs in states.values())
            },
        }

    def _resume_freshness(
        self,
        sprint: dict[str, Any],
        resume: dict[str, Any] | None,
        *,
        audit: _AuditOnce | None = None,
    ) -> dict[str, Any]:
        """The freshness of a sprint's resume, read here and nowhere else.

        An open sprint is judged against the significant linked-card events of the committed audit; a
        closed or stopped sprint against its own record and nothing else, since that record is frozen
        at the terminal transition. The returned shape is the same in every case.
        """
        if not resume:
            return {
                "fresh": False,
                "error": "resume_missing",
                "recorded_at": None,
                "last_event_at": None,
                "lag_seconds": None,
                "threshold_seconds": RESUME_FRESHNESS_GRACE_SECONDS,
            }
        last_event = ""
        terminal = str(sprint.get("status") or "") in SPRINT_TERMINAL_STATUSES
        if self.data_dir is not None and not terminal:
            refs = {
                str(card.get("ref") or "")
                for card in sprint.get("cards") or []
                if isinstance(card, dict) and str(card.get("ref") or "")
            }
            slice_refs = refs | {str(sprint["ref"])}
            if audit is not None:
                source = audit.events(slice_refs)
            else:
                source = self.audit.events(references=slice_refs)
            for event in source:
                if is_significant_observer_event(event, linked_refs=refs, sprint_ref=sprint["ref"]):
                    last_event = max(last_event, str(event.get("occurred_at") or ""))
        recorded_at = str(resume.get("recorded_at") or "")
        lag_seconds = _resume_lag_seconds(recorded_at, last_event)
        invalid_recorded_at = _timestamp(recorded_at) is None
        stale = invalid_recorded_at or bool(
            last_event and (lag_seconds is None or lag_seconds > RESUME_FRESHNESS_GRACE_SECONDS)
        )
        return {
            "fresh": not stale,
            "error": "resume_stale" if stale else None,
            "recorded_at": recorded_at or None,
            "last_event_at": last_event or None,
            "lag_seconds": lag_seconds,
            "threshold_seconds": RESUME_FRESHNESS_GRACE_SECONDS,
        }


def _sql_atomic(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Put one complete Sprint protocol operation in the SQL client's transaction."""

    @functools.wraps(method)
    def wrapped(self: SprintWriter, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request_id = kwargs.get("request_id")
        try:
            # `_depth` is this thread's own, so the check needs no lock around it.
            if self.client._depth:
                return method(self, *args, **kwargs)
            with self.client.transaction():
                # Admission, request claims and reservations share this transaction-scoped lock.
                self.client._execute("SELECT pg_advisory_xact_lock(%s)", (1_600,))
                return method(self, *args, **kwargs)
        except TaskError as exc:
            if isinstance(request_id, str):
                try:
                    self.transactions.drop(request_id)
                except TaskError:
                    pass
            if exc.code == "audit_pending":
                raise TaskError("backend_error", "PostgreSQL Sprint transaction rolled back", 1) from None
            raise
        except Exception as exc:  # noqa: BLE001 - the rollback is the public fact.
            if isinstance(request_id, str):
                try:
                    self.transactions.drop(request_id)
                except TaskError:
                    pass
            raise TaskError("backend_error", f"PostgreSQL Sprint transaction rolled back: {exc}", 1) from None

    return wrapped


class SprintWriter:
    """Sprint mutations with the task protocol's durable audit semantics."""

    def __init__(
        self,
        client: SqlCardClient,
        *,
        data_dir: str | Path,
        thresholds: dict[str, int] | None = None,
        instance: str | Path | None = None,
        po_session_state: Callable[[str], str | None] | None = None,
    ) -> None:
        self.client = client
        self.thresholds = (
            budget_thresholds({"sprint_budget": thresholds}) if thresholds else budget_thresholds()
        )
        # The state of a PO session by id, None when the store has none: the board store's own
        # `po_sessions` unless a caller supplies the answer.
        self._po_session_state = po_session_state or self._stored_po_session_state
        self.reader = SprintReader(client, data_dir=data_dir, thresholds=self.thresholds)
        from ummanu.board.sql_sprints import SqlSprintTransaction

        self.audit = task_audit_for(client)
        self.transactions = SqlSprintTransaction(
            client, self.audit, Path(data_dir) / "board" / "sql-sprint-locks"
        )
        self.data_dir = Path(data_dir)
        self.instance = Path(instance) if instance is not None else None

    def _host(self):
        """Construct the normalized host lazily to keep reader imports acyclic."""
        from ummanu.board.sql_host import SqlBoardHost

        return SqlBoardHost(
            self.client,
            data_dir=str(self.data_dir),
            instance=str(self.instance) if self.instance else None,
            audit=self.audit,
        )

    @staticmethod
    def _host_error(exc: Exception) -> TaskError:
        from ummanu.board.events import BoardEventPending
        from ummanu.board.transitions import BoardProtocolError

        if isinstance(exc, BoardEventPending):
            return TaskError(
                "audit_pending", "Sprint lifecycle write is pending repair; retry with the same request id", 4
            )
        if isinstance(exc, BoardProtocolError) and ("rejected" in str(exc) or "refused" in str(exc)):
            return TaskError("backend_error", str(exc), 1)
        if isinstance(exc, TaskError):
            return exc
        return TaskError("validation", str(exc), 2)

    def _transition_host(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        request_id: str,
        observer: str | None = None,
        budget_by_type: tuple[tuple[str, int], ...] = (),
    ) -> None:
        from ummanu.board import (
            Actor,
            EntityKind,
            RelatedRefs,
            SprintState,
            SprintSupplement,
            TransitionRequest,
        )

        try:
            host = self._host()
            supplement = SprintSupplement(observer=observer, budget_by_type=budget_by_type)
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    reference,
                    SprintState(target),
                    Actor(role, actor),
                    reason,
                    RelatedRefs(),
                    request_id,
                    supplement if (observer is not None or budget_by_type) else None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the host adapter normalizes its public failures.
            raise self._host_error(exc) from None

    def create(
        self,
        *,
        role: str,
        actor: str,
        goal: str,
        definition_of_done: str = "",
        repositories: list[str] | None = None,
        product: str = "",
        issues: list[str] | None = None,
        projects: list[str] | None = None,
        reference: str = "",
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
        worker: str | None = None,
        reviewer: str | None = None,
        po_session: str | None = None,
        allowed_productions: list[str] | None = None,
        e2e_budget: int | None = None,
        local_run_exceptions: list[dict[str, Any]] | None = None,
        standing_decisions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._role(role, {"po", "steward"}, actor=actor)
        request_id = request_id or str(uuid.uuid4())
        intent = self._create_intent(
            role=role,
            actor=actor,
            goal=goal,
            definition_of_done=definition_of_done,
            repositories=repositories or [],
            product=product,
            issues=issues or [],
            reservations=projects or [],
            reference=reference,
            observer=observer,
            worker=worker,
            reviewer=reviewer,
            po_session=po_session,
            allowed_productions=allowed_productions or [],
            e2e_budget=e2e_budget,
            local_run_exceptions=local_run_exceptions,
            standing_decisions=standing_decisions,
        )
        # Lock admission with row creation; resolve request ownership first.
        with sprint_admission_lock(self.data_dir):
            try:
                with self.client.transaction():
                    self.client._execute("SELECT pg_advisory_xact_lock(%s)", (1_600,))
                    return self._create_under_admission(request_id, intent)
            except TaskError as exc:
                if exc.code == "audit_pending":
                    raise TaskError(
                        "backend_error", "PostgreSQL Sprint transaction rolled back", 1
                    ) from None
                raise
            except Exception as exc:  # noqa: BLE001 - rollback is the public fact.
                raise TaskError(
                    "backend_error", f"PostgreSQL Sprint transaction rolled back: {exc}", 1
                ) from None

    def _create_under_admission(self, request_id: str, intent: SprintCreateIntent) -> dict[str, Any]:
        intent_document = intent.to_document()
        document, committed = self.transactions.existing(
            request_id, kind=SPRINT_CREATED, intent=intent_document
        )
        if committed is not None:
            return self._committed_result(SPRINT_CREATED, committed)
        if document is None:
            self._check_ownership(intent.product, list(intent.issues), list(intent.reservations))
            self._check_po_channel(intent)
            self._check_conflicts(intent.admission(), excluding="")
            document, committed = self._begin_create(request_id, intent)
            if committed is not None:
                return self._committed_result(SPRINT_CREATED, committed)
        return self._run_create(document, admitted=True)

    @_sql_atomic
    def restore_create(
        self,
        *,
        reference: str,
        goal: str,
        definition_of_done: str = "",
        repositories: list[str] | None = None,
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
        status: str = "open",
    ) -> dict[str, Any]:
        """Recreate one exported sprint row, without the rules for opening a sprint.

        Not an admission: `restore` writes the real fields immediately after, and several restored
        entities are written one after another, so this must not check or invent ownership.

        Status and observer are written before the reference: the row becomes visible the moment its
        reference lands, and between the two writes a reader would otherwise see an open sprint with no
        observer — the one state the strict reader calls corrupt.
        """
        request_id = request_id or str(uuid.uuid4())
        intent = self._create_intent(
            role="steward",
            actor="restore",
            goal=goal,
            definition_of_done=definition_of_done,
            repositories=repositories or [],
            product="",
            issues=[],
            reservations=[],
            reference=reference,
            require_goal=False,
            observer=observer,
            status=status,
            require_executable_observer=False,
            canonical_repositories=False,
        )
        intent_document = intent.to_document()
        document, committed = self.transactions.existing(
            request_id, kind=SPRINT_CREATED, intent=intent_document
        )
        if committed is not None:
            return self._committed_result(SPRINT_CREATED, committed)
        if document is None:
            document, committed = self._begin_create(request_id, intent)
            if committed is not None:
                return self._committed_result(SPRINT_CREATED, committed)
        return self._run_create(document, admitted=False)

    def _create_intent(
        self,
        *,
        role: str,
        actor: str,
        goal: str,
        definition_of_done: str,
        repositories: list[str],
        product: str,
        issues: list[str],
        reservations: list[str],
        reference: str,
        require_goal: bool = True,
        observer: dict[str, Any] | None = None,
        worker: str | None = None,
        reviewer: str | None = None,
        status: str = "open",
        require_executable_observer: bool = True,
        canonical_repositories: bool = True,
        po_session: str | None = None,
        allowed_productions: list[str] | None = None,
        e2e_budget: int | None = None,
        local_run_exceptions: list[dict[str, Any]] | None = None,
        standing_decisions: list[dict[str, Any]] | None = None,
    ) -> SprintCreateIntent:
        """The normalized request, which is both the replay key and the repair recipe.

        A repeat of the same request id carrying a different intent is another operation and is refused
        before any side effect. The observer is part of the staged intent for the same reason the
        reservations are: a repair has to write the value the caller chose, not one it picks up later.

        Repository roots are canonicalized here, where the operator declaring them is, so the absolute
        root is what the row persists and a root this host cannot resolve is refused before any board
        row, staged intent, metadata or audit event exists. Recovery canonicalizes nothing.
        """
        goal = goal.strip()
        reference = reference.strip()
        if require_goal and not goal:
            raise TaskError("validation", "create requires a non-empty goal", 2)
        if reference and not reference.startswith(SPRINT_REFERENCE_PREFIX):
            raise TaskError("validation", f"sprint reference must start with {SPRINT_REFERENCE_PREFIX}", 2)
        if reference:
            try:
                sprint_reference_number(reference)
            except BoardIdentityError as exc:
                raise TaskError("validation", str(exc), 2) from None
        try:
            state = SprintState(status)
        except ValueError:
            raise TaskError("validation", f"unknown sprint status {status!r}", 2) from None
        pins = self._executor_intent(worker=worker, reviewer=reviewer)
        if e2e_budget is None:
            e2e_budget = DEFAULT_E2E_BUDGET
        if isinstance(e2e_budget, bool) or not isinstance(e2e_budget, int) or e2e_budget < 0:
            raise TaskError("validation", f"--e2e-budget is a whole number of runs, 0 or more; not {e2e_budget!r}", 2)
        try:
            exceptions = parse_local_run_exceptions(
                [] if local_run_exceptions is None else local_run_exceptions, projects=reservations
            )
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        decisions = self._decision_inputs([] if standing_decisions is None else standing_decisions)
        if decisions and role != "po":
            raise TaskError("authorization_refused", "only the PO records quoted owner decisions", 3)
        if e2e_budget + sum(entry["value"] for entry in decisions if entry["kind"] == "e2e_grant") > 2_147_483_647:
            raise TaskError("validation", "e2e budget exceeds the supported finite counter", 2)
        return SprintCreateIntent(
            role=Role(role),
            actor=actor,
            goal=goal,
            definition_of_done=definition_of_done,
            repositories=tuple(
                canonical_repository_roots(repositories)
                if canonical_repositories
                else _unique_strings(repositories)
            ),
            product=product.strip(),
            issues=tuple(_unique_strings(issues)),
            reservations=tuple(_unique_strings(reservations)),
            reference=reference,
            state=state,
            observer=self._observer_intent(observer, executable=require_executable_observer),
            worker=pins["worker"],
            reviewer=pins["reviewer"],
            po_session=str(po_session or "").strip() or None,
            allowed_productions=self._productions_intent(allowed_productions or []),
            e2e_budget=e2e_budget,
            local_run_exceptions=exceptions,
            owner_decisions=tuple(decisions),
        )

    @staticmethod
    def _productions_intent(projects: list[str]) -> tuple[str, ...]:
        """The `--allow-production` values as the intent keeps them: each once, in the order given."""
        cleaned = [str(project).strip() for project in projects]
        if any(not project for project in cleaned):
            raise TaskError("validation", "--allow-production names a registered project; it is empty", 2)
        return tuple(_unique_strings(cleaned))

    def _check_po_channel(self, intent: SprintCreateIntent) -> None:
        """Refuse a PO session that is not open and a production that is not a registered project.

        Both are reads before anything is written, beside `_check_ownership`. Nothing is inferred
        here: no session is guessed and no production is added that the caller did not name.
        """
        if intent.po_session:
            state = self._po_session_state(intent.po_session)
            if state is None:
                raise TaskError(
                    "validation",
                    f"there is no PO session {intent.po_session}; a sprint records the open PO "
                    "session that creates it (--po-session, or $UMMANU_PO_SESSION inside a PO turn)",
                    2,
                )
            if state != "open":
                raise TaskError(
                    "validation",
                    f"PO session {intent.po_session} is {state}; a sprint records an open PO session",
                    2,
                )
        if intent.allowed_productions:
            if self.instance is None:
                raise TaskError(
                    "validation", "--allow-production needs the instance directory; pass --instance", 2
                )
            from ummanu.product_issues import registered_projects

            unknown = sorted(set(intent.allowed_productions) - registered_projects(self.instance))
            if unknown:
                raise TaskError(
                    "validation",
                    "--allow-production names unknown registered project(s): " + ", ".join(unknown),
                    2,
                )

    def _stored_po_session_state(self, session_id: str) -> str | None:
        rows = self.client._query("SELECT state FROM po_sessions WHERE session_id = %s", (session_id,))
        return str(rows[0][0]) if rows else None

    def _observer_intent(
        self,
        observer: dict[str, Any] | None,
        *,
        executable: bool,
    ) -> dict[str, Any] | None:
        """The observer value a create writes, refused here rather than at the backend.

        An operator opening a sprint has to state one: absent, null, empty, `default` and `inherited`
        are not interpretations this model has. Recovery reproduces whatever its export carried,
        including the migration provenance of a closed row, which is not executable.

        A declared profile is resolved against the installation's head registry: a sprint may not be
        opened on a head that does not exist, or the fence would stop its projects on the first tick.
        """
        if observer is None:
            if executable:
                raise TaskError(
                    "validation",
                    "sprint requires an explicit observer; pass --observer <profile> or --observer none",
                    2,
                )
            return None
        value = parse_observer(observer)
        if value is None:
            raise TaskError("validation", "sprint observer is not a valid observer value", 2)
        if not executable:
            return value
        if not is_executable(value):
            raise TaskError(
                "validation",
                "sprint observer must be a concrete head profile or none; migration provenance "
                "is a record of what ran and can never be declared",
                2,
            )
        if value["kind"] == KIND_HEAD:
            # Only a concrete head needs the registry. `none` declares no profile, so a sprint
            # that runs without an observer is not held up by a registry it never asks about.
            try:
                check_observer_profile(
                    value,
                    installed_head_profiles(self.instance),
                    subject="sprint",
                )
            except ObserverMetadataError as exc:
                raise TaskError("validation", exc.message, 2) from None
        return value

    def _executor_intent(self, *, worker: str | None, reviewer: str | None) -> dict[str, str | None]:
        """The worker and reviewer pins a create writes, or `None` for the role it pins nothing on.

        `None` is the operator saying nothing about the role, and it stays a pin nobody made: no
        `role_defaults` value is read here, and no field is written for it. Everything else was
        spelled deliberately and is held to the observer's standard — a profile of this
        installation's head registry, refused at this boundary with the same error the observer's
        own unknown profile is refused with, before any row exists.

        `none` and the empty string are refused rather than folded into the absent state. `--observer
        none` says a sprint runs without an observer, which is a way a sprint can run; a card that
        runs without a worker is not, so the word means nothing here and answering it with silence
        would turn a stated intention into a missing field.
        """
        pins: dict[str, str | None] = {}
        profiles: set[str] | None = None
        for role in EXECUTOR_FIELDS:
            spelling = {"worker": worker, "reviewer": reviewer}[role]
            if spelling is None:
                pins[role] = None
                continue
            text = str(spelling)
            if not text.strip() or text != text.strip():
                raise TaskError(
                    "validation",
                    f"sprint {role} must name a head profile; leave the option out to pin no "
                    "profile and let the observer choose one per card",
                    2,
                )
            if text == NONE_SPELLING:
                raise TaskError(
                    "validation",
                    f"sprint {role} has no {NONE_SPELLING!r}: a sprint whose cards run without a "
                    f"{role} is not a thing to declare. Leave the option out to pin no profile and "
                    "let the observer choose one per card",
                    2,
                )
            if profiles is None:
                try:
                    profiles = installed_head_profiles(self.instance)
                except ObserverMetadataError as exc:
                    raise TaskError("validation", exc.message, 2) from None
            if text not in profiles:
                raise TaskError(
                    "validation",
                    f"sprint {role} names head profile {text!r}, which is not a profile of this "
                    "installation's head registry",
                    2,
                )
            pins[role] = text
        return pins

    def _begin_create(
        self, request_id: str, intent: SprintCreateIntent
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Claim the request id after the mutable preconditions have passed."""
        reference = intent.reference
        if reference:
            board_id = _sprint_board(self.client, create=False)
            if board_id is not None and self.client.call(
                "getTaskByReference", project_id=board_id, reference=reference
            ):
                raise TaskError("validation", "sprint reference already exists", 2)
            try:
                TaskReader(self.client).show(reference)
            except TaskError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise TaskError("validation", "sprint reference already belongs to a Pipeline card", 2)
        intent_document = intent.to_document()
        event = self._event(
            SPRINT_CREATED,
            intent.role.value,
            intent.actor,
            reference,
            request_id,
            {"intent": intent_document},
        )
        document, committed = self.transactions.begin(
            request_id, kind=SPRINT_CREATED, intent=intent_document, event=event
        )
        if document is None and committed is None:
            raise TaskError("audit_pending", "sprint transaction claim is unavailable", 4)
        return document, committed  # type: ignore[return-value]

    def _run_create(self, document: dict[str, Any], *, admitted: bool) -> dict[str, Any]:
        """Drive the staged create to its single audit event, or leave it repairable."""
        try:
            reference = self._finish_create(document, admitted=admitted)
            sprint_document = self.reader.show(reference)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            event = document["event"]
            event["task_id"] = sprint.entity_id
            event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint.admission())
            return SprintMutationReceipt(SPRINT_CREATED, str(event["event_id"])).to_document(sprint_document)
        except TaskError:
            raise
        except (OSError, KeyError, TypeError):
            self._compensate_create(document)
            raise TaskError(
                "audit_pending",
                "sprint create is pending repair; retry with the same request id",
                4,
            ) from None

    def _compensate_create(self, document: dict[str, Any]) -> bool:
        """Take back the row of a create that never got as far as its reference.

        The staged intent stays here either way: it is what a repeat resumes, and only the caller of a
        refusal it will never repeat discards it. Returns whether the request now holds nothing on the
        backend, which is what makes discarding its intent safe.
        """
        progress = document.get("progress") or {}
        task_id = progress.get("task_id")
        if progress.get("reference_done"):
            return False
        if not isinstance(task_id, int):
            return not progress
        try:
            if self.client.call("removeTask", task_id=task_id) is not True:
                return False
            document["progress"] = {}
            self.transactions.save(document)
            return True
        except (TaskError, OSError, KeyError, TypeError):
            return False

    def _finish_create(self, document: dict[str, Any], *, admitted: bool) -> str:
        """Apply every backend sub-step, recognising the ones an earlier attempt did."""
        intent = SprintCreateIntent.from_document(document["intent"])
        event = document["event"]
        progress = document.setdefault("progress", {})
        board_id = ensure_sprint_board(self.client)
        created_ref = self._reference(document, board_id, admitted=admitted)
        # Recheck admission before a resumed create publishes a sprint.
        if admitted and not progress.get("reference_done"):
            staged = progress.get("task_id")
            staged_id = staged if isinstance(staged, int) else None
            self._check_reference_claim(created_ref, staged_id)
            self._check_conflicts(intent.admission(reference=created_ref), excluding_id=staged_id)
        row = self._create_row(document, board_id, created_ref, admitted=admitted)
        task_id = _positive_int(row.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "board store returned an invalid sprint", 1)
        event.update(
            {
                "ref": created_ref,
                "task_id": entity_id("sprint", task_id),
            }
        )
        event["backend"]["task_id"] = task_id
        progress["task_id"] = task_id
        self.transactions.save(document)
        self._ensure_metadata(document, task_id, self._create_values(intent, event=event), step="fields")
        # Write the reference last to publish an atomically admitted sprint.
        if str(row.get("reference") or "") != created_ref:
            progress["reference_started"] = True
            self.transactions.save(document)
            if not self.client.call(
                "updateTask",
                id=task_id,
                reference=created_ref,
                description="",
            ):
                raise TaskError("backend_error", "board store rejected the sprint reference", 1)
            row = self._create_row(document, board_id, created_ref, admitted=admitted)
            if str(row.get("reference") or "") != created_ref:
                raise TaskError("backend_error", "sprint reference remains incomplete", 1)
        progress["reference_done"] = True
        self.transactions.save(document)
        return created_ref

    def _reference(self, document: dict[str, Any], board_id: int, *, admitted: bool) -> str:
        """This request's sprint reference, allocated once and then remembered.

        The reference is what publishes a sprint, so a create that stalled and is being repeated
        writes the one it was already going to write rather than a second one allocated meanwhile.
        A repeat that took its row back holds nothing, including this record, and allocates afresh.

        An automatic reference used to be the row's own board id, which is not a record of what
        the board handed out. Row ids trail the references by hundreds here, so on 2026-08-06 a new
        sprint took `sprint:804` from a sprint closed in July and became unaddressable behind it.

        Only an admitted create allocates. A restore is the one create that adopts a row it finds
        under its reference, because that row is the one it exported and is putting back; a
        reference invented here would let it adopt a row it has never seen. Restoring a sprint
        therefore means naming it, and a restore without a reference is refused rather than given
        somebody else's row.
        """
        intent = SprintCreateIntent.from_document(document["intent"])
        recorded = intent.reference or str(document.get("reference") or "")
        if recorded:
            return recorded
        if not admitted:
            raise TaskError("validation", "a restored sprint must name its own reference", 2)
        with reference_allocation_lock(self.data_dir):
            try:
                rows = board_rows(self.client.call, board_id)
            except BoardRowsUnavailable:
                raise TaskError("backend_error", "board store returned an invalid sprint list", 1) from None
            # Kept beside the staged intent rather than in `progress`, which records what the
            # request holds on the backend: an allocation holds nothing, and a create discarded
            # before it wrote a row hands the number straight back.
            document["reference"] = next_reference(rows, SPRINT_REFERENCE_PREFIX)
            self.transactions.save(document)
        return str(document["reference"])

    def _create_row(
        self,
        document: dict[str, Any],
        board_id: int,
        reference: str,
        *,
        admitted: bool,
    ) -> dict[str, Any]:
        """The row this request created, creating it once when it has none yet.

        A row counts as this request's own only when the staged progress names its task id, or, for
        recovery, when nothing has been written for this reference yet. An admitted create never adopts
        a row it merely shares a reference with.
        """
        rows = [
            row
            for row in self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
            if isinstance(row, dict)
        ]
        staged = (document.get("progress") or {}).get("task_id")
        if isinstance(staged, int):
            row = next((row for row in rows if _positive_int(row.get("id")) == staged), None)
            if row is None:
                raise TaskError("backend_error", "the sprint row of this request was not found", 1)
            return row
        if not admitted:
            row = next((row for row in rows if str(row.get("reference") or "") == reference), None)
            if row is not None:
                return row
        # Unpublished create rows are found by request id, not reference.
        marker = _create_marker(str(document["request_id"]))
        matches = [row for row in rows if str(row.get("description") or "") == marker]
        if len(matches) > 1:
            raise TaskError("backend_error", "pending sprint create correlation is ambiguous", 1)
        if matches:
            return matches[0]
        columns = self.client.call("getColumns", project_id=board_id) or []
        column_id = next(
            (_positive_int(column.get("id")) for column in columns if isinstance(column, dict)), None
        )
        if column_id is None:
            raise TaskError("backend_error", "sprint board has no column", 1)
        document.setdefault("progress", {})["create_started"] = True
        self.transactions.save(document)
        created = _positive_int(
            self.client.call(
                "createTask",
                project_id=board_id,
                title=SprintCreateIntent.from_document(document["intent"]).goal,
                description=marker,
                column_id=column_id,
                reference=reference,
            )
        )
        if created is None:
            raise TaskError("backend_error", "board store rejected the sprint write", 1)
        document["progress"]["task_id"] = created
        self.transactions.save(document)
        rows = [
            row
            for row in self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
            if isinstance(row, dict)
        ]
        row = next((row for row in rows if _positive_int(row.get("id")) == created), None)
        if row is None:
            raise TaskError("backend_error", "the created sprint row was not found", 1)
        return row

    def _create_values(self, intent: SprintCreateIntent, *, event: dict[str, Any] | None = None) -> dict[str, str]:
        values = {
            "sprint_goal": intent.goal,
            "sprint_definition_of_done": intent.definition_of_done,
            "sprint_repositories": json.dumps(list(intent.repositories), separators=(",", ":")),
            "sprint_status": intent.state.value,
            "sprint_budget": _budget_json(SprintBudget.from_legacy(thresholds=self.thresholds).to_document()),
            "sprint_current_task": "",
            "sprint_resume": "",
        }
        # Written with the fields, which is before the reference publishes the row: a sprint is
        # never readable open without the observer it was opened with. A restored row that
        # carried no observer at all keeps carrying none, and the strict reader refuses it.
        if intent.observer is not None:
            values[OBSERVER_FIELD] = encode_observer(intent.observer)
        # A pinned executor is written with the rest of the fields, for the same reason: the row is
        # never readable with cards to cut under a pin the sprint was not opened with. A role the
        # operator pinned nothing on gets no field at all, which is how absence stays absence.
        for role, field in EXECUTOR_FIELDS.items():
            executor = {"worker": intent.worker, "reviewer": intent.reviewer}[role]
            if executor:
                values[field] = encode_executor(executor)
        # Only what the caller named: a sprint without either reads as null and empty.
        if intent.po_session:
            values[PO_SESSION_FIELD] = intent.po_session
        if intent.allowed_productions:
            values[ALLOWED_PRODUCTIONS_FIELD] = json.dumps(
                list(intent.allowed_productions), separators=(",", ":")
            )
        # Only a budget other than the column's default: the read names it only then.
        if intent.e2e_budget != DEFAULT_E2E_BUDGET:
            values[sprint_e2e.SPRINT_E2E_BUDGET] = str(intent.e2e_budget)
        if intent.local_run_exceptions:
            values[LOCAL_RUN_EXCEPTIONS_FIELD] = json.dumps(
                [entry.to_document() for entry in intent.local_run_exceptions],
                sort_keys=True, separators=(",", ":"),
            )
        if intent.owner_decisions:
            assert event is not None
            entries = list(intent.owner_decisions)
            values[owner_decisions.FIELD] = json.dumps(owner_decisions.attributed(entries, event), sort_keys=True, separators=(",", ":"))
            values[ALLOWED_PRODUCTIONS_FIELD] = json.dumps(owner_decisions.productions(list(intent.allowed_productions), entries), separators=(",", ":"))
            values[sprint_e2e.SPRINT_E2E_BUDGET] = str(intent.e2e_budget + sum(entry["value"] for entry in entries if entry["kind"] == "e2e_grant"))
        # A restored legacy row gets no ownership keys at all; `restore` then writes
        # back exactly the fields its own export carried.
        if intent.product:
            values["sprint_product"] = intent.product
        if intent.issues:
            values["sprint_issues"] = json.dumps(list(intent.issues), separators=(",", ":"))
        if intent.reservations:
            values["sprint_reservations"] = json.dumps(list(intent.reservations), separators=(",", ":"))
        return values

    def _ensure_metadata(
        self,
        document: dict[str, Any],
        task_id: int,
        values: dict[str, str],
        *,
        step: str,
    ) -> None:
        """Write one step of the sprint's fields once, and prove the backend kept it.

        The board answers its metadata call with a boolean; anything other than `True` is a refusal. The
        step is recorded durably, so a repair of a later step never rewrites an earlier one back.
        """
        progress = document.setdefault("progress", {})
        if progress.get(f"{step}_done"):
            return
        if not self._metadata_matches(task_id, values):
            progress[f"{step}_started"] = True
            self.transactions.save(document)
            if self.client.call("saveTaskMetadata", task_id=task_id, values=values) is not True:
                raise TaskError("backend_error", "board store rejected the sprint metadata", 1)
            if not self._metadata_matches(task_id, values):
                raise TaskError("backend_error", "sprint metadata remains incomplete", 1)
        progress[f"{step}_done"] = True
        self.transactions.save(document)

    def _metadata_matches(self, task_id: int, values: dict[str, str]) -> bool:
        actual = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(actual, dict):
            raise TaskError("backend_error", "board store returned invalid sprint metadata", 1)
        stored = {str(key): _text(value) for key, value in actual.items()}
        if "sprint_budget" in values:
            if _budget(stored.get("sprint_budget"), self.thresholds) != _budget(
                values["sprint_budget"], self.thresholds
            ):
                return False
            values = {key: value for key, value in values.items() if key != "sprint_budget"}
        return all(stored.get(key) == value for key, value in values.items())

    def _committed_result(self, action: str, committed: dict[str, Any]) -> dict[str, Any]:
        sprint_document = self.reader.show(str(committed["ref"]))
        return SprintMutationReceipt(action, str(committed["event_id"])).to_document(sprint_document)

    def _check_ownership(self, product: str, issues: list[str], reservations: list[str]) -> None:
        """Prove the sprint owns a product, an open issue and registered projects.

        Every step is a read, so a rejected sprint leaves no row, no metadata and no audit event.
        """
        if not product:
            raise TaskError("validation", "sprint requires an owning product; pass --product", 2)
        if not issues:
            raise TaskError(
                "validation", "sprint requires at least one open issue of its product; pass --issue", 2
            )
        if not reservations:
            raise TaskError("validation", "sprint requires at least one reserved project; pass --project", 2)
        if self.instance is None:
            raise TaskError("validation", "sprint ownership needs the instance directory; pass --instance", 2)
        from ummanu.product_issues import ProductIssueStore, registered_projects

        store = ProductIssueStore(self.client, data_dir=self.data_dir, instance=self.instance)
        try:
            store.show_product(product)
        except TaskError as exc:
            if exc.code != "not_found":
                raise
            raise TaskError("not_found", f"sprint product {product!r} was not found", 2) from None
        known = {str(issue.get("ref") or ""): issue for issue in store.list_issues(include_closed=True)}
        for reference in issues:
            issue = known.get(reference)
            if issue is None:
                raise TaskError("not_found", f"issue {reference!r} was not found", 2)
            owner = str(issue.get("product") or "")
            if owner != product:
                raise TaskError(
                    "validation",
                    f"issue {reference!r} belongs to product {owner!r}, not to {product!r}",
                    2,
                )
            if issue.get("closed"):
                raise TaskError(
                    "validation", f"issue {reference!r} is closed; a sprint needs an open issue", 2
                )
        unknown = sorted(set(reservations) - registered_projects(self.instance))
        if unknown:
            raise TaskError("validation", "unknown registered project(s): " + ", ".join(unknown), 2)

    def _check_reference_claim(self, reference: str, staged_id: int | None) -> None:
        """Refuse the reference this create is about to write when another sprint holds it.

        Both paths pass through here. A stalled create holds nothing, including its reference, so
        between its refusal and its repeat another sprint may legitimately open under that
        reference; the repeat is not its owner. An allocated reference is only as free as the
        enumeration it was counted from, and this is where that is proven against the backend.
        """
        board_id = _sprint_board(self.client, create=False)
        if board_id is None:
            return
        row = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
        owner = _positive_int(row.get("id")) if isinstance(row, dict) else None
        if owner is None or owner == staged_id:
            return
        raise TaskError(
            "sprint_conflict",
            f"sprint reference {reference} now belongs to another sprint; "
            "open this sprint again with a new request",
            2,
        )

    def _open_sprint_limit(self) -> int:
        """This installation's limit, read live at the moment admission runs.

        Nothing here can widen the limit, which is what makes a malformed setting harmless.
        """
        return instance_open_sprint_limit(self.instance)

    def _check_conflicts(
        self,
        candidate: SprintAdmission,
        *,
        excluding: str = "",
        excluding_id: int | None = None,
    ) -> None:
        """Refuse a sprint this installation has no room, or no disjoint room, for.

        Every collision the caller can act on is reported before the generic count refusal. A sprint is
        left out of the scan only when it is proven to be the very row this transition is about — the
        row `reopen` reads by reference, or the row a staged create recorded its task id for. A matching
        reference alone proves nothing.
        """
        others = [
            sprint
            for sprint in self.reader.list(statuses={"open"}, create=False)
            if not (
                (excluding and sprint["ref"] == excluding)
                or (excluding_id is not None and _sprint_number(sprint) == excluding_id)
            )
        ]
        _refuse_open_sprint(
            candidate,
            [SprintAdmission.from_document(sprint) for sprint in others],
            limit=self._open_sprint_limit(),
        )

    def comment(
        self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None
    ) -> dict[str, Any]:
        self._role(
            role, {"po", "dispatcher", "worker", "reviewer", "observer", "steward", "retro"}, actor=actor
        )
        if not body.strip():
            raise TaskError("validation", "comment requires a non-empty body", 2)
        # The observer comments on its own sprint only. Guarded outside the transaction, as `resume`
        # is, so a refusal's audit record is not rolled back with the write it refused.
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(role=role, actor=actor, reference=reference, request_id=request_id)
        return self._comment_atomic(
            role=role, actor=actor, reference=reference, body=body, request_id=request_id
        )

    @_sql_atomic
    def _comment_atomic(
        self, *, role: str, actor: str, reference: str, body: str, request_id: str
    ) -> dict[str, Any]:
        return self._write(
            "commented",
            role,
            actor,
            reference,
            request_id,
            {"body_sha256": _digest(body)},
            lambda sprint: self.client.call(
                "createComment", task_id=_sprint_number(sprint), user_id=0, content=f"[{role}]\n{body}"
            ),
        )

    def set_current_task(
        self, *, role: str, actor: str, reference: str, task_reference: str, request_id: str | None = None
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "observer", "steward"}, actor=actor)
        task_reference = task_reference.strip()
        if not task_reference:
            raise TaskError("validation", "current task requires a task reference", 2)
        # Guard the resume pointer to prevent cross-sprint cursor movement.
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(
            role=role,
            actor=actor,
            reference=reference,
            request_id=request_id,
        )

        return self._set_current_task_atomic(
            role=role,
            actor=actor,
            reference=reference,
            task_reference=task_reference,
            request_id=request_id,
        )

    @_sql_atomic
    def _set_current_task_atomic(
        self, *, role: str, actor: str, reference: str, task_reference: str, request_id: str
    ) -> dict[str, Any]:
        def mutation(sprint: SprintWriteSnapshot) -> None:
            task = TaskReader(self.client).show(task_reference)
            if task.get("sprint") != reference:
                raise TaskError("validation", "current task is not linked to this sprint", 2)
            self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={"sprint_current_task": task_reference},
            )

        return self._write(
            "current_task_set", role, actor, reference, request_id, {"task": task_reference}, mutation
        )

    @_sql_atomic
    def set_po_session(
        self, *, role: str, actor: str, reference: str, session_id: str, request_id: str
    ) -> dict[str, Any]:
        """Record the PO session a sprint answers to now; the PO service's resolver is the one caller.

        Not an edit of the sprint's contract: the resolver writes it only when the recorded session
        no longer exists or is closed and it opened a fresh one. The request id makes a repeat the
        same write.
        """
        self._role(role, {"po"}, actor=actor)
        session_id = str(session_id or "").strip()
        if not session_id:
            raise TaskError("validation", "a PO session id is required", 2)

        def mutation(sprint: SprintWriteSnapshot) -> None:
            self.client.call(
                "saveTaskMetadata", task_id=_sprint_number(sprint), values={PO_SESSION_FIELD: session_id}
            )

        # A literal kind, like every `_write` call: the terminal-write table of docs/PROTOCOLS.md
        # is pinned by a test that reads the kinds from these calls.
        return self._write(
            "po_session_set", role, actor, reference, request_id, {"po_session": session_id}, mutation
        )

    def allow_production(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        project: str,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Add one production the sprint's operations may touch: the PO's decision, recorded (secretary-1769).

        Role `po` only, on an open sprint, for a registered project, with a reason. It only adds: a
        project the sprint already allows is a no-op that writes nothing and answers
        `already_allowed`. Nothing else of the sprint's contract is changed here. The request id makes
        a repeat the same write, and binds the sprint and the project.
        """
        self._role(role, {"po"}, actor=actor)
        project = str(project or "").strip()
        reason = str(reason or "").strip()
        if not project:
            raise TaskError("validation", "--project names a registered project; it is empty", 2)
        if not reason:
            raise TaskError("validation", "allowing a production requires a non-empty --reason", 2)
        if self.instance is None:
            raise TaskError("validation", "allowing a production needs the instance directory; pass --instance", 2)
        from ummanu.product_issues import registered_projects

        if project not in registered_projects(self.instance):
            raise TaskError("validation", f"--project names unknown registered project: {project}", 2)
        return self._allow_production_atomic(
            role=role,
            actor=actor,
            reference=reference,
            project=project,
            reason=reason,
            request_id=request_id or str(uuid.uuid4()),
        )

    @_sql_atomic
    def _allow_production_atomic(
        self, *, role: str, actor: str, reference: str, project: str, reason: str, request_id: str
    ) -> dict[str, Any]:
        known = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        allowed: list[str] = []
        if known is not None:
            payload = known.get("payload") if isinstance(known.get("payload"), dict) else {}
            if (known.get("kind"), known.get("ref"), payload.get("project")) != (
                "production_allowed",
                reference,
                project,
            ):
                raise TaskError(
                    "validation",
                    f"request id {request_id!r} already belongs to another sprint write; a request id is "
                    "repeated only with the same sprint and project",
                    2,
                )
        else:
            sprint = self.reader.show(reference)
            status = str(sprint.get("status") or "")
            if status != SprintState.OPEN.value:
                raise TaskError("closed", f"sprint {reference} is {status}; it allows no new production", 3)
            allowed = [str(item) for item in sprint.get("allowed_productions") or []]
            for entry in reversed(sprint.get("owner_decisions") or []):
                if entry["kind"] == "production" and entry["scope"] == project:
                    if not entry["value"]:
                        raise TaskError("authorization_refused", f"standing owner decision {entry['id']} refuses this production; record a later quoted answer", 3)
                    break
            if project in allowed:
                return {"action": "already_allowed", "sprint": sprint, "event_id": None}

        def mutation(sprint: SprintWriteSnapshot) -> None:
            self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={ALLOWED_PRODUCTIONS_FIELD: json.dumps([*allowed, project], separators=(",", ":"))},
            )

        return self._write(
            "production_allowed",
            role,
            actor,
            reference,
            request_id,
            {"project": project, "reason": reason},
            mutation,
        )

    def _decision_inputs(self, entries: Any) -> list[dict[str, Any]]:
        try:
            parsed = owner_decisions.parse_decisions(entries)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        if any(entry["id"].startswith("owner-comment:") for entry in parsed):
            raise TaskError("validation", "owner-comment: IDs are reserved for the genuine owner-comment adapter", 2)
        projects = {entry["scope"] for entry in parsed if entry["kind"] == "production" or entry["scope"] != "sprint"}
        if projects:
            if self.instance is None:
                raise TaskError("validation", "owner decision project scopes need --instance", 2)
            from ummanu.product_issues import registered_projects

            unknown = projects - registered_projects(self.instance)
            if unknown:
                raise TaskError("validation", "unknown owner decision project scopes: " + ", ".join(sorted(unknown)), 2)
        return parsed

    def record_owner_decisions(
        self, *, role: str, actor: str, reference: str, entries: list[dict[str, Any]],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._role(role, {"po"}, actor=actor)
        parsed = self._decision_inputs(entries)
        if not parsed:
            raise TaskError("validation", "record-owner-decisions needs at least one entry", 2)
        return self._record_owner_decisions_atomic(
            role=role, actor=actor, reference=reference, entries=parsed,
            request_id=request_id or str(uuid.uuid4()),
        )

    @_sql_atomic
    def _record_owner_decisions_atomic(
        self, *, role: str, actor: str, reference: str, entries: list[dict[str, Any]], request_id: str,
    ) -> dict[str, Any]:
        # Lock the same row charging updates before reading and projecting the new decisions.
        self.client._execute("SELECT ref FROM sprints WHERE ref = %s FOR UPDATE", (reference,))
        payload = {"entries": entries}
        known = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if known is not None:
            if (known.get("kind"), known.get("ref"), known.get("payload")) != (
                "owner_decisions_recorded", reference, payload,
            ):
                raise TaskError("validation", "request id already belongs to another sprint write", 2)
            return self._write("owner_decisions_recorded", role, actor, reference, request_id, payload, lambda _: None)
        sprint = self.reader.show(reference)
        existing = list(sprint.get("owner_decisions") or [])
        by_id = {entry["id"]: entry for entry in existing}
        added = []
        for entry in entries:
            if entry["id"] in by_id:
                if owner_decisions.input_entry(by_id[entry["id"]]) != entry:
                    raise TaskError("validation", f"owner decision {entry['id']} already has different content", 2)
            else:
                added.append(entry)
        if not added:
            return self._write("owner_decisions_recorded", role, actor, reference, request_id, payload, lambda _: None)
        grant = sum(entry["value"] for entry in added if entry["kind"] == "e2e_grant")
        if int(sprint["e2e"]["budget"]) + grant > 2_147_483_647:
            raise TaskError("validation", "e2e budget exceeds the supported finite counter", 2)

        def mutation(snapshot: SprintWriteSnapshot) -> None:
            event = self.audit.pending_event(request_id)
            assert event is not None
            values = {
                owner_decisions.FIELD: json.dumps([*existing, *owner_decisions.attributed(added, event)], sort_keys=True, separators=(",", ":")),
                ALLOWED_PRODUCTIONS_FIELD: json.dumps(owner_decisions.productions(list(sprint.get("allowed_productions") or []), added), separators=(",", ":")),
            }
            if grant:
                values[sprint_e2e.SPRINT_E2E_BUDGET_ADD] = str(grant)
            self.client.call("saveTaskMetadata", task_id=_sprint_number(snapshot), values=values)

        return self._write("owner_decisions_recorded", role, actor, reference, request_id, payload, mutation)

    def raise_e2e_budget(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        authorized_by: str,
        add: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Raise the sprint's e2e run budget by the runs the owner recorded (secretary-1796).

        Role `po` only, on an open sprint, and only with `authorized_by`: the event id of an owner-role
        comment on one of this sprint's e2e budget decision cards, made after that card was handed to
        the owner, whose one answer line is `e2e budget: raise <N>` (`e2e_budget.authorized_raise`).
        The raise is that N: `add`, when given, has to equal it. The PO applies the owner's answer; it
        has no path to raise the budget on its own authority, and `e2e budget: no` authorizes nothing.
        One authorizing comment raises once. Everything is refused before anything is written. The
        request id defaults to one derived from the authorizing event, so a repeat is the same raise;
        the audit record names the event and N.
        """
        self._role(role, {"po"}, actor=actor)
        if add is not None and (isinstance(add, bool) or not isinstance(add, int) or add < 1):
            raise TaskError("validation", f"--add is a whole number of runs, 1 or more; not {add!r}", 2)
        authorized_by = str(authorized_by or "").strip()
        decision, runs = sprint_e2e.authorized_raise(self.audit, TaskReader(self.client), authorized_by, reference, add)
        return self._raise_e2e_budget_atomic(
            role=role,
            actor=actor,
            reference=reference,
            add=runs,
            authorized_by=authorized_by,
            decision=decision,
            request_id=request_id or f"e2e-budget-raise-{authorized_by}",
        )

    @_sql_atomic
    def _raise_e2e_budget_atomic(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        add: int,
        authorized_by: str,
        decision: str,
        request_id: str,
    ) -> dict[str, Any]:
        self.client._execute("SELECT ref FROM sprints WHERE ref = %s FOR UPDATE", (reference,))
        identity = (sprint_e2e.SPRINT_BUDGET_RAISED, reference, add, authorized_by)
        known = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if known is not None:
            payload = known.get("payload") if isinstance(known.get("payload"), dict) else {}
            if (known.get("kind"), known.get("ref"), payload.get("add"), payload.get("authorized_by")) != identity:
                raise TaskError(
                    "validation",
                    f"request id {request_id!r} already belongs to another sprint write; a raise is repeated "
                    "only with the same sprint, --add and --authorized-by",
                    2,
                )
        else:
            if any(entry["id"] == f"owner-comment:{authorized_by}" for entry in self.reader.show(reference).get("owner_decisions") or []):
                raise TaskError("authorization_refused", f"the owner's comment {authorized_by} already has a grant entry; each answer raises once", 3)
            for event in self.audit.events(reference, kind=sprint_e2e.SPRINT_BUDGET_RAISED):
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                if payload.get("authorized_by") == authorized_by:
                    raise TaskError(
                        "authorization_refused",
                        f"the owner's comment {authorized_by} already authorized a raise of {reference} "
                        f"({event.get('request_id')}); each answer raises once",
                        3,
                    )

        def mutation(sprint: SprintWriteSnapshot) -> None:
            current = self.reader.show(reference)
            if int(current["e2e"]["budget"]) + add > 2_147_483_647:
                raise TaskError("validation", "e2e budget exceeds the supported finite counter", 2)
            authorizing = self.audit.committed_event(self.audit.event_id_owner(authorized_by))
            body = sprint_e2e._comment_body(TaskReader(self.client), decision, authorizing["payload"]["body_sha256"])
            if body is None:
                raise TaskError("authorization_refused", "the authorizing owner quotation is no longer readable", 3)
            entry = {"id": f"owner-comment:{authorized_by}", "kind": "e2e_grant", "scope": "sprint", "value": add, "quotation": body}
            recorded = owner_decisions.attributed([entry], self.audit.pending_event(request_id))
            self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={sprint_e2e.SPRINT_E2E_BUDGET_ADD: str(add),
                        owner_decisions.FIELD: json.dumps([*(current.get("owner_decisions") or []), *recorded], sort_keys=True, separators=(",", ":"))},
            )

        return self._write(
            "e2e_budget_raised",
            role,
            actor,
            reference,
            request_id,
            {"add": add, "authorized_by": authorized_by, "decision": decision},
            mutation,
        )

    def record_budget(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        event_type: str,
        request_id: str | None = None,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        result = self._record_budget(
            role=role,
            actor=actor,
            reference=reference,
            event_type=event_type,
            request_id=request_id,
            source_event_id=source_event_id,
        )
        sprint = result.get("sprint") if isinstance(result.get("sprint"), dict) else {}
        budget = sprint.get("budget") if isinstance(sprint.get("budget"), dict) else {}
        if budget.get("signal_reached"):
            # Once per sprint: the charges only grow, so the signal once reached stays reached.
            owner_events.record(
                owner_events.BUDGET_SIGNAL,
                reference,
                f"{reference} reached its budget signal: {budget.get('total')} restart events of "
                f"{(budget.get('thresholds') or {}).get('hard')} before the hard stop",
                f"{owner_events.BUDGET_SIGNAL}:{reference}",
                to=self.client,
            )
        return result

    @_sql_atomic
    def _record_budget(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        event_type: str,
        request_id: str | None = None,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "steward"}, actor=actor)
        if event_type not in BUDGET_RECORDED_EVENT_TYPES:
            raise TaskError("validation", "unknown budget event type " + repr(event_type), 2)
        # One recording path for both families; only the charge is conditional. An uncharged type
        # can never reach the hard limit, so it never takes the typed hard-stop edge below.
        charged = event_type in BUDGET_EVENT_TYPES
        request_id = request_id or str(uuid.uuid4())
        existing = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if existing is not None:
            if existing.get("record_type") == "board.protocol_event":
                raise TaskError(
                    "validation",
                    "request id belongs to a typed Sprint lifecycle occurrence; retry its protocol recovery",
                    2,
                )
            payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
            if existing.get("kind") != "budget_recorded":
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            if bool(payload.get("hard_limit_stop")):
                return self._finish_hard_budget(
                    role=role,
                    actor=actor,
                    reference=reference,
                    event_type=event_type,
                    request_id=request_id,
                    source_event_id=source_event_id,
                    event=existing,
                )
            return (
                self._committed("budget_recorded", existing)
                if self.audit.committed_event(request_id)
                else self._pending("budget_recorded", existing)
            )
        before_document = self.reader.show(reference)
        before = SprintWriteSnapshot.from_document(before_document, thresholds=self.thresholds)
        before_budget = before.budget
        hard_stop = (
            charged
            and before.state is SprintState.OPEN
            and before_budget.total + 1 >= self.thresholds["hard"]
        )
        if hard_stop:
            counts = dict(before_budget.by_type)
            counts[event_type] += 1
            budget = SprintBudget.from_legacy({"by_type": counts}, thresholds=self.thresholds)
            event = self._event(
                "budget_recorded",
                role,
                actor,
                reference,
                request_id,
                {
                    "event_type": event_type,
                    "source_event_id": source_event_id or None,
                    "hard_limit_stop": True,
                    "budget": {"by_type": dict(budget.by_type)},
                },
                before,
            )
            self.audit.stage(request_id, event)
            return self._finish_hard_budget(
                role=role,
                actor=actor,
                reference=reference,
                event_type=event_type,
                request_id=request_id,
                source_event_id=source_event_id,
                event=event,
            )

        def mutation(sprint: SprintWriteSnapshot) -> None:
            budget = sprint.budget
            if charged:
                counts = dict(budget.by_type)
                counts[event_type] += 1
                normalized = SprintBudget.from_legacy({"by_type": counts}, thresholds=self.thresholds)
                values = {"sprint_budget": _budget_json(normalized.to_document())}
            else:
                uncharged = dict(budget.uncharged)
                uncharged[event_type] += 1
                values = {
                    BUDGET_UNCHARGED_FIELD: json.dumps(uncharged, sort_keys=True, separators=(",", ":"))
                }
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=values)

        return self._write(
            "budget_recorded",
            role,
            actor,
            reference,
            request_id,
            {
                "event_type": event_type,
                "source_event_id": source_event_id or None,
                "hard_limit_stop": False,
            },
            mutation,
        )

    def _finish_hard_budget(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        event_type: str,
        request_id: str,
        source_event_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Finish one hard charge and its stopped state as one host-owned effect.

        The generic charge is the request owner and is staged first, but it may not publish until the
        typed hard-stop occurrence has staged, persisted the computed budget plus stopped state, and
        committed. A retry always drives that typed owner before publishing its charge.
        """
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if (
            event.get("kind") != "budget_recorded"
            or event.get("ref") != reference
            or event.get("actor") != {"role": role, "id": actor}
            or payload.get("event_type") != event_type
            or payload.get("source_event_id") != (source_event_id or None)
            or payload.get("hard_limit_stop") is not True
        ):
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        stored_budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
        by_type = stored_budget.get("by_type") if isinstance(stored_budget.get("by_type"), dict) else None
        if by_type is None or set(by_type) != set(BUDGET_EVENT_TYPES):
            raise TaskError("audit_pending", "hard-budget record lacks its normalized budget", 4)
        counts = tuple(sorted((name, value) for name, value in by_type.items()))
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for _name, value in counts):
            raise TaskError("audit_pending", "hard-budget record has an invalid normalized budget", 4)
        try:
            self._transition_host(
                role=role,
                actor=actor,
                reference=reference,
                target="stopped",
                reason="budget hard limit reached",
                request_id=request_id + ":typed-hard-stop",
                budget_by_type=counts,
            )
        except TaskError:
            # A generic charge may wait only on the exact typed occurrence
            # that owns the state effect.  If staging/effect admission failed
            # before that occurrence survived, retain neither record: the
            # released generic write discarded on every failed mutation, and
            # otherwise this request can become stranded once the Sprint
            # changes state through another command.
            typed_request_id = request_id + ":typed-hard-stop"
            if self.audit.event(typed_request_id) is None:
                self.audit.discard(request_id, event)
            raise
        result = (
            self._committed("budget_recorded", event)
            if self.audit.committed_event(request_id)
            else self._pending("budget_recorded", event)
        )
        self._record_hard_stop(
            role=role,
            actor=actor,
            reference=reference,
            request_id=request_id,
            budget_event_id=str(event.get("event_id") or ""),
            event_type=event_type,
            source_event_id=source_event_id,
        )
        # The charge that stopped the sprint, once: written in this transaction, beside the stop.
        owner_events.record(
            owner_events.SPRINT_STOPPED,
            reference,
            f"{reference} was stopped: its budget reached the hard limit ({event_type} charged)",
            f"{owner_events.SPRINT_STOPPED}:{reference}:{request_id}",
            to=self.client,
        )
        # The generic event is deliberately returned only after the typed host
        # transition, so callers and dispatcher output observe the stopped row.
        result["sprint"] = self.reader.show(reference)
        return result

    def _record_hard_stop(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        budget_event_id: str,
        event_type: str,
        source_event_id: str,
    ) -> None:
        """Record the state transition separately from the charge that caused it."""
        stop_request_id = request_id + ":budget-hard-stop"
        if self.audit.committed_event(stop_request_id) is not None:
            return
        sprint_document = self.reader.show(reference)
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        event = self._event(
            "budget_hard_stopped",
            role,
            actor,
            reference,
            stop_request_id,
            {
                "reason": "budget_hard_limit",
                "budget_event_id": budget_event_id or None,
                "event_type": event_type,
                "source_event_id": source_event_id or None,
            },
            sprint,
        )
        self.audit.stage(stop_request_id, event)
        self._record("budget_hard_stopped", event)

    def resume(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        entry: dict[str, Any],
        request_id: str | None = None,
        delivery_id: str = "",
        through_event: str = "",
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "observer", "steward"}, actor=actor)
        try:
            normalized = SprintResume.from_legacy(entry, required=True, now=_now)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        if normalized is None:
            raise TaskError("validation", "resume entry must be a JSON object", 2)
        if _timestamp(normalized.recorded_at) is None:
            raise TaskError("validation", "resume recorded_at must include a timezone", 2)
        delivery_id = delivery_id.strip()
        through_event = through_event.strip()
        if bool(delivery_id) != bool(through_event):
            raise TaskError(
                "validation",
                "resume delivery acknowledgement requires both delivery_id and through_event",
                2,
            )
        if (delivery_id or through_event) and role != "observer":
            raise TaskError("role_forbidden", "only an observer resume can acknowledge delivery", 3)
        # Guard whole resumes by sprint as acknowledgements move its event cursor.
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(
            role=role,
            actor=actor,
            reference=reference,
            request_id=request_id,
        )

        return self._resume_atomic(
            role=role,
            actor=actor,
            reference=reference,
            normalized=normalized,
            request_id=request_id,
            delivery_id=delivery_id,
            through_event=through_event,
        )

    @_sql_atomic
    def _resume_atomic(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        normalized: SprintResume,
        request_id: str,
        delivery_id: str,
        through_event: str,
    ) -> dict[str, Any]:
        def mutation(sprint: SprintWriteSnapshot) -> None:
            self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={"sprint_resume": json.dumps(normalized.to_document(), separators=(",", ":"))},
            )
            self.client.call(
                "createComment",
                task_id=_sprint_number(sprint),
                user_id=0,
                content="[sprint:resume]\n" + normalized.selected_step,
            )

        payload = {"fields": list(RESUME_FIELDS)}
        if delivery_id:
            payload.update({"delivery_id": delivery_id, "through_event": through_event})
        return self._write("resume_recorded", role, actor, reference, request_id, payload, mutation)

    def close(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        decisions: SprintCloseDecisions | Mapping[str, Any] | None = None,
        request_id: str | None = None,
        reason: str = "",
        closeout: str = "",
    ) -> dict[str, Any]:
        """Close a sprint on explicit typed decisions while preserving the durable JSON contract.

        The PO closes any sprint; the observer closes the one it was launched for, and is refused
        any other by the identity guard, outside the transaction so the refusal stays audited.
        """
        self._role(role, {"po", "observer"}, actor=actor)
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(role=role, actor=actor, reference=reference, request_id=request_id)
        closed = self._close_atomic(
            role=role,
            actor=actor,
            reference=reference,
            decisions=decisions,
            request_id=request_id,
            reason=reason,
            closeout=closeout,
        )
        # After the close committed, once per close record: a repeat of this request id answers the
        # same record and writes nothing new, and a reopened sprint's next close is a new record.
        owner_events.record(
            owner_events.SPRINT_CLOSED,
            reference,
            f"{reference} was closed by the {role}" + (f": {reason.strip()}" if str(reason or "").strip() else ""),
            f"{owner_events.SPRINT_CLOSED}:{reference}:{closed.get('event_id') or request_id}",
            to=self.client,
        )
        return closed

    @serialized
    @_sql_atomic
    def _close_atomic(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        decisions: SprintCloseDecisions | Mapping[str, Any] | None,
        request_id: str,
        reason: str,
        closeout: str,
    ) -> dict[str, Any]:
        try:
            offered = SprintCloseDecisions.from_document(decisions) if decisions is not None else None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        intent = SprintCloseIntent(Role(role), actor, reference)
        intent_document = intent.to_document()
        with sprint_admission_lock(self.data_dir), self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                document, committed = self.transactions.existing(
                    request_id,
                    kind=SPRINT_CLOSED,
                    intent=intent_document,
                )
                if committed is not None:
                    self._check_completed_close(committed, offered, reason=reason, closeout=closeout)
                    return self._close_result(committed)
                if document is not None:
                    self._check_staged_decisions(document, offered)
                    self._check_staged_closeout(document, reason=reason, closeout=closeout)
                if document is None:
                    from ummanu.sprint_close import plan_close_decisions

                    sprint_document = self.reader.show(reference, include_cards=False)
                    sprint = SprintCloseSnapshot.from_document(sprint_document)
                    targets = self._close_targets(sprint)
                    plan = plan_close_decisions(
                        offered,
                        declared_issues=sprint.issues,
                        remaining=targets.remaining,
                        states=targets.remaining_state_map,
                        issue_states=self._declared_issue_states(list(sprint.issues)),
                    )
                    self._check_close_decisions_are_writable(
                        plan, role=role, states=targets.remaining_state_map
                    )
                    closeout_plan = self._plan_closeout(
                        sprint, plan, actor=actor, reason=reason, closeout=closeout
                    )
                    event = self._event(
                        SPRINT_CLOSED,
                        role,
                        actor,
                        reference,
                        request_id,
                        {
                            "intent": intent_document,
                            "reason": str(reason or ""),
                            "closeout": closeout_plan.to_document() if closeout_plan else None,
                            "targets": targets.to_document(),
                            "archived_tasks": [],
                            "remaining_tasks": list(targets.remaining),
                            "decisions": plan.to_document(),
                            "closed_issues": [],
                            "moved_tasks": [],
                            "disposed_tasks": [],
                            "conflicts": [],
                        },
                        sprint_document,
                    )
                    document, committed = self.transactions.begin(
                        request_id,
                        kind=SPRINT_CLOSED,
                        intent=intent_document,
                        event=event,
                    )
                    if committed is not None:
                        return self._close_result(committed)
                    if document is None:
                        raise TaskError("audit_pending", "sprint close transaction claim is unavailable", 4)
                close_payload = document["event"].get("payload", {})
                close_plan = SprintCloseDecisions.from_document(close_payload.get("decisions"))
                closeout_plan = SprintCloseoutPlan.from_document(close_payload.get("closeout"))
                self.client.sprints.save_close(
                    reference,
                    request_id,
                    close_plan.to_document(),
                    reason=str(close_payload.get("reason") or ""),
                    closeout_document=closeout_plan.document if closeout_plan else None,
                )
                return self._run_close(document)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _issue_store(self) -> Any:
        from ummanu.product_issues import ProductIssueStore

        return ProductIssueStore(self.client, data_dir=self.data_dir, instance=self.instance)

    def _declared_issue_states(self, declared: list[str]) -> dict[str, dict[str, Any]]:
        """What every declared issue actually is, for the decisions to be matched against.

        An installation without its instance directory cannot read Product/Issue records at all; a close
        that needs to write one is refused for that separately.
        """
        if self.instance is None:
            return {}
        store = self._issue_store()
        states: dict[str, dict[str, Any]] = {}
        for reference in declared:
            try:
                issue = store.show_issue(reference)
            except TaskError:
                continue
            states[reference] = {
                "closed": bool(issue.get("closed")),
                "close_reason": str(issue.get("close_reason") or ""),
            }
        return states

    def _close_step_status(self, request_id: str) -> str:
        """Whether one step of a close is done, half-written, or still to do.

        The only proof that a step happened is its own derived request id carrying a committed event. A
        pending event under that id is a step that reached the backend but not the journal and is
        finished by driving the same id again — never by looking at the object and finding it already
        in the shape the step wanted, which is somebody else's change as easily as this close's.
        """
        if self.audit.committed_event(request_id) is not None:
            return "done"
        if self.audit.pending_event(request_id) is not None:
            return "pending"
        return "todo"

    def _require_close_step_settled(self, request_id: str) -> None:
        """A step is only over once its own event is committed, whatever the backend shows."""
        if self._close_step_status(request_id) != "done":
            raise TaskError(
                "audit_pending",
                "sprint close step is pending repair; retry with the same request id",
                4,
            )

    def _close_conflict(
        self,
        document: dict[str, Any],
        payload: dict[str, Any],
        *,
        section: str,
        reference: str,
        verdict: str,
        actual: str,
        message: str,
    ) -> None:
        """Stop on somebody else's change, recording a typed recoverable conflict."""
        try:
            conflict = SprintCloseConflict.from_document(
                {"section": section, "ref": reference, "verdict": verdict, "actual": actual}
            )
        except ValueError as exc:
            raise TaskError("audit_pending", str(exc), 4) from None
        conflicts = payload.setdefault("conflicts", [])
        if isinstance(conflicts, list):
            existing = []
            for item in conflicts:
                if isinstance(item, Mapping):
                    try:
                        existing.append(SprintCloseConflict.from_document(item))
                    except ValueError:
                        raise TaskError(
                            "audit_pending", "sprint close transaction has invalid conflicts", 4
                        ) from None
            if not any(item.ref == reference for item in existing):
                conflicts.append(conflict.to_document())
                self.transactions.save(document)
        raise TaskError("close_conflict", message, 3)

    def _close_targets(self, sprint: SprintCloseSnapshot) -> SprintCloseTargets:
        """Freeze this close's task set before any archival write."""
        if not sprint.has_reservations:
            return SprintCloseTargets()
        cards = TaskReader(self.client).list(sprint=sprint.ref)
        return SprintCloseTargets.from_cards(cards)

    def _check_staged_decisions(
        self,
        document: dict[str, Any],
        decisions: SprintCloseDecisions | None,
    ) -> None:
        """A retry may repeat the staged plan, or amend exactly a recorded conflict."""
        if decisions is None:
            return
        payload = (document.get("event") or {}).get("payload") or {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError:
            return
        offered = SprintCloseDecisions(
            issues=tuple(sorted(decisions.issues, key=lambda entry: entry.ref)),
            cards=tuple(sorted(decisions.cards, key=lambda entry: entry.ref)),
        )
        current = SprintCloseDecisions(
            issues=tuple(sorted(staged.issues, key=lambda entry: entry.ref)),
            cards=tuple(sorted(staged.cards, key=lambda entry: entry.ref)),
        )
        if offered == current:
            return
        from ummanu.sprint_close import ALREADY_CLOSED, ALREADY_MOVED

        confirmation = {"issues": ALREADY_CLOSED, "cards": ALREADY_MOVED}
        conflicts: dict[str, SprintCloseConflict] = {}
        for item in payload.get("conflicts") or []:
            if not isinstance(item, Mapping):
                continue
            try:
                conflict = SprintCloseConflict.from_document(item)
            except ValueError:
                continue
            conflicts[conflict.ref] = conflict
        amended: list[SprintCloseConflict] = []
        for section, current_entries, offered_entries in (
            ("issues", current.issues, offered.issues),
            ("cards", current.cards, offered.cards),
        ):
            if len(offered_entries) != len(current_entries):
                self._refuse_restated_decisions()
            for was, now in zip(current_entries, offered_entries):
                if was == now:
                    continue
                conflict = conflicts.get(now.ref)
                if (
                    conflict is None
                    or was.ref != now.ref
                    or conflict.section != section
                    or now.verdict != confirmation[section]
                    or now.actual != conflict.actual
                ):
                    self._refuse_restated_decisions()
                amended.append(conflict)
        if not amended:
            self._refuse_restated_decisions()
        payload["decisions"] = offered.to_document()
        payload["conflicts"] = [
            item
            for item in (payload.get("conflicts") or [])
            if not (
                isinstance(item, Mapping)
                and any(
                    item.get("ref") == conflict.ref and item.get("section") == conflict.section
                    for conflict in amended
                )
            )
        ]
        self.transactions.save(document)

    def _check_completed_close(
        self,
        committed: dict[str, Any],
        decisions: SprintCloseDecisions | None,
        *,
        reason: str,
        closeout: str,
    ) -> None:
        """A repeat of a finished close carries the same canonical typed plan, or is refused."""
        payload = committed.get("payload") if isinstance(committed.get("payload"), dict) else {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", "committed sprint close has invalid decisions", 4) from exc
        if decisions is not None:
            offered = SprintCloseDecisions(
                issues=tuple(sorted(decisions.issues, key=lambda entry: entry.ref)),
                cards=tuple(sorted(decisions.cards, key=lambda entry: entry.ref)),
            )
            current = SprintCloseDecisions(
                issues=tuple(sorted(staged.issues, key=lambda entry: entry.ref)),
                cards=tuple(sorted(staged.cards, key=lambda entry: entry.ref)),
            )
            if offered != current:
                self._refuse_restated_decisions()
        self._check_staged_closeout({"event": committed}, reason=reason, closeout=closeout)

    def _refuse_restated_decisions(self) -> None:
        raise TaskError(
            "validation",
            "this request id was staged with other decisions; retry it with the same file",
            2,
        )

    def _plan_closeout(
        self,
        sprint: SprintCloseSnapshot,
        plan: SprintCloseDecisions,
        *,
        actor: str,
        reason: str,
        closeout: str,
    ) -> SprintCloseoutPlan | None:
        """Freeze the closeout this close will write, before the transaction opens."""
        if not str(closeout or "").strip():
            return None
        from ummanu.sprint_close import closeout_document, closeout_path

        document = closeout_path(sprint.ref, day=_now()[:10])
        text = closeout_document(
            reference=sprint.ref,
            goal=sprint.goal,
            actor=actor,
            reason=str(reason or ""),
            body=str(closeout),
            decisions=plan,
        )
        self._check_closeout_is_writable(document, text, actor=actor)
        return SprintCloseoutPlan(
            document=document,
            text=text,
            body_sha256=_digest(str(closeout)),
        )

    def _check_closeout_is_writable(self, document: str, text: str, *, actor: str) -> None:
        """Refuse a closeout this installation cannot write, before anything is written.

        Asked here it names what is missing and leaves the sprint open; asked in the terminal phase,
        the same refusal would be a transaction to repair. The rules are the knowledge writer's own
        -- `check_knowledge_document` is its preflight, not a second copy of it.
        """
        from ummanu.knowledge_write import KnowledgeError, check_knowledge_document
        from ummanu.state_repo import StateRepoError

        if self.instance is None:
            raise TaskError(
                "validation",
                "writing the sprint's closeout needs the instance directory; pass --instance",
                2,
            )
        try:
            check_knowledge_document(self._instance_dir(), document=document, actor=actor, text=text)
        except (KnowledgeError, StateRepoError) as exc:
            raise TaskError("validation", f"sprint close cannot write its closeout: {exc}", 2) from None

    def _instance_dir(self) -> Path:
        """The installation directory the closeout is written into, however the caller named it."""
        instance = Path(str(self.instance))
        return instance.parent if instance.is_file() else instance

    def _check_staged_closeout(self, document: dict[str, Any], *, reason: str, closeout: str) -> None:
        """A retry of a staged close carries the same closeout body and reason."""
        payload = (document.get("event") or {}).get("payload") or {}
        if "closeout" not in payload and "reason" not in payload:
            return
        staged = SprintCloseoutPlan.from_document(payload.get("closeout"))
        body = str(closeout or "").strip()
        if str(reason or "") and str(reason) != str(payload.get("reason") or ""):
            self._refuse_restated_closeout()
        if not body:
            return
        if staged is None or staged.body_sha256 != _digest(str(closeout)):
            self._refuse_restated_closeout()

    def _refuse_restated_closeout(self) -> None:
        raise TaskError(
            "validation",
            "this request id was staged with another closeout or reason; retry it with the same one",
            2,
        )

    def _check_close_decisions_are_writable(
        self, plan: SprintCloseDecisions, *, role: str, states: Mapping[str, str]
    ) -> None:
        """Refuse a plan this installation cannot perform, before the transaction opens.

        Every disposition move the plan would make is asked of `card_transition` for the closing
        role, the table the dispose step's own move is checked against, so a close whose role may not
        make one of its moves is refused whole, with nothing staged, instead of failing half-written
        (`close_plan_forbidden`). A PO close moves under the sprint override and its every edge is
        allowed; an observer's cannot take a card out of Assessment, which it decides instead.
        """
        from ummanu.sprint_close import ALREADY_CLOSED, ALREADY_MOVED, DISPOSITION_TARGETS, KEEP_OPEN

        closing = [entry for entry in plan.issues if entry.verdict not in {KEEP_OPEN, ALREADY_CLOSED}]
        if closing and self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        if not plan.cards:
            return
        forbidden = []
        for entry in plan.cards:
            target = "" if entry.verdict == ALREADY_MOVED else DISPOSITION_TARGETS[entry.verdict]
            source = str(states.get(entry.ref) or "")
            if not target or source == target:
                continue
            try:
                card_transition(role, source, target)
            except (CardTransitionForbidden, ValueError):
                forbidden.append(f"{entry.ref} (in {source or 'an unknown column'}, {entry.verdict})")
        if forbidden:
            raise TaskError(
                "close_plan_forbidden",
                f"a {role} close cannot move " + ", ".join(forbidden)
                + "; decide it with `task decide` (or move it) before closing",
                3,
            )
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        live = []
        for entry in plan.cards:
            try:
                writer._check_dispatcher_archivable(entry.ref)
            except TaskError as exc:
                if exc.code != "live_work":
                    raise
                live.append(entry.ref)
        if live:
            raise TaskError(
                "live_work",
                "sprint close cannot dispose of card(s) whose dispatcher work is still live; "
                "settle them first: " + ", ".join(live),
                3,
            )

    def _run_close(self, document: dict[str, Any]) -> dict[str, Any]:
        event = document.get("event")
        if not isinstance(event, dict):
            raise TaskError("audit_pending", "sprint close transaction has no audit event", 4)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise TaskError("audit_pending", "sprint close transaction has no payload", 4)
        try:
            targets = SprintCloseTargets.from_document(payload.get("targets"))
            decisions = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", f"sprint close transaction is invalid: {exc}", 4) from None
        archive = list(targets.archive)
        archived = payload.setdefault("archived_tasks", [])
        if not isinstance(archived, list) or not all(isinstance(ref, str) for ref in archived):
            raise TaskError("audit_pending", "sprint close transaction has invalid archival progress", 4)
        try:
            self._close_declared_issues(document, event, payload, decisions)
            writer = TaskWriter(self.client, data_dir=self.data_dir) if archive else None
            for task_ref in archive:
                step_request_id = _close_archive_request_id(str(document["request_id"]), task_ref)
                if self._close_step_status(step_request_id) != "done":
                    assert writer is not None
                    writer.archive(
                        role=str(document["intent"]["role"]),
                        actor=str(document["intent"]["actor"]),
                        reference=task_ref,
                        reason=f"archived when sprint {event['ref']} closed",
                        request_id=step_request_id,
                        sprint_close=str(event["ref"]),
                    )
                    self._require_close_step_settled(step_request_id)
                if task_ref not in archived:
                    archived.append(task_ref)
                    self.transactions.save(document)
            self._dispose_remaining_cards(document, event, payload, decisions, targets)
            payload["observer_cleanup"] = CleanupJournal(self.data_dir).observer_handoff(
                self.reader.show(str(event["ref"]), include_cards=False))
            self.transactions.save(document)
            self._write_closeout(document, event, payload)
            sprint = self.reader.show(str(event["ref"]), include_cards=False)
            typed_request_id = str(document["request_id"]) + ":typed-close"
            typed_pending = self.audit.pending_event(typed_request_id)
            if sprint["status"] != "closed" or (
                typed_pending is not None and typed_pending.get("record_type") == "board.protocol_event"
            ):
                document.setdefault("progress", {})["status_started"] = True
                self.transactions.save(document)
                self._transition_host(
                    role=str(document["intent"]["role"]),
                    actor=str(document["intent"]["actor"]),
                    reference=str(event["ref"]),
                    target="closed",
                    reason="Sprint closed",
                    request_id=typed_request_id,
                )
            document.setdefault("progress", {})["status_done"] = True
            self.transactions.save(document)
            self.transactions.complete(document)
        except TaskError:
            raise
        except (OSError, KeyError, TypeError, ValueError):
            raise TaskError(
                "audit_pending", "sprint close is pending repair; retry with the same request id", 4
            ) from None
        update_active_sprint_projects(self.data_dir, self.reader.show(str(event["ref"]), include_cards=False))
        return self._close_result(event)

    def _close_declared_issues(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: SprintCloseDecisions,
    ) -> None:
        """Perform the typed closing verdicts, one issue at a time."""
        from ummanu.sprint_close import ALREADY_CLOSED, KEEP_OPEN

        closed = payload.setdefault("closed_issues", [])
        if not isinstance(closed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid issue progress", 4)
        pending = [
            entry
            for entry in decisions.issues
            if entry.verdict not in {KEEP_OPEN, ALREADY_CLOSED} and entry.ref not in closed
        ]
        if not pending:
            return
        if self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        store = self._issue_store()
        document.setdefault("progress", {})["issues_started"] = True
        self.transactions.save(document)
        for entry in pending:
            reference = entry.ref
            step_request_id = _close_step_request_id(str(document["request_id"]), "issue", reference)
            status = self._close_step_status(step_request_id)
            if status != "done":
                current = store.show_issue(reference)
                if status == "todo" and current.get("closed"):
                    carried = str(current.get("close_reason") or "unknown")
                    self._close_conflict(
                        document,
                        payload,
                        section="issues",
                        reference=reference,
                        verdict=entry.verdict,
                        actual=carried,
                        message=(
                            f"issue {reference} was closed as {carried} by somebody else, and this "
                            f"close states {entry.verdict}; retry with that decision amended to "
                            f"already_closed naming {carried}"
                        ),
                    )
                store.close_issue(
                    reference=reference,
                    reason=entry.verdict,
                    actor=str(document["intent"]["actor"]),
                    request_id=step_request_id,
                    role=str(document["intent"]["role"]),
                    sprint_close=str(event["ref"]),
                )
                self._require_close_step_settled(step_request_id)
            closed.append(reference)
            self.transactions.save(document)

    def _dispose_remaining_cards(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: SprintCloseDecisions,
        targets: SprintCloseTargets,
    ) -> None:
        """Take every remaining card into the recorded end its typed disposition names."""
        from ummanu.sprint_close import ALREADY_MOVED, DISPOSITION_TARGETS

        if not decisions.cards:
            return
        moved = payload.setdefault("moved_tasks", [])
        disposed = payload.setdefault("disposed_tasks", [])
        if not isinstance(moved, list) or not isinstance(disposed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid disposition progress", 4)
        planned = targets.remaining_state_map
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        reader = TaskReader(self.client)
        # Every step of a close is written in the closing caller's own name. A PO close disposes a
        # card through the sprint override; an observer's close needs none, the card is its sprint's.
        role = str(document["intent"]["role"])
        actor = str(document["intent"]["actor"])
        for entry in decisions.cards:
            reference = entry.ref
            verdict = entry.verdict
            reason = entry.reason
            target = "" if verdict == ALREADY_MOVED else DISPOSITION_TARGETS[verdict]
            if target and str(planned.get(reference) or "") != target:
                move_request_id = _close_step_request_id(
                    str(document["request_id"]), "dispose-move", reference
                )
                status = self._close_step_status(move_request_id)
                if status != "done":
                    if status == "todo" and reader.show(reference)["state"] == target:
                        self._close_conflict(
                            document,
                            payload,
                            section="cards",
                            reference=reference,
                            verdict=verdict,
                            actual=target,
                            message=(
                                f"card {reference} was moved to {target} by somebody else, not by "
                                f"this close; retry with that disposition amended to already_moved "
                                f"naming {target}"
                            ),
                        )
                    writer.move(
                        role=role,
                        actor=actor,
                        reference=reference,
                        target=target,
                        reason=f"{verdict} when sprint {event['ref']} closed: {reason}",
                        sprint_override=role == "po",
                        sprint_override_reason=(
                            f"disposed by the close of {event['ref']}: {reason}" if role == "po" else ""
                        ),
                        request_id=move_request_id,
                    )
                    self._require_close_step_settled(move_request_id)
            if reference not in moved:
                moved.append(reference)
                self.transactions.save(document)
            archive_request_id = _close_step_request_id(
                str(document["request_id"]), "dispose-archive", reference
            )
            if self._close_step_status(archive_request_id) != "done":
                writer.archive(
                    role=role,
                    actor=actor,
                    reference=reference,
                    reason=f"archived when sprint {event['ref']} closed: {reason}",
                    request_id=archive_request_id,
                    sprint_close=str(event["ref"]),
                )
                self._require_close_step_settled(archive_request_id)
            if reference not in disposed:
                disposed.append(reference)
                self.transactions.save(document)

    def _write_closeout(
        self, document: dict[str, Any], event: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Write this close's typed knowledge-closeout plan exactly once."""
        plan = SprintCloseoutPlan.from_document(payload.get("closeout"))
        if plan is None:
            return
        reference = str(event["ref"])
        step_request_id = _close_step_request_id(str(document["request_id"]), "closeout", reference)
        if self._close_step_status(step_request_id) == "done":
            return
        from ummanu.knowledge_write import KnowledgeError, write_knowledge_document

        actor = str(document["intent"]["actor"])
        self._check_closeout_is_writable(plan.document, plan.text, actor=actor)
        document.setdefault("progress", {})["closeout_started"] = True
        self.transactions.save(document)
        step = self._event(
            SPRINT_CLOSEOUT,
            str(document["intent"]["role"]),
            actor,
            reference,
            step_request_id,
            {"close_request_id": str(document["request_id"]), "document": plan.document, "commit": ""},
            self.reader.show(reference, include_cards=False),
        )
        self.audit.stage(step_request_id, step)
        try:
            written = write_knowledge_document(
                self._instance_dir(), document=plan.document, actor=actor, text=plan.text
            )
        except KnowledgeError as exc:
            raise TaskError("backend_error", f"sprint close could not write its closeout: {exc}", 1) from None
        # The knowledge writer's content revision of the closeout, where a Git commit id used to be.
        step["payload"]["commit"] = written.commit
        step["payload"]["changed"] = bool(written.changed)
        self.audit.stage(step_request_id, step)
        self.audit.append(step_request_id, step)
        self._require_close_step_settled(step_request_id)
        payload["closeout"] = plan.mark_written(written.commit).to_document()
        self.transactions.save(document)

    def _close_result(self, event: dict[str, Any]) -> dict[str, Any]:
        from ummanu.sprint_close import CLOSE_NOT_DONE

        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        try:
            decisions = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", "sprint close has invalid decisions", 4) from exc
        closeout_plan = SprintCloseoutPlan.from_document(payload.get("closeout"))
        return {
            "action": SPRINT_CLOSED,
            "sprint": self.reader.show(str(event["ref"])),
            "event_id": str(event["event_id"]),
            "archived_tasks": list(payload.get("archived_tasks") or []),
            "remaining_tasks": list(payload.get("remaining_tasks") or []),
            "issue_decisions": [entry.to_document() for entry in decisions.issues],
            "closed_issues": list(payload.get("closed_issues") or []),
            "card_dispositions": [entry.to_document() for entry in decisions.cards],
            "disposed_tasks": list(payload.get("disposed_tasks") or []),
            "reason": str(payload.get("reason") or ""),
            "closeout": closeout_plan.to_result() if closeout_plan else None,
            "definition_of_done": {"satisfied": False, "reason": CLOSE_NOT_DONE},
            "cleanup": CleanupJournal(self.data_dir).summary(sprint=str(event["ref"])),
        }

    @_sql_atomic
    def reopen(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reopen a sprint that still satisfies every rule for an open sprint.

        The other transition into `open`, so it runs the same admission order as `create` on the same
        staged-intent primitive. The observer is decided again here and is never inherited: what the row
        carries is either the value of the run that closed or migration provenance, and neither is a
        decision about the run being opened now. It is written while the sprint is still closed, so the
        row is never readable open under a value the reopening caller did not choose.
        """
        self._role(role, {"po"}, actor=actor)
        request_id = request_id or str(uuid.uuid4())
        intent = SprintReopenIntent(
            role=Role(role),
            actor=actor,
            reference=reference,
            observer=self._observer_intent(observer, executable=True),
        )
        intent_document = intent.to_document()
        with sprint_admission_lock(self.data_dir):
            document, committed = self.transactions.existing(
                request_id, kind=SPRINT_REOPENED, intent=intent_document
            )
            if committed is not None:
                return self._committed_result(SPRINT_REOPENED, committed)
            if document is None:
                sprint_document = self.reader.show(reference, include_cards=False)
                sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
                self._check_reopen(sprint, reference)
                event = self._event(
                    SPRINT_REOPENED,
                    role,
                    actor,
                    reference,
                    request_id,
                    {"intent": intent_document},
                    sprint,
                )
                document, committed = self.transactions.begin(
                    request_id, kind=SPRINT_REOPENED, intent=intent_document, event=event
                )
                if committed is not None:
                    return self._committed_result(SPRINT_REOPENED, committed)
                if document is None:
                    raise TaskError("audit_pending", "sprint transaction claim is unavailable", 4)
            return self._run_reopen(document)

    def _check_reopen(self, sprint: SprintWriteSnapshot, reference: str) -> None:
        """Every rule an open sprint has to satisfy, read live before any write."""
        missing = [
            name
            for name, value in (
                ("product", sprint.product),
                ("issues", sprint.issues),
                ("reservations", sprint.reservations),
            )
            if not value
        ]
        if missing:
            raise TaskError(
                "validation",
                f"sprint {reference} predates sprint ownership and has no "
                + ", ".join(missing)
                + "; open a new sprint that owns its issues instead of reopening it",
                2,
            )
        self._check_ownership(sprint.product, list(sprint.issues), list(sprint.reservations))
        self._check_conflicts(sprint.admission(), excluding=reference)

    def _run_reopen(self, document: dict[str, Any]) -> dict[str, Any]:
        """Drive the staged reopen to its single audit event, or leave it repairable."""
        intent = SprintReopenIntent.from_document(document["intent"])
        reference = intent.reference
        try:
            sprint_document = self.reader.show(reference, include_cards=False)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            if not (document.get("progress") or {}).get("opened_done"):
                # A staged reopen held nothing while it waited for its repeat, so the
                # installation is measured again before this sprint becomes the open one.
                self._check_conflicts(sprint.admission(), excluding=reference)
            # The value the row carries now, recorded durably before the write that replaces it.
            self._record_observer_preimage(document, sprint)
            document.setdefault("progress", {})["observer_started"] = True
            self.transactions.save(document)
            if intent.observer is None:
                raise TaskError("validation", "sprint reopen intent lacks its observer", 2)
            self._transition_host(
                role=intent.role.value,
                actor=intent.actor,
                reference=reference,
                target="open",
                reason="Sprint reopened",
                request_id=str(document["request_id"]) + ":typed-reopen",
                observer=encode_observer(intent.observer),
            )
            document.setdefault("progress", {})["observer_done"] = True
            document.setdefault("progress", {})["opened_done"] = True
            self.transactions.save(document)
            sprint_document = self.reader.show(reference)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            event = document["event"]
            event["task_id"] = sprint.entity_id
            event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint.admission())
            return SprintMutationReceipt(SPRINT_REOPENED, str(event["event_id"])).to_document(sprint_document)
        except TaskError:
            raise
        except (OSError, KeyError, TypeError, ValueError):
            raise TaskError(
                "audit_pending",
                "sprint reopen is pending repair; retry with the same request id",
                4,
            ) from None

    def _record_observer_preimage(self, document: dict[str, Any], sprint: SprintWriteSnapshot) -> None:
        """Record what the row's observer was, once, before this reopen writes over it."""
        progress = document.setdefault("progress", {})
        if "observer_preimage" in progress:
            return
        try:
            progress["observer_preimage"] = encode_observer(sprint.observer) if sprint.observer else None
        except ValueError:
            progress["observer_preimage"] = None
        self.transactions.save(document)

    @_sql_atomic
    def restore(
        self, *, reference: str, values: dict[str, str], request_id: str | None = None
    ) -> dict[str, Any]:
        """Rewrite one sprint entity's fields verbatim from a checkpoint export.

        Not a sprint mutation an operator makes, so it is not refused on status the way `comment` or
        `resume` are.
        """
        unknown = sorted(set(values) - SPRINT_METADATA)
        if unknown:
            raise TaskError("validation", "restore carries unknown sprint fields: " + ", ".join(unknown), 2)

        def mutation(sprint: SprintWriteSnapshot) -> None:
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=dict(values))

        return self._write(
            "restored", "steward", "restore", reference, request_id, {"fields": sorted(values)}, mutation
        )

    def _guard_observer_identity(self, *, role: str, actor: str, reference: str, request_id: str) -> None:
        """Refuse a sprint write of role `observer` that is not about the caller's own sprint.

        The card guard's counterpart on the entity side, with the same two codes and the same
        fail-closed rule: a head that names no sprint cannot be authenticated at all. The refusal is
        audited under its own request id so it neither consumes the operation's retry key nor is
        recorded twice on a retry.
        """
        if role != "observer":
            return
        from ummanu.runtime.role_env import declared_observer_sprint
        from ummanu.tasks import _sprint_guard_denial_request_id

        declared = declared_observer_sprint()
        if declared and declared == reference:
            return
        code, message = (
            (
                "observer_identity_unbound",
                (
                    "this observer names no sprint, so its writes cannot be authenticated; "
                    "it has to be launched by the dispatcher for one sprint"
                ),
            )
            if not declared
            else (
                "observer_sprint_mismatch",
                f"this observer belongs to sprint {declared}, not to {reference}",
            )
        )
        denial_request_id = _sprint_guard_denial_request_id(request_id)
        event = self.audit.committed_event(denial_request_id)
        if event is None:
            event = self._event(
                "sprint_guard_denied",
                role,
                actor,
                reference,
                denial_request_id,
                {
                    "code": code,
                    "message": message,
                    "project": "",
                    "sprint": declared,
                    "operation_request_id": request_id,
                },
            )
            event["outcome"] = "denied"
            event["backend"]["revision"] = "not_written"
            self.audit.stage(denial_request_id, event)
            try:
                self.audit.append(denial_request_id, event)
            except OSError:
                raise TaskError(
                    "audit_pending",
                    "sprint write was denied but audit repair is required",
                    4,
                ) from None
        payload = event.get("payload") if isinstance(event, dict) else {}
        raise TaskError(str(payload.get("code") or code), str(payload.get("message") or message), 3)

    def _write(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None,
        payload: dict[str, Any],
        mutation: Callable[[SprintWriteSnapshot], Any],
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            return self._committed(kind, committed)
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            if pending.get("record_type") == "board.protocol_event":
                raise TaskError(
                    "validation",
                    "request id belongs to a typed Sprint lifecycle occurrence; retry its protocol recovery",
                    2,
                )
            return self._pending(kind, pending)
        sprint_document = self.reader.show(reference)
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        if sprint.state in {SprintState.CLOSED, SprintState.STOPPED} and kind in {
            "current_task_set",
            "e2e_budget_raised",
            "production_allowed",
            "owner_decisions_recorded",
            "resume_recorded",
        }:
            raise TaskError("closed", "sprint is closed", 3)
        event = self._event(kind, role, actor, reference, request_id, payload, sprint)
        self.audit.stage(request_id, event)
        mutation(sprint)
        return self._record(kind, event)

    def _record(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        sprint_document = self.reader.show(str(event["ref"]))
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        event["task_id"] = sprint.entity_id
        event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
        request_id = str(event["request_id"])
        self.audit.stage(request_id, event)
        event_id = self.audit.append(request_id, event)
        update_active_sprint_projects(self.data_dir, sprint.admission())
        return SprintMutationReceipt(kind, event_id).to_document(sprint_document)

    def _committed(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        sprint_document = self.reader.show(str(event["ref"]))
        event_id = self.audit.append(str(event["request_id"]), event)
        return SprintMutationReceipt(kind, event_id).to_document(sprint_document)

    def _pending(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        # The staged event is only retained after a successful backend mutation in the
        # simple writes. Creation stages its board id before assigning metadata.
        sprint_document = self.reader.show(str(event["ref"]))
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        event["task_id"] = sprint.entity_id
        event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
        self.audit.stage(str(event["request_id"]), event)
        event_id = self.audit.append(str(event["request_id"]), event)
        update_active_sprint_projects(self.data_dir, sprint.admission())
        return SprintMutationReceipt(kind, event_id).to_document(sprint_document)

    def _event(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        payload: dict[str, Any],
        sprint: SprintWriteSnapshot | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = (
            sprint.entity_id if isinstance(sprint, SprintWriteSnapshot) else sprint["id"] if sprint else ""
        )
        return {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": kind,
            "outcome": "success",
            "task_id": task_id,
            "ref": reference,
            "backend": {
                "kind": BOARD_STORE_KIND,
                "task_id": _sprint_number(sprint) if sprint else None,
                "revision": "pending",
            },
            "request_id": request_id,
            "payload": payload,
        }

    @staticmethod
    def _role(role: str, allowed: set[str], *, actor: str) -> None:
        """`admit_role`, the one role check every task, sprint and issue write makes."""
        admit_role(role, actor, allowed)


def _sprint_number(sprint: SprintWriteSnapshot | dict[str, Any] | None) -> int:
    """The sprint's number, read through the same parser a card's number is read through."""
    entity = sprint.entity_id if isinstance(sprint, SprintWriteSnapshot) else (sprint or {}).get("id")
    number = entity_number("sprint", entity)
    if number is None:
        raise TaskError("backend_error", "board store returned an invalid sprint", 1)
    return number


def _is_sprint_row(raw: Any) -> bool:
    """A row on the sprint board becomes a sprint when it carries a sprint reference.

    A create writes the reference after the fields the sprint was admitted with, so a row still
    without one is an unfinished create its own staged transaction repairs.
    """
    return isinstance(raw, dict) and str(raw.get("reference") or "").startswith(SPRINT_REFERENCE_PREFIX)


def _create_marker(request_id: str) -> str:
    return "[ummanu-sprint-transaction:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest() + "]"


def _close_archive_request_id(request_id: str, reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"{request_id}:sprint-close-archive:{digest}"


def _closeout_result(plan: Any) -> dict[str, Any] | None:
    """Released compatibility helper backed by the typed closeout boundary."""
    typed = SprintCloseoutPlan.from_document(plan)
    return typed.to_result() if typed is not None else None


def _close_step_request_id(request_id: str, step: str, reference: str) -> str:
    """One derived id per step of a close, so a retry replays it instead of repeating it."""
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"{request_id}:sprint-close-{step}:{digest}"


def canonical_repository_roots(paths: list[Any]) -> list[str]:
    """The absolute roots a declaration means, resolved where the caller declared them.

    A relative root only names a tree next to the process that wrote it, so it is resolved once,
    here, at declaration time. Resolving it later would answer against whichever process runs the
    check, and two sprints sharing a tree would read as disjoint. A root this host cannot resolve
    at all is refused rather than guessed at.
    """
    roots: list[str] = []
    for raw in paths:
        text = str(raw).strip()
        if not text:
            continue
        try:
            root = str(Path(text).expanduser().resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise TaskError(
                "validation",
                f"repository root {text!r} cannot be resolved on this host ({exc}); "
                "declare a repository root this host can name",
                2,
            ) from None
        if root not in roots:
            roots.append(root)
    return roots


def _scanned_roots(paths: list[Any], *, refusal: Callable[[str, str], TaskError]) -> list[Path]:
    """The stored roots of one sprint being scanned, or a refusal naming the bad value.

    Fails closed on anything that is not already absolute: such a value is canonical to no host,
    and resolving it here is exactly how two sprints sharing a working tree read as disjoint.
    """
    roots: list[Path] = []
    for raw in paths:
        text = str(raw).strip()
        if not text:
            continue
        root = Path(text)
        if not root.is_absolute():
            raise refusal(text, "is not an absolute path")
        try:
            root = root.resolve()
        except (OSError, RuntimeError, ValueError):
            raise refusal(text, "cannot be resolved on this host") from None
        if root not in roots:
            roots.append(root)
    return roots


def _roots_overlap(left: Path, right: Path) -> bool:
    """Whether two canonical roots name one tree, which includes one nested in the other."""
    return left == right or left in right.parents or right in left.parents


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _resume_lag_seconds(recorded_at: str, last_event_at: str) -> int | None:
    """Seconds a resume trails its latest linked-card event, or None for bad timestamps."""
    if not recorded_at or not last_event_at:
        return 0
    recorded = _timestamp(recorded_at)
    event = _timestamp(last_event_at)
    if recorded is None or event is None:
        return None
    return max(0, int((event - recorded).total_seconds()))


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _ownership(meta: dict[str, str]) -> dict[str, Any]:
    """The sprint's product, issues and reservations, only where the row has them.

    A sprint created before ownership existed carries none of the three keys. A reader answering
    `""` and `[]` for it would put values on the entity nobody wrote, and the next checkpoint would
    store them as if they were chosen.
    """
    result: dict[str, Any] = {}
    if "sprint_product" in meta:
        result["product"] = meta["sprint_product"]
    if "sprint_issues" in meta:
        result["issues"] = _json_list(meta["sprint_issues"])
    if "sprint_reservations" in meta:
        result["reservations"] = _json_list(meta["sprint_reservations"])
    return result


def _observer(meta: dict[str, str]) -> dict[str, Any]:
    """The sprint's declared observer, only where the row carries the field.

    Three states stay apart, because their repairs differ: the key is missing, the key holds one of
    the four tagged forms, or the key holds something else. The last is reported as `None` rather
    than dropped, so a corrupt row does not pass for one that carries nothing.
    """
    if OBSERVER_FIELD not in meta:
        return {}
    return {"observer": parse_observer(meta[OBSERVER_FIELD])}


def _json_list(value: str | None) -> list[str]:
    """Released private compatibility alias around the typed Sprint read boundary."""
    return sprint_string_list(value)


def _budget(
    value: Any = None,
    thresholds: dict[str, int] | None = None,
    uncharged: Any = None,
) -> dict[str, Any]:
    """Released private compatibility projection of :class:`SprintBudget`."""
    return SprintBudget.from_legacy(value, thresholds=thresholds, uncharged=uncharged).to_document()


def _budget_json(budget: dict[str, Any]) -> str:
    """The `sprint_budget` field's value. The uncharged counts have their own field and stay out."""
    return json.dumps(
        {key: value for key, value in budget.items() if key != "uncharged"}, separators=(",", ":")
    )


def _source_audit(value: Any) -> dict[str, str] | None:
    """Released private compatibility projection of :class:`SprintSourceAudit`."""
    source = SprintSourceAudit.from_legacy(value)
    return source.to_document() if source is not None else None


def _resume(value: Any, *, required: bool = False) -> dict[str, Any] | None:
    try:
        resume = SprintResume.from_legacy(value, required=required, now=_now)
    except ValueError as exc:
        raise TaskError("validation", str(exc), 2) from None
    if resume is None:
        return None
    if required and _timestamp(resume.recorded_at) is None:
        raise TaskError("validation", "resume recorded_at must include a timezone", 2)
    return resume.to_document()
