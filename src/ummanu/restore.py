from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu import _proc, state_repo
from ummanu._fsutil import file_lock, ndjson_lines, write_text_atomic
from ummanu.backup_policy import (
    ARCHIVE_ROOT,
    BACKUP_KINDS,
    CORE_POLICY,
    POSTGRES_BACKUP_VERSION,
    BackupPolicy,
    is_memory_journal_git_runtime_entry,
    is_memory_model_cache_entry,
    policy_for,
    restore_plan_components,
    should_skip_data_entry,
)
from ummanu.backup_verify import _verify_plain_tar
from ummanu.board.backend import CARD, SPRINT, board_client, entity_number
from ummanu.board.extension_bag import EXTENSION_BAG, fold_extension_bags
from ummanu.board.legacy_codec import (
    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,
)
from ummanu.board.legacy_codec import (
    enum_or_default as _enum_or_default,  # noqa: F401 - released private compatibility alias
)
from ummanu.board.legacy_codec import (
    positive_int as _positive_int,
)
from ummanu.board.local_run import LOCAL_RUN_EXCEPTIONS_FIELD, parse_local_run_exceptions
from ummanu.board.owner_decisions import FIELD as OWNER_DECISIONS_FIELD, stored_decisions
from ummanu.board.normalized_checkpoint import NormalizedBoardError, validated_normalized_cards
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.task_routing import TaskMetadata
from ummanu.config import DataDirError, instance_data_dir, validate_instance
from ummanu.data import init_layout
from ummanu.product_issues import (
    registered_projects,
)
from ummanu.runtime.head import CODEX_LAUNCH_MODES
from ummanu.sprint_observer import (
    EXECUTOR_FIELDS,
    ObserverMetadataError,
    check_observer_profile,
    encode_executor,
    encode_observer,
    installed_head_profiles,
    is_executable,
    parse_observer,
)
from ummanu.tasks import (
    TaskError,
    TaskReader,
    TaskWriter,
    all_project_cards,
)

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient


@dataclass(frozen=True)
class RestorePlan:
    archive: Path
    backup_kind: str
    backup_version: int
    data_dir: Path
    components: tuple[dict[str, str], ...]
    instance_identity: dict[str, str]


class RestoreError(RuntimeError):
    pass


RESTORE_STATE_FILE = "restore-state.json"
MEMORY_REINDEX_TIMEOUT_SECONDS = 300
# Every card needs a kind; refuse rows the current board cannot place.
_RECORD_TYPES = {"task", "issue", "product"}
_PRODUCT_ISSUE_METADATA = (
    "record_type",
    "product_id",
    "product_projects",
    "issue_product",
    "issue_kind",
    "issue_priority",
    "issue_closed_reason",
)


def _entity_number(kind: str, identity: object) -> int:
    """A restored row's backend number, read through the one identity parser.

    `board/backend.py` mints `<kind>_<backend>_<n>` and reads it back; restore used to strip one
    literal prefix and hand whatever remained to `int()`, which raised `ValueError` rather than a
    restore refusal on any identity it did not recognise.
    """
    number = entity_number(kind, identity)
    if number is None:
        raise RestoreError(f"restored row carries no usable {kind} identity: {identity!r}")
    return number


def restore_state(data_dir: Path) -> dict[str, Any]:
    """Read the derived restore progress record without treating it as canon."""
    try:
        value = json.loads((data_dir / RESTORE_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def import_normalized_board(
    data_dir: Path, *, client: SqlCardClient | None = None, instance: Path | None = None
) -> int:
    """Populate an empty board, using one outer transaction when the target is PostgreSQL."""
    if client is None:
        if instance is None:
            raise RestoreError("restore requires the target instance to bind its board")
        client = board_client(instance, serves=(CARD, SPRINT))
    if not client._depth:
        with client.transaction():
            return _import_normalized_board(data_dir, client=client, instance=instance)
    return _import_normalized_board(data_dir, client=client, instance=instance)


def _import_normalized_board(
    data_dir: Path, *, client: SqlCardClient, instance: Path | None = None
) -> int:
    """Populate an empty board from the normalized export and prove parity on every retry."""
    from ummanu.sprints import sprint_admission_lock

    data_dir = data_dir.expanduser().resolve()
    # Restoring open sprints is set admission against create and reopen.
    with file_lock(data_dir / "board" / ".restore.lock"), sprint_admission_lock(data_dir):
        try:
            cards = _normalized_cards(
                data_dir, registered_project_ids=(registered_projects(instance) if instance else None)
            )
            sprints = _normalized_sprints(data_dir)
            # Validate both sets before the first backend write.
            _check_sql_sprint_current_tasks(cards, sprints)
            _check_restored_observers(sprints, instance)
            _check_restored_executors(sprints)
            _check_restored_admission(sprints, instance)
            reader = TaskReader(client)
            writer = TaskWriter(client, data_dir=data_dir)
            _, unresolved = writer.reconcile(defer_restore_comments=True, defer_bulk_restore=True)
            if unresolved:
                raise RestoreError("board audit repair is required before restore")
            _set_restore_phase(client, "inventory")
            board_id, columns, swimlanes = reader._board()
            existing = _existing_board_cards(client, board_id)
            unexpected = set(existing) - {card["reference"] for card in cards}
            if unexpected:
                raise RestoreError("board is not empty or does not match normalized restore data")
            # Read once before writes for idempotency and backend-audit binding.
            existing_sprints = _existing_sprints(data_dir, client, sprints)
            prefix = _restore_request_prefix(data_dir, writer.audit, set(existing) | set(existing_sprints))
            _validate_deferred_restore_comments(writer.audit, cards, sprints, prefix)
            # The writes follow `board.import_order.IMPORT_PHASES`, the one statement of their order:
            # history first, so a restored row claiming an exported request finds its `requests`
            # row (`issue_comment_claims_its_request`); the record kinds, comments, closure, order
            # and sprints after it.
            _restore_board_history(data_dir, writer.audit)
            ordered_cards = sorted(cards, key=_restore_card_order)
            columns, swimlanes = _ensure_restore_swimlanes(
                client, board_id, columns, swimlanes, ordered_cards
            )
            from ummanu.task_restore import restore_cards_batched

            restore_cards_batched(
                writer,
                ordered_cards,
                board_id=board_id,
                columns=columns,
                swimlanes=swimlanes,
                existing=existing,
                request_prefix=prefix,
            )
            _set_restore_phase(client, "proof")
            setup = reader.restore_snapshot()
            _require_card_snapshot(data_dir, cards, setup)
            from ummanu.task_restore import commit_restored_cards

            commit_restored_cards(writer, ordered_cards, setup, request_prefix=prefix)
            _set_restore_phase(client, "proof")
            _restore_card_comments_batched(writer, ordered_cards, setup, prefix)
            from ummanu.task_restore import close_restored_cards_batched

            close_restored_cards_batched(client, ordered_cards, setup, board_id=board_id)
            _set_restore_phase(client, "closure")
            post_close = reader.restore_snapshot()
            _require_card_snapshot(data_dir, cards, post_close)
            _set_restore_phase(client, "order")
            _reconcile_restored_order(writer, cards, post_close, prefix)
            _set_restore_phase(client, "final_parity")
            actual = reader.restore_snapshot()
            _require_card_snapshot(data_dir, cards, actual)
            if any(_core_from_live(actual[card["reference"]]) != _core_from_export(card) for card in cards):
                _update_restore_state(data_dir, board="failed", board_parity="failed")
                raise RestoreError("board parity check failed")
            if _restored_order_mismatch(cards, actual):
                _update_restore_state(data_dir, board="failed", board_parity="failed")
                raise RestoreError("board parity check failed: restored card order")
            _import_sprints(data_dir, client, sprints, existing_sprints, prefix)
            pending_comments = [
                event for event in writer.audit.pending_events() if event.get("kind") == "restored_comment"
            ]
            if pending_comments:
                raise RestoreError("board comment audit repair is required before restore can complete")
        except TaskError as exc:
            raise RestoreError(exc.message) from None
        _update_restore_state(
            data_dir,
            board="complete",
            board_parity="complete",
            board_count=len(cards),
            sprints="complete",
            sprint_parity="complete",
            sprint_count=len(sprints),
        )
        return len(cards)


def _restore_board_history(data_dir: Path, audit: Any) -> None:
    """Recreate portable committed request/audit history without replaying effects.

    The import's first write phase (`board.import_order`): these rows reference no board row, and
    an exported issue or product comment claims one of them by its `[request-id:...]` stamp.
    """
    path = data_dir / "board" / "audit.json"
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            events = payload.get("events") if isinstance(payload, dict) else None
        else:
            ndjson = data_dir / "board" / "audit.ndjson"
            if not ndjson.is_file():
                return
            events = [json.loads(line) for line in ndjson_lines(ndjson.read_text(encoding="utf-8")) if line]
    except (OSError, ValueError) as exc:
        raise RestoreError(f"normalized board audit export is invalid: {exc}") from None
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise RestoreError("normalized board audit export has no events list")
    for event in events:
        request_id = event.get("request_id")
        event_id = event.get("event_id")
        if not isinstance(request_id, str) or not request_id or not isinstance(event_id, str) or not event_id:
            raise RestoreError("normalized board audit export contains an invalid event")
        try:
            # History, not new transitions: it owes no origin return (`SqlTaskAudit.append`).
            audit.append(request_id, event, restoring=True)
        except TaskError as exc:
            raise RestoreError(f"normalized board audit restore failed: {exc.message}") from None


def _set_restore_phase(client: Any, phase: str) -> None:
    """Mark a restore boundary when a diagnostic client provides a phase observer."""
    hook = getattr(client, "set_restore_phase", None)
    if callable(hook):
        hook(phase)


def _existing_sprints(
    data_dir: Path, client: SqlCardClient, sprints: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The sprint entities the target already holds, read once before any write."""
    if not sprints:
        return {}
    from ummanu.sprints import SprintReader

    return {sprint["ref"]: sprint for sprint in SprintReader(client, data_dir=data_dir).export()}


def _existing_board_cards(client: SqlCardClient, board_id: int) -> dict[str, dict[str, Any]]:
    """Read both active and closed Pipeline records before deciding a restore is empty."""
    sql_rows = getattr(client, "restore_card_rows", None)
    raw_cards = sql_rows() if callable(sql_rows) else all_project_cards(client, board_id)
    result: dict[str, dict[str, Any]] = {}
    for card in raw_cards:
        if not isinstance(card, dict):
            continue
        reference = card.get("reference")
        if isinstance(reference, str) and reference:
            if reference in result:
                raise RestoreError(f"board contains duplicate reference: {reference}")
            result[reference] = card
    return result


def _ensure_restore_swimlanes(
    client: SqlCardClient,
    board_id: int,
    columns: dict[int, str],
    swimlanes: dict[int, str],
    cards: list[dict[str, Any]],
) -> tuple[dict[int, str], dict[int, str]]:
    """Create all missing exported lanes in one bounded restore setup batch."""
    present = {name.casefold() for name in swimlanes.values()}
    missing = sorted(
        {
            str(card.get("swimlane") or "").strip()
            for card in cards
            if str(card.get("swimlane") or "").strip()
            and str(card.get("swimlane") or "").strip().casefold() not in present
        },
        key=str.casefold,
    )
    if not missing:
        return columns, swimlanes
    try:
        client.call_batch(("addSwimlane", {"project_id": board_id, "name": name}) for name in missing)
    except TaskError as exc:
        from ummanu.task_restore import refused_in_transaction, store_refusal

        if refused_in_transaction(client, exc):
            raise store_refusal("swimlane batch", exc) from None
        # A lost aggregate answer can follow any applied subset.  The fresh map below is the proof.
    raw = client.call("getActiveSwimlanes", project_id=board_id) or []
    refreshed = {
        identifier: str(lane.get("name") or "")
        for lane in raw
        if isinstance(lane, dict) and (identifier := _positive_int(lane.get("id"))) is not None
    }
    absent = [name for name in missing if name.casefold() not in {v.casefold() for v in refreshed.values()}]
    if absent:
        raise RestoreError(f"could not create restored swimlane: {absent[0]}")
    return columns, refreshed


def _restore_card_comments_batched(
    writer: TaskWriter,
    cards: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    prefix: str,
) -> None:
    from ummanu.task_restore import RestoreCommentOccurrence, restore_comments_batched

    intended: list[RestoreCommentOccurrence] = []
    for card in cards:
        reference = card["reference"]
        current = live.get(reference)
        if current is None:
            raise RestoreError(f"restored card disappeared before comment recovery: {reference}")
        task_id = _entity_number("task", current["id"])
        occurrences: dict[str, int] = {}
        for index, body in enumerate(_restore_comments(card)):
            occurrence = occurrences.get(body, 0)
            occurrences[body] = occurrence + 1
            intended.append(
                RestoreCommentOccurrence(
                    reference,
                    task_id,
                    body,
                    occurrence,
                    f"{prefix}comment:{reference}:{index}",
                )
            )
    restore_comments_batched(writer, intended)


def _require_card_snapshot(
    data_dir: Path, cards: list[dict[str, Any]], snapshot: dict[str, dict[str, Any]]
) -> None:
    """Translate a vanished restored reference into the public parity failure."""
    if any(card["reference"] not in snapshot for card in cards):
        _update_restore_state(data_dir, board="failed", board_parity="failed")
        raise RestoreError("board parity check failed: restored card is missing")


def _validate_deferred_restore_comments(
    audit: SqlTaskAudit,
    cards: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    prefix: str,
) -> None:
    """Fail before board writes unless every deferred event belongs to this canon."""
    from ummanu.tasks import _digest

    expected: dict[str, tuple[str, str, int]] = {}
    for subject, bodies, label in (
        *(
            (card["reference"], _restore_comments(card), "comment")
            for card in sorted(cards, key=_restore_card_order)
        ),
        *(
            (
                sprint["reference"],
                [str(entry["text"]) for entry in sprint["comments"]],
                "sprint-comment",
            )
            for sprint in sprints
        ),
    ):
        seen: dict[str, int] = {}
        for index, body in enumerate(bodies):
            occurrence = seen.get(body, 0)
            seen[body] = occurrence + 1
            expected[f"{prefix}{label}:{subject}:{index}"] = (subject, _digest(body), occurrence)
    pending = audit.pending_events()
    if audit.status()["pending"] != len(pending):
        raise RestoreError("board audit repair is required before restore")
    card_refs = {str(card["reference"]) for card in cards}
    for event in pending:
        request_id = str(event.get("request_id") or "")
        if (
            event.get("kind") == "restored_bulk"
            and event.get("ref") in card_refs
            and request_id == f"{prefix}card:{event.get('ref')}"
        ):
            # The card transaction validates the complete content identity and resumes it below.
            continue
        payload = event.get("payload")
        identity = expected.get(request_id)
        if (
            event.get("kind") != "restored_comment"
            or identity is None
            or event.get("ref") != identity[0]
            or not isinstance(payload, dict)
            or payload.get("body_sha256") != identity[1]
            or payload.get("restore_occurrence") != identity[2]
        ):
            raise RestoreError("board audit repair is required before restore")


def _import_sprints(
    data_dir: Path,
    client: SqlCardClient,
    sprints: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    prefix: str,
) -> None:
    """Recreate the sprint entities and prove they match the export."""
    if not sprints:
        # Do not invent an empty sprint board.
        return
    from ummanu.data import normalize_sprint_entity
    from ummanu.sprints import SprintReader, SprintWriter, ensure_sprint_board

    ensure_sprint_board(client)
    reader = SprintReader(client, data_dir=data_dir)
    writer = SprintWriter(client, data_dir=data_dir)
    unexpected = set(existing) - {sprint["reference"] for sprint in sprints}
    if unexpected:
        raise RestoreError("sprint board is not empty or does not match normalized restore data")
    for sprint in sprints:
        reference = sprint["reference"]
        if reference not in existing:
            writer.restore_create(
                goal=sprint["goal"],
                definition_of_done=sprint["definition_of_done"],
                repositories=list(sprint["repositories"]),
                reference=reference,
                request_id=f"{prefix}sprint-create:{reference}",
                # Publish status and observer before a readable reference.
                observer=sprint.get("observer"),
                status=str(sprint["status"]),
            )
        writer.restore(
            reference=reference,
            values=_restore_sprint_metadata(sprint),
            request_id=f"{prefix}sprint:{reference}",
        )
    setup = {entity["ref"]: entity for entity in reader.export()}
    _restore_sprint_comments_batched(writer, sprints, setup, prefix)
    live = {entity["reference"]: entity for entity in map(normalize_sprint_entity, reader.export())}
    if any(_sprint_core(live.get(sprint["reference"], {})) != _sprint_core(sprint) for sprint in sprints):
        _update_restore_state(data_dir, sprints="failed", sprint_parity="failed")
        raise RestoreError("sprint parity check failed")


def _restore_sprint_comments_batched(
    writer: Any,
    sprints: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    prefix: str,
) -> None:
    from ummanu.task_restore import RestoreCommentOccurrence, restore_comments_batched

    intended: list[RestoreCommentOccurrence] = []
    for sprint in sprints:
        reference = sprint["reference"]
        current = live.get(reference)
        if current is None:
            raise RestoreError(f"restored sprint disappeared before comment recovery: {reference}")
        task_id = _entity_number("sprint", current["id"])
        occurrences: dict[str, int] = {}
        for index, entry in enumerate(sprint["comments"]):
            body = str(entry["text"])
            occurrence = occurrences.get(body, 0)
            occurrences[body] = occurrence + 1
            intended.append(
                RestoreCommentOccurrence(
                    reference,
                    task_id,
                    body,
                    occurrence,
                    f"{prefix}sprint-comment:{reference}:{index}",
                    entity="sprint",
                    recorded_at=str(entry.get("ts") or ""),
                )
            )
    restore_comments_batched(writer, intended)


SPRINT_PARITY_FIELDS = (
    "reference",
    "goal",
    "definition_of_done",
    "repositories",
    "product",
    "issues",
    "reservations",
    "status",
    "budget",
    "current_task",
    "resume",
    "audit",
    "observer",
    "worker",
    "reviewer",
    "po_session",
    "allowed_productions",
    "local_run_exceptions",
    "owner_decisions",
    "e2e",
)


def _check_restored_observers(sprints: list[dict[str, Any]], instance: Path | None) -> None:
    """Validate the whole exported observer set before the first backend write of any set.

    A row without a readable value is either a corrupt export or one taken before the observer
    migration, and restoring it either way would publish a row the reader this installation comes
    back with immediately calls corrupt. Nothing is written here: it is the preflight.
    """
    profiles: set[str] = set()
    if any(str(sprint.get("status") or "") == "open" for sprint in sprints):
        # Only open rows need a registered head; closed archives remain restorable.
        try:
            profiles = installed_head_profiles(instance)
        except ObserverMetadataError as exc:
            raise RestoreError(f"sprint observer metadata cannot be validated: {exc.message}") from None
    problems: list[str] = []
    for sprint in sprints:
        reference = str(sprint.get("reference") or "?")
        status = str(sprint.get("status") or "")
        if "observer" not in sprint:
            problems.append(
                f"{reference}: the row carries no observer field. The export is either corrupt or "
                "was taken before the observer migration; both are refused. Add the value to that "
                'row in the export\'s state/board/sprints.json ("observer": {"kind": "head", '
                '"profile": "<profile>"} for a closed row\'s head, or {"kind": "none"}) and run '
                "the restore again"
            )
            continue
        value = parse_observer(sprint.get("observer"))
        if value is None:
            problems.append(f"{reference}: observer value is not one of the tagged forms")
            continue
        if status == "open" and not is_executable(value):
            problems.append(
                f"{reference}: an open sprint may not carry migration provenance ({value.get('source')})"
            )
            continue
        if status == "open":
            # An open row's declared head must still exist.
            try:
                check_observer_profile(value, profiles, subject=reference)
            except ObserverMetadataError as exc:
                problems.append(exc.message)
    if problems:
        raise RestoreError("sprint observer metadata is invalid: " + "; ".join(problems))


def _check_restored_executors(sprints: list[dict[str, Any]]) -> None:
    """Validate the exported executor pins as a set, beside the observers and before any write.

    Absence is legal here and needs no repair: a sprint that pins nobody carries neither key, and
    every export taken before these fields existed is one of those. What is refused is a key that is
    there and is not a profile name, because recovering it as absence would turn a constraint the
    owner set into "the observer chooses", silently and durably.
    """
    from ummanu.sprint_observer import EXECUTOR_PINNED, parse_executor

    problems = [
        f"{sprint.get('reference') or '?'}: {role} pin is not a head profile name"
        for sprint in sprints
        for role in EXECUTOR_FIELDS
        if role in sprint and parse_executor(sprint[role])["state"] != EXECUTOR_PINNED
    ]
    if problems:
        raise RestoreError("sprint executor metadata is invalid: " + "; ".join(problems))


def _check_restored_admission(sprints: list[dict[str, Any]], instance: Path | None) -> None:
    """Refuse an export whose open sprints this installation would never have admitted.

    `restore_create` is deliberately not an admission decision, so the set as a whole is asked once,
    here, before the first backend write: an archive carrying two open sprints that share a product,
    a reservation, a repository tree or an observer head would otherwise be a way to arrive at
    exactly the pair admission exists to refuse. The limit is the target installation's.
    """
    from ummanu.sprints import (
        instance_open_sprint_limit,
        open_sprint_admission_error,
    )

    rows = [
        {
            "ref": str(sprint.get("reference") or ""),
            "product": str(sprint.get("product") or ""),
            "reservations": list(sprint.get("reservations") or []),
            "repositories": list(sprint.get("repositories") or []),
            "observer": parse_observer(sprint.get("observer")),
        }
        for sprint in sprints
        if str(sprint.get("status") or "") == "open"
    ]
    problem = open_sprint_admission_error(rows, limit=instance_open_sprint_limit(instance))
    if problem is not None:
        raise RestoreError(f"restored open sprints are not admissible on this installation: {problem}")


# Preserve absent ownership keys; empty replacement is lossy.
_ABSENT = object()


def _sprint_core(sprint: dict[str, Any]) -> dict[str, Any]:
    """The exported sprint contract, without what a rewrite cannot reproduce."""
    core: dict[str, Any] = {field: sprint.get(field, _ABSENT) for field in SPRINT_PARITY_FIELDS}
    core["local_run_exceptions"] = sprint.get("local_run_exceptions", [])
    core["owner_decisions"] = sprint.get("owner_decisions", [])
    core["e2e"] = sprint.get("e2e", {"budget": 3, "used": 0, "charges": []})
    core["comments"] = [
        str(comment.get("text") or "") for comment in sprint.get("comments", []) if isinstance(comment, dict)
    ]
    return core


def _restore_sprint_metadata(sprint: dict[str, Any]) -> dict[str, str]:
    resume = sprint.get("resume")
    ownership = {
        key: value
        for key, value in (
            ("sprint_product", str(sprint.get("product") or "")),
            ("sprint_issues", json.dumps(list(sprint.get("issues") or []), separators=(",", ":"))),
            (
                "sprint_reservations",
                json.dumps(list(sprint.get("reservations") or []), separators=(",", ":")),
            ),
        )
        # Preserve absent ownership fields rather than invent empty values.
        if value not in {"", "[]"}
    }
    return ownership | {
        "sprint_goal": str(sprint["goal"]),
        "sprint_definition_of_done": str(sprint["definition_of_done"]),
        "sprint_repositories": json.dumps(list(sprint["repositories"]), separators=(",", ":")),
        "sprint_status": str(sprint["status"]),
        "sprint_budget": json.dumps(
            {"by_type": sprint["budget"]["by_type"]}, sort_keys=True, separators=(",", ":")
        ),
        **(
            {
                "sprint_budget_uncharged": json.dumps(
                    sprint["budget"]["uncharged"], sort_keys=True, separators=(",", ":")
                )
            }
            if sprint["budget"].get("uncharged")
            else {}
        ),
        "sprint_current_task": str(sprint["current_task"]),
        "sprint_resume": (json.dumps(resume, sort_keys=True, separators=(",", ":")) if resume else ""),
        "sprint_source_audit": json.dumps(sprint["audit"], sort_keys=True, separators=(",", ":")),
        **({"sprint_observer": encode_observer(sprint["observer"])} if "observer" in sprint else {}),
        # Written back only for a role the record declares, so a restored row carries the pin it
        # was exported with and no field at all where the owner pinned nobody.
        **{
            EXECUTOR_FIELDS[role]: encode_executor(str(sprint[role]))
            for role in EXECUTOR_FIELDS
            if sprint.get(role)
        },
        # Only what the record carries: a sprint exported without them restores without them.
        **({"sprint_po_session": str(sprint["po_session"])} if sprint.get("po_session") else {}),
        **(
            {LOCAL_RUN_EXCEPTIONS_FIELD: json.dumps(sprint["local_run_exceptions"], separators=(",", ":"))}
            if sprint.get("local_run_exceptions") else {}
        ),
        **(
            {
                "sprint_allowed_productions": json.dumps(
                    list(sprint["allowed_productions"]), separators=(",", ":")
                )
            }
            if sprint.get("allowed_productions")
            else {}
        ),
        OWNER_DECISIONS_FIELD: json.dumps(sprint.get("owner_decisions", []), sort_keys=True, separators=(",", ":")),
        # The e2e run budget as exported (secretary-1796); an export without it restores the default.
        **(
            {
                "sprint_e2e_budget": str(int(sprint["e2e"]["budget"])),
                "sprint_e2e_used": str(int(sprint["e2e"]["used"])),
                "sprint_e2e_charges": json.dumps(
                    list(sprint["e2e"].get("charges") or []), sort_keys=True, separators=(",", ":")
                ),
            }
            if isinstance(sprint.get("e2e"), dict)
            else {}
        ),
    }


DEFAULT_MEMORY_MODEL = "intfloat/multilingual-e5-large"
DEFAULT_MEMORY_DIM = 1024


def rebuild_memory_index(
    data_dir: Path,
    instance_dir: Path | None,
    *,
    python: Path | None = None,
    script: Path | None = None,
    model: str | None = None,
    dim: int | None = None,
    threads: int | None = None,
    runner=None,
    isolated: bool = False,
) -> int:
    """Replace the derived index from restored canon.

    `isolated` builds the in-process embedder in a short-lived child process instead (ummanu-53
    P14). The model is resident only while the child runs, and the host gets that memory back when
    the child exits, so a caller that goes on to start `ummanu-memory-mcp` (recover's host step) never
    holds a second copy of the model beside the service's own.
    """
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    facts_dir = _memory_canon_dir(data_dir, instance_dir)
    try:
        if runner is not None:
            result = runner(facts_dir, memory_dir / "export.ndjson", memory_dir / "index.sqlite")
            count = int(result["parity"]["indexed"])
        elif python is not None or script is not None:
            if (
                python is None
                or script is None
                or not isinstance(model, str)
                or not model
                or not isinstance(dim, int)
            ):
                raise RuntimeError("external memory rebuild contract is not configured")
            python = python.expanduser().absolute()
            script = script.expanduser().resolve()
            if not python.is_file() or not os.access(python, os.X_OK) or not script.is_file():
                raise RuntimeError("external memory rebuild argv contract is unavailable")
            completed = _proc.run(
                [
                    str(python),
                    str(script),
                    "--canon",
                    str(facts_dir),
                    "--export",
                    str(memory_dir / "export.ndjson"),
                    "--target-db",
                    str(memory_dir / "index.sqlite"),
                    "--model",
                    model,
                    "--dim",
                    str(dim),
                ],
                timeout=MEMORY_REINDEX_TIMEOUT_SECONDS,
                env={
                    **os.environ,
                    "MEMORY_CACHE_DIR": str(memory_dir / "fastembed-cache"),
                    "MEMORY_THREADS": str(threads or 1),
                },
            )
            if completed.returncode:
                raise RuntimeError("memory reindex command failed: " + _reindex_error_detail(completed))
            result = json.loads(completed.stdout)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError("memory reindex command reported failure")
            count = int(result["parity"]["indexed"])
        else:
            build = _embedded_rebuild_in_child if isolated else _embedded_rebuild
            count = build(
                facts_dir, memory_dir, model or DEFAULT_MEMORY_MODEL, dim or DEFAULT_MEMORY_DIM, threads or 1
            )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise RestoreError(f"could not rebuild memory index: {exc}") from None
    _update_restore_state(data_dir, memory_index="complete", memory_index_count=count)
    return count


def _embedded_rebuild(facts_dir: Path, memory_dir: Path, model: str, dim: int, threads: int) -> int:
    """The rebuild with this process's own fastembed model."""
    from .memory_reindex import rebuild
    from .memory_service import build_document_embedder

    result = rebuild(
        facts_dir,
        memory_dir / "export.ndjson",
        memory_dir / "index.sqlite",
        model,
        dim,
        document_embed=build_document_embedder(model, memory_dir / "fastembed-cache", threads),
    )
    return int(result["parity"]["indexed"])


def _embedded_rebuild_child(sender, *arguments) -> None:
    try:
        sender.send(("ok", _embedded_rebuild(*arguments)))
    except BaseException as exc:  # noqa: BLE001 - the parent re-raises it under its own name
        sender.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


def _embedded_rebuild_in_child(facts_dir: Path, memory_dir: Path, model: str, dim: int, threads: int) -> int:
    """`_embedded_rebuild` in a forked child, so the model is gone from the host once it returns.

    Forked rather than spawned: the child needs no second import of the product, and it runs the
    rebuild this process would have run, under the same configuration. The parent never imports the
    embedding stack. A child the kernel kills (an OOM kill is exit 137) is named by its signal.
    """
    # A buffered line the child inherits would otherwise be written twice, once by each process.
    sys.stdout.flush()
    sys.stderr.flush()
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_embedded_rebuild_child,
        args=(sender, facts_dir, memory_dir, model, dim, threads),
        name="ummanu-memory-rebuild",
    )
    child.start()
    sender.close()
    try:
        outcome = receiver.recv()
    except EOFError:
        outcome = None
    finally:
        receiver.close()
        child.join()
    if outcome is None:
        code = child.exitcode
        how = f"was killed by signal {-code}" if code is not None and code < 0 else f"exited {code}"
        raise RuntimeError(f"the memory rebuild process {how} before it reported a result")
    status, value = outcome
    if status != "ok":
        raise RuntimeError(value)
    return int(value)


def restore_findings(data_dir: Path) -> list[str]:
    """Return stable, actionable restore findings for doctor."""
    state = restore_state(data_dir)
    findings: list[str] = []
    if not state:
        findings.append("restore is incomplete")
        return findings
    if state.get("board_parity") == "failed":
        findings.append("board restore parity failed")
    elif state.get("board") != "complete":
        findings.append("board restore is incomplete")
    if state.get("sprint_parity") == "failed":
        findings.append("sprint restore parity failed")
    elif "sprints" in state and state.get("sprints") != "complete":
        # Older recovery state without sprints has no sprint step to diagnose.
        findings.append("sprint restore is incomplete")
    if state.get("memory_index") != "complete":
        findings.append("memory index has not been rebuilt")
    if state.get("reconcile") != "complete":
        findings.append("managed reconcile has not been applied")
    return findings


def _reindex_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """Return the public failure reason from memory-mcp's JSON contract."""
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError):
        return f"exit {completed.returncode}"
    error = result.get("error") if isinstance(result, dict) else None
    return error if isinstance(error, str) and error else f"exit {completed.returncode}"


def mark_reconcile_applied(data_dir: Path) -> None:
    """Mark the explicit live reconcile verification complete."""
    if not (data_dir / RESTORE_STATE_FILE).is_file():
        return
    _update_restore_state(data_dir, reconcile="complete")


def _normalized_cards(
    data_dir: Path, *, registered_project_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    try:
        # Released materializers before the paired export left only cards.json in the local
        # restore directory. Preserve that demonstrated input; every current producer requires
        # the pair, and a present NDJSON file must still match exactly.
        cards = validated_normalized_cards(
            data_dir / "board",
            registered_project_ids=registered_project_ids,
            require_ndjson=False,
        )
    except NormalizedBoardError as exc:
        raise RestoreError(str(exc)) from None
    for card in cards:
        if not isinstance(card.get("column"), str) or _state_for_column(card["column"]) is None:
            raise RestoreError("normalized board export has an invalid column")
        if not isinstance(card.get("fields"), dict) or not isinstance(card.get("metadata"), dict):
            raise RestoreError("normalized board export has invalid task data")
        _fold_checkpoint_extensions(card)
        # Exports taken before the tasks table stated its record type carry none on older task
        # rows; absent means task, as in `reference_repair`. A present unknown value is refused.
        if "record_type" not in card["metadata"]:
            card["metadata"]["record_type"] = "task"
        if card["metadata"].get("record_type") not in _RECORD_TYPES:
            raise RestoreError(f"normalized board export card {card['reference']} has no record type")
        if not isinstance(card.get("title"), str) or not isinstance(card.get("description"), str):
            raise RestoreError("normalized board export has invalid task text")
        if "closed" in card and not isinstance(card["closed"], bool):
            raise RestoreError("normalized board export has an invalid closed state")
        if not isinstance(card.get("comments", []), list) or any(
            not isinstance(comment, dict) or not isinstance(comment.get("text"), str)
            for comment in card.get("comments", [])
        ):
            raise RestoreError("normalized board export has invalid comments")
    return sorted(cards, key=lambda card: str(card["reference"]))


def _normalized_sprints(data_dir: Path) -> list[dict[str, Any]]:
    """Read the exported sprint entities."""
    from ummanu.sprints import SPRINT_REFERENCE_PREFIX

    path = data_dir / "board" / "sprints.json"
    if not path.is_file():
        return []
    try:
        sprints = json.loads(path.read_text(encoding="utf-8"))["sprints"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RestoreError("normalized sprint export is unavailable") from None
    if not isinstance(sprints, list) or any(not isinstance(sprint, dict) for sprint in sprints):
        raise RestoreError("normalized sprint export is invalid")
    refs = [sprint.get("reference") for sprint in sprints]
    if any(not isinstance(ref, str) or not ref.startswith(SPRINT_REFERENCE_PREFIX) for ref in refs) or len(
        set(refs)
    ) != len(refs):
        raise RestoreError("normalized sprint export has invalid references")
    for sprint in sprints:
        if not isinstance(sprint.get("goal"), str) or not sprint["goal"].strip():
            raise RestoreError("normalized sprint export has an invalid goal")
        if sprint.get("status") not in {"open", "closed", "stopped"}:
            raise RestoreError("normalized sprint export has an invalid status")
        if not isinstance(sprint.get("definition_of_done"), str) or not isinstance(
            sprint.get("current_task"), str
        ):
            raise RestoreError("normalized sprint export has invalid entity text")
        if not isinstance(sprint.get("repositories"), list) or any(
            not isinstance(repo, str) for repo in sprint["repositories"]
        ):
            raise RestoreError("normalized sprint export has invalid repositories")
        # Pre-ownership exports retain absent ownership.
        if not isinstance(sprint.get("product", ""), str):
            raise RestoreError("normalized sprint export has an invalid product")
        if not isinstance(sprint.get("po_session", ""), str):
            raise RestoreError("normalized sprint export has an invalid po_session")
        for field in ("issues", "reservations", "allowed_productions"):
            value = sprint.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise RestoreError(f"normalized sprint export has invalid {field}")
        try:
            parse_local_run_exceptions(sprint.get("local_run_exceptions", []), projects=sprint.get("reservations", []))
        except ValueError as exc:
            raise RestoreError(f"normalized sprint export has invalid local_run_exceptions: {exc}") from None
        try:
            stored_decisions(sprint.get("owner_decisions", []))
        except (ValueError, TypeError, KeyError) as exc:
            raise RestoreError(f"normalized sprint export has invalid owner_decisions: {exc}") from None
        e2e = sprint.get("e2e", {"budget": 3, "used": 0, "charges": []})
        if (not isinstance(e2e, dict) or set(e2e) != {"budget", "used", "charges"}
                or any(type(e2e[key]) is not int or not 0 <= e2e[key] <= 2_147_483_647 for key in ("budget", "used"))
                or not isinstance(e2e["charges"], list)):
            raise RestoreError("normalized sprint export has invalid e2e budget/counters/charges")
        budget = sprint.get("budget")
        if (
            not isinstance(budget, dict)
            or not isinstance(budget.get("by_type"), dict)
            or any(not isinstance(count, int) for count in budget["by_type"].values())
        ):
            raise RestoreError("normalized sprint export has an invalid budget")
        # Missing legacy uncharged counts restore as zero.
        uncharged = budget.get("uncharged", {})
        if not isinstance(uncharged, dict) or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in uncharged.values()
        ):
            raise RestoreError("normalized sprint export has an invalid budget")
        if sprint.get("resume") is not None and not isinstance(sprint.get("resume"), dict):
            raise RestoreError("normalized sprint export has an invalid resume entry")
        if not isinstance(sprint.get("audit"), dict):
            raise RestoreError("normalized sprint export has invalid audit metadata")
        if not isinstance(sprint.get("comments", []), list) or any(
            not isinstance(comment, dict) or not isinstance(comment.get("text"), str)
            for comment in sprint.get("comments", [])
        ):
            raise RestoreError("normalized sprint export has invalid records")
        sprint.setdefault("comments", [])
    return sorted(sprints, key=lambda sprint: str(sprint["reference"]))


def _check_sql_sprint_current_tasks(
    cards: list[dict[str, Any]], sprints: list[dict[str, Any]]
) -> None:
    """Refuse a normalized cursor that the scoped SQL relation cannot represent.

    A cursor is a pointer, never an instruction to attach or reparent a Card.  Checking the two
    exported sets here keeps an invalid archive from writing even its first Pipeline row and gives
    the operator a stable restore error instead of a commit-time foreign-key diagnostic.
    """
    linked = {
        (str(card["reference"]), str(card.get("metadata", {}).get("sprint_ref") or ""))
        for card in cards
    }
    for sprint in sprints:
        current = str(sprint.get("current_task") or "")
        reference = str(sprint["reference"])
        if current and (current, reference) not in linked:
            raise RestoreError(
                f"normalized sprint export current_task {current!r} is not an included Card "
                f"already linked to {reference}"
            )


def _restore_request_prefix(data_dir: Path, audit: SqlTaskAudit, live_refs: set[str]) -> str:
    """Return the request-id namespace this recovery writes its audit under.

    Restore events are durable, so a second recovery from a recovered checkpoint meets its own
    request ids again; reusing them would short-circuit every write as already committed against a
    backend that holds nothing. A namespace whose committed events name entities the target does not
    have was written against an earlier backend, so this recovery takes a fresh one and
    `restore-state.json` keeps it.
    """
    state = restore_state(data_dir)
    token = state.get("restore_namespace")
    if (
        not isinstance(token, str)
        or not token
        or not _namespace_is_local(audit, token, live_refs)
        or _namespace_is_exported(data_dir, token)
    ):
        token = uuid.uuid4().hex
        # Missing `sprints` means this recovery never tracked that step.
        _update_restore_state(data_dir, restore_namespace=token, sprints=state.get("sprints", "pending"))
    return f"restore:{token}:"


def _namespace_is_exported(data_dir: Path, token: str) -> bool:
    """Whether the history this recovery is about to restore already holds this namespace.

    The store starts a recovery empty and gets its history back from the export after the
    namespace is chosen (`_restore_board_history`), so an earlier recovery's events are not in the
    audit yet at that moment -- they are in the export, beside the `restore-state.json` that names the
    same token. Reusing it would have this recovery write its own events under request ids the
    restored history is about to claim with another payload. An export that cannot be read answers
    no here; `_restore_board_history` refuses it by name later.
    """
    prefix = f"restore:{token}:"
    board = data_dir / "board"
    try:
        path = board / "audit.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            events = payload.get("events") if isinstance(payload, dict) else None
        else:
            ndjson = board / "audit.ndjson"
            if not ndjson.is_file():
                return False
            events = [
                json.loads(line) for line in ndjson_lines(ndjson.read_text(encoding="utf-8")) if line
            ]
    except (OSError, ValueError):
        return False
    return isinstance(events, list) and any(
        isinstance(event, dict) and str(event.get("request_id") or "").startswith(prefix)
        for event in events
    )


def _namespace_is_local(audit: SqlTaskAudit, token: str, live_refs: set[str]) -> bool:
    prefix = f"restore:{token}:"
    events = [
        event
        for event in audit.events()
        if str(event.get("request_id") or "").startswith(prefix)
    ]
    return all(
        str(event.get("ref") or "") in live_refs
        for event in events
    )


def _restore_board_metadata(card: dict[str, Any]) -> dict[str, str]:
    result = {str(key): str(value) for key, value in card["metadata"].items()}
    for key, value in _restore_fields(card).items():
        result.setdefault(key, value)
    return result


def _restore_comments(card: dict[str, Any]) -> list[str]:
    return [str(comment["text"]) for comment in card.get("comments", [])]


def _restore_position(card: dict[str, Any]) -> int | None:
    position = card.get("position")
    return position if isinstance(position, int) and position > 0 else None


def _restored_order_mismatch(cards: list[dict[str, Any]], actual: dict[str, dict[str, Any]]) -> bool:
    """Сверяет порядок открытых карточек внутри (колонка, свимлейн).

    Абсолютные номера позиций сравнивать нельзя: старая доска держала позиции плотными среди активных
    задач, а закрытая задача сохраняет устаревшее значение и перестаёт занимать слот, поэтому
    экспорт живой доски содержит и дыры, и повторы. Восстановимо здесь только относительное
    расположение; у закрытых карточек позиции нет вовсе.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for card in cards:
        if card.get("closed"):
            continue
        groups.setdefault((str(card["column"]), str(card.get("swimlane") or "")), []).append(card)
    for group in groups.values():
        # Match the creation ordering and tie-breaker.
        expected = [card["reference"] for card in sorted(group, key=_restore_card_order)]
        live = sorted(
            group,
            key=lambda card: (
                _positive_int(actual[card["reference"]].get("position")) or 0,
                str(card["reference"]),
            ),
        )
        if expected != [card["reference"] for card in live]:
            return True
    return False


def _reconcile_restored_order(
    writer: TaskWriter,
    cards: list[dict[str, Any]],
    actual: dict[str, dict[str, Any]],
    prefix: str,
) -> None:
    """Repair only active groups whose post-close relative order differs."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for card in cards:
        if not card.get("closed"):
            groups.setdefault((str(card["column"]), str(card.get("swimlane") or "")), []).append(card)
    for index, ((column, swimlane), group) in enumerate(sorted(groups.items())):
        expected = [card["reference"] for card in sorted(group, key=_restore_card_order)]
        live = sorted(
            expected,
            key=lambda reference: (
                _positive_int(actual[reference].get("position")) or 0,
                reference,
            ),
        )
        if live == expected:
            continue
        writer.reconcile_restore_order(
            column=column,
            swimlane=swimlane,
            references=expected,
            request_id=f"{prefix}order:{index}",
        )


def _restore_card_order(card: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(card["column"]),
        str(card.get("swimlane") or ""),
        _restore_position(card) or 0,
        str(card["reference"]),
    )


def _restore_fields(card: dict[str, Any]) -> dict[str, str]:
    fields = card["fields"]
    metadata = card["metadata"]
    value = lambda name: str(metadata.get(name, fields.get(name, "")) or "")
    typed = TaskMetadata.from_legacy(
        {
            "task_type": value("task_type"),
            "complexity": value("complexity"),
            "family_preference": value("family_preference"),
            "codex_launch_mode": value("codex_launch_mode"),
        },
        codex_modes=CODEX_LAUNCH_MODES,
    )
    return {
        "project": value("project"),
        "task_type": typed.task_type_text,
        "blocked_by": value("blocked_by"),
        "head": value("head"),
        "review_head": value("review_head"),
        "slug": value("slug"),
        "base_branch": value("base_branch"),
        "seed_ref": value("seed_ref"),
        "supersedes": value("supersedes"),
        "complexity": typed.routing.complexity.value,
        "family_preference": typed.routing.family_preference.value,
        # Legacy `exec` reads as no mode; live modes round-trip unchanged.
        "codex_launch_mode": typed.routing.codex_launch_mode or "",
    }




def _fold_checkpoint_extensions(card: dict[str, Any]) -> None:
    """Read a record's `extensions` through the one fold rule, into what restore writes.

    A checkpoint or archive written before revision `0014_neutral_extension_bag` may carry its bag
    under an older top-level key.  Every such key folds into the current bag, and the bag's fields
    reach `metadata` (which is what restore writes, and what lands back in the bag) unless
    `metadata` already names them; its lane fills an empty `swimlane`.
    """
    if "extensions" not in card:
        return
    card["extensions"] = fold_extension_bags(card["extensions"])
    bag = card["extensions"].get(EXTENSION_BAG, {})
    for name, value in bag.items():
        if value is None:
            continue
        if name == "swimlane":
            if not card.get("swimlane"):
                card["swimlane"] = str(value)
        else:
            card["metadata"].setdefault(str(name), str(value))


def _core_from_export(card: dict[str, Any]) -> dict[str, Any]:
    fields = _restore_fields(card)
    metadata = card["metadata"]
    # Not a restore field: a card exported without a stored choice is restored without one, and
    # both read as the same value.
    kind = TaskMetadata.from_legacy(
        {"review": metadata.get("review"), "live_impact": metadata.get("live_impact")},
        codex_modes=CODEX_LAUNCH_MODES,
    )
    return {
        "ref": card["reference"],
        "title": card["title"],
        "description": card["description"],
        "state": _state_for_column(card["column"]),
        "closed": bool(card.get("closed", False)),
        "project": fields["project"],
        "type": fields["task_type"],
        "review": kind.review.value,
        "live_impact": kind.live_impact,
        "blocked_by": fields["blocked_by"] or None,
        "claim": {"worker": metadata.get("claim") or None, "claimed_at": None},
        "routing": {
            "complexity": fields["complexity"],
            "family_preference": fields["family_preference"],
            "head": fields["head"] or None,
            "review_head": fields["review_head"] or None,
            "resolved_head": metadata.get("resolved_head") or None,
            "resolved_review_head": metadata.get("resolved_review_head") or None,
            "codex_launch_mode": fields["codex_launch_mode"] or None,
        },
        "workspace": {
            "slug": metadata.get("slug") or None,
            "base_branch": metadata.get("base_branch") or None,
            "seed_ref": metadata.get("seed_ref") or None,
            "supersedes": metadata.get("supersedes") or None,
        },
        # Absolute position is intentionally not compared; see _restored_order_mismatch.
        "swimlane": str(card.get("swimlane") or "") or None,
        "comments": [{"body": body} for body in _restore_comments(card)],
        "product_issue_metadata": _product_issue_metadata(card["metadata"]),
    }


def _core_from_live(card: dict[str, Any]) -> dict[str, Any]:
    extensions = card.get("extensions", {}).get(EXTENSION_BAG, {})
    return {
        "ref": card.get("ref"),
        "title": card.get("title"),
        "description": card.get("description"),
        "state": card.get("state"),
        "closed": bool(card.get("closed", False)),
        "project": card.get("project"),
        "type": card.get("type"),
        "review": card.get("review"),
        "live_impact": card.get("live_impact"),
        "blocked_by": card.get("blocked_by"),
        "claim": card.get("claim"),
        "routing": {
            "complexity": card["routing"].get("complexity"),
            "family_preference": card["routing"].get("family_preference"),
            "head": card["routing"].get("head_override"),
            "review_head": card["routing"].get("review_head_override"),
            "resolved_head": card["routing"].get("resolved_worker_head"),
            "resolved_review_head": card["routing"].get("resolved_review_head"),
            "codex_launch_mode": card["routing"].get("codex_launch_mode"),
        },
        "workspace": card.get("workspace"),
        "swimlane": extensions.get("swimlane"),
        "comments": [{"body": str(comment.get("body") or "")} for comment in card.get("comments", [])],
        "product_issue_metadata": _product_issue_metadata(extensions),
    }


def _product_issue_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    record_type = metadata.get("record_type")
    if record_type not in {"issue", "product"}:
        return {}
    return {key: str(metadata[key]) for key in _PRODUCT_ISSUE_METADATA if key in metadata}


def _state_for_column(column: str) -> str | None:
    return _STATE_BY_COLUMN.get(column)


def _update_restore_state(data_dir: Path, **changes: Any) -> None:
    state = restore_state(data_dir)
    state.update(changes)
    state["version"] = 1
    try:
        write_text_atomic(
            data_dir / RESTORE_STATE_FILE,
            json.dumps(state, indent=2, sort_keys=True) + "\n",
        )
    except RuntimeError as exc:
        raise RestoreError(f"could not record restore progress: {exc}") from None


def bootstrap_empty(instance_path: Path, *, dry_run: bool = False) -> RestorePlan:
    _, target, identity = _target(instance_path)
    _reject_existing_target(target)
    plan = RestorePlan(
        archive=Path(),
        backup_kind="empty",
        backup_version=POSTGRES_BACKUP_VERSION,
        data_dir=target,
        components=restore_plan_components(CORE_POLICY, empty=True),
        instance_identity=identity,
    )
    if not dry_run:
        init_layout(target)
    return plan


def restore_backup(
    archive: Path,
    instance_path: Path,
    *,
    dry_run: bool = False,
    _allow_postgres_engine: bool = False,
) -> RestorePlan:
    _, target, target_identity = _target(instance_path)
    _reject_existing_target(target)
    archive = archive.expanduser()
    if not archive.is_file():
        raise RestoreError(f"archive not found: {archive}")

    try:
        verified = _verify_plain_tar(archive)
    except RuntimeError as exc:
        raise RestoreError(str(exc)) from None
    else:
        if verified.code or verified.findings or not isinstance(verified.manifest, dict):
            findings = "; ".join(verified.findings) or "archive verification failed"
            raise RestoreError(findings)
        manifest = verified.manifest
        archive_identity = _archive_identity(manifest)
        if archive_identity != target_identity:
            raise RestoreError("archive instance identity does not match target instance")
        kind = manifest.get("backup_kind")
        if kind not in BACKUP_KINDS or manifest.get("version") != POSTGRES_BACKUP_VERSION:
            raise RestoreError("archive kind or version is not supported")
        policy = policy_for(kind)
        if policy is None:
            raise RestoreError("archive kind is not supported")
        if kind == "full" and not _allow_postgres_engine:
            raise RestoreError("PostgreSQL full archives require ummanu restore-postgres")
        plan = RestorePlan(
            archive=archive,
            backup_kind=kind,
            backup_version=POSTGRES_BACKUP_VERSION,
            data_dir=target,
            components=restore_plan_components(policy),
            instance_identity=target_identity,
        )
        if dry_run:
            return plan
        _stage_and_publish(archive, target, policy=policy)
        return plan


def restore_postgres_backup(
    archive: Path,
    instance_path: Path,
    *,
    dry_run: bool = False,
) -> RestorePlan:
    """Restore a PostgreSQL full archive into a distinct, empty local store.

    The target container, credentials and roles remain owned by the existing
    board-store lifecycle.  This operation applies/verifies that lifecycle,
    restores data as owner, proves normalized parity, and starts no processes.
    """
    archive = archive.expanduser()
    verified = _verify_plain_tar(archive)
    manifest = verified.manifest if isinstance(verified.manifest, dict) else {}
    if verified.code or verified.findings:
        raise RestoreError("; ".join(verified.findings) or "archive verification failed")
    _, target, target_identity = _target(instance_path)
    if _archive_identity(manifest) != target_identity:
        raise RestoreError("archive instance identity does not match target instance")
    policy = policy_for("full")
    if (
        manifest.get("board_backend") != "postgres"
        or manifest.get("backup_kind") != "full"
        or manifest.get("version") != POSTGRES_BACKUP_VERSION
        or policy is None
    ):
        raise RestoreError("PostgreSQL local restore requires a PostgreSQL full archive")
    plan = RestorePlan(
        archive=archive,
        backup_kind="full",
        backup_version=POSTGRES_BACKUP_VERSION,
        data_dir=target,
        components=restore_plan_components(policy),
        instance_identity=target_identity,
    )
    component = manifest.get("components", {}).get("postgres_dump")
    if not isinstance(component, dict):
        raise RestoreError("PostgreSQL archive has no engine dump component")
    from ummanu._fsutil import sha256_file
    from ummanu.board.postgres_recovery import (
        PostgresRecoveryError,
        restore_dump,
        target_counts,
        write_restore_marker,
    )
    from ummanu.board.store import BoardStoreError, resolve

    marker = target / "postgres-restore.json"
    if target.exists():
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
            config = resolve(_instance_file_for_restore(instance_path).parent)
        except (OSError, ValueError, BoardStoreError):
            raise RestoreError(f"target data root already exists: {target}") from None
        if record.get("archive_sha256") != sha256_file(archive):
            raise RestoreError("existing PostgreSQL restore belongs to a different archive")
        if target_counts(config) != component.get("table_counts"):
            raise RestoreError("existing PostgreSQL restore no longer matches the archive")
        _verify_postgres_normalized_parity(target, _instance_file_for_restore(instance_path).parent)
        return plan
    if dry_run:
        return plan
    try:
        with tempfile.TemporaryDirectory(prefix=".ummanu-postgres-archive-") as temporary:
            dump = Path(temporary) / "postgres.dump"
            with tarfile.open(archive, "r") as bundle:
                member = bundle.getmember(f"{ARCHIVE_ROOT}/engine/postgres.dump")
                source = bundle.extractfile(member)
                if source is None or not member.isfile() or _unsafe_member(member):
                    raise RestoreError("PostgreSQL dump archive entry is unsafe")
                with source, dump.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            dump.chmod(0o600)
            result = restore_dump(dump, _instance_file_for_restore(instance_path).parent, component)
        plan = restore_backup(archive, instance_path, _allow_postgres_engine=True)
        _verify_postgres_normalized_parity(target, _instance_file_for_restore(instance_path).parent)
        write_restore_marker(target, archive, result)
        return plan
    except PostgresRecoveryError as exc:
        raise RestoreError(str(exc)) from None


def _instance_file_for_restore(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded / "instance.yaml" if expanded.is_dir() else expanded


def _verify_postgres_normalized_parity(data_dir: Path, instance_dir: Path) -> None:
    expected_cards = _normalized_cards(data_dir)
    expected_sprints = _normalized_sprints(data_dir)
    client = None
    try:
        client = board_client(instance_dir, serves=(CARD, SPRINT), role="read")
        actual_cards = [
            __import__("ummanu.data", fromlist=["normalize_board_card"]).normalize_board_card(card, card)
            for card in TaskReader(client).export()
            if isinstance(card, dict) and str(card.get("reference") or "")
        ]
        from ummanu.board.sql_audit import SqlTaskAudit
        from ummanu.data import normalize_sprint_entity
        from ummanu.sprints import SprintReader

        actual_sprints = [normalize_sprint_entity(item) for item in SprintReader(client).export()]
        history_payload = json.loads((data_dir / "board" / "audit.json").read_text(encoding="utf-8"))
        expected_history = history_payload.get("events") if isinstance(history_payload, dict) else None
        actual_history = SqlTaskAudit(client).events()
    except TaskError as exc:
        raise RestoreError(f"restored PostgreSQL normalized verification failed: {exc.message}") from None
    except (OSError, ValueError) as exc:
        raise RestoreError(f"portable PostgreSQL verification data is invalid: {exc}") from None
    finally:
        if client is not None:
            client.connection.close()
    by_ref = lambda rows: sorted(rows, key=lambda row: str(row.get("reference") or ""))
    if (
        by_ref(actual_cards) != by_ref(expected_cards)
        or by_ref(actual_sprints) != by_ref(expected_sprints)
        or actual_history != expected_history
    ):
        raise RestoreError("restored PostgreSQL store does not match the portable normalized export")


def plan_as_json(plan: RestorePlan, *, action: str, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "action": action,
        "dry_run": dry_run,
        "archive": str(plan.archive) if action.startswith("restore") else None,
        "backup_kind": plan.backup_kind,
        "backup_version": plan.backup_version,
        "data_dir": str(plan.data_dir),
        "components": list(plan.components),
        "instance_identity": plan.instance_identity,
        "next_steps": _next_steps(plan.components),
    }


def _target(instance_path: Path) -> tuple[Path, Path, dict[str, str]]:
    report = validate_instance(instance_path)
    if report.errors:
        raise RestoreError("invalid target instance: " + "; ".join(map(str, report.errors)))
    try:
        target = instance_data_dir(report.instance_path)
    except DataDirError as exc:
        raise RestoreError(str(exc)) from None
    return report.instance_path, target, _identity(report.instance)


def _identity(config: dict[str, Any]) -> dict[str, str]:
    offsite = config.get("offsite")
    remote = offsite.get("instance_remote") if isinstance(offsite, dict) else None
    name = config.get("name")
    if not isinstance(name, str) or not isinstance(remote, str):
        raise RestoreError("target instance has no usable identity")
    return {"name": name, "instance_remote": remote}


def _archive_identity(manifest: dict[str, Any]) -> dict[str, str]:
    instance = manifest.get("instance")
    identity = instance.get("identity") if isinstance(instance, dict) else None
    if not isinstance(identity, dict):
        raise RestoreError("archive has no instance identity")
    name, remote = identity.get("name"), identity.get("instance_remote")
    if not isinstance(name, str) or not isinstance(remote, str):
        raise RestoreError("archive has invalid instance identity")
    return {"name": name, "instance_remote": remote}


def _next_steps(components: tuple[dict[str, str], ...]) -> list[str]:
    labels = {
        "board_restore": "board restore",
        "memory_index": "memory index rebuild",
        "host_reconcile": "reconcile",
    }
    return [labels[component["name"]] for component in components if component["name"] in labels]


def _reject_existing_target(target: Path) -> None:
    if target.exists():
        raise RestoreError(f"target data root already exists: {target}")


def _unsafe_member(member: tarfile.TarInfo) -> bool:
    path = Path(member.name)
    return (
        path.is_absolute()
        or ".." in path.parts
        or member.issym()
        or member.islnk()
        or member.isdev()
        or member.isfifo()
    )


def _allowed_data_path(relative: str, policy: BackupPolicy) -> bool:
    return not should_skip_data_entry(Path(relative), policy=policy)


def _stage_and_publish(plain_archive: Path, target: Path, *, policy: BackupPolicy) -> None:
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.restore-", dir=parent) as temporary:
            data_staging = Path(temporary) / "data"
            init_layout(data_staging)
            with tarfile.open(plain_archive, "r") as archive:
                prefix = f"{ARCHIVE_ROOT}/ummanu-data/"
                for member in archive.getmembers():
                    if not member.name.startswith(prefix):
                        continue
                    relative = Path(member.name.removeprefix(prefix))
                    if is_memory_journal_git_runtime_entry(relative):
                        continue
                    # A full archive from before the model cache left the policy still carries it.
                    if is_memory_model_cache_entry(relative) and not _allowed_data_path(
                        relative.as_posix(), policy
                    ):
                        continue
                    if _unsafe_member(member) or not _allowed_data_path(relative.as_posix(), policy):
                        raise RestoreError(f"unsafe archive entry: {member.name}")
                    if member.isdir():
                        (data_staging / relative).mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise RestoreError(f"unsupported archive entry type: {member.name}")
                    destination = data_staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise RestoreError(f"could not read archive entry: {member.name}")
                    with source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
            _update_restore_state(
                data_staging,
                board="pending",
                sprints="pending",
                memory_index="pending",
                reconcile="pending",
                # Archive audit and progress belong to this data directory's backend.
                restore_namespace=uuid.uuid4().hex,
            )
            _reject_existing_target(target)
            os.replace(data_staging, target)
    except RestoreError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"restore staging failed: {exc}") from None


def _memory_canon_dir(data_dir: Path, instance_dir: Path | None) -> Path:
    """Canon facts, from the private repo; the data dir is the pre-flatten path."""
    if instance_dir is not None:
        facts_dir = state_repo.memory_facts_dir(instance_dir)
        if facts_dir.is_dir():
            return facts_dir
        raise RestoreError(f"memory canon is not available for index rebuild: {facts_dir}")
    legacy = data_dir / "memory" / "facts"
    if not legacy.is_dir():
        raise RestoreError("memory facts are not available for index rebuild")
    return legacy
