"""Audit-aware board restore operations for ``TaskWriter``."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from ummanu.board.backend import BOARD_STORE_KIND, entity_id, entity_number
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.import_order import record_rank


@dataclass(frozen=True)
class RestoreCommentOccurrence:
    """One exported comment occurrence and its stable backend/audit identity."""

    reference: str
    task_id: int
    body: str
    occurrence: int
    request_id: str
    entity: str = "card"
    recorded_at: str = ""


@dataclass(frozen=True)
class RestoreCardObligation:
    """One normalized row owned by the restore-only bulk transaction."""

    card: dict[str, Any]
    metadata: dict[str, str]
    request_id: str
    column_id: int
    swimlane_id: int
    identity: dict[str, Any]


def _entity_number(kind: str, identity: object) -> int:
    """A live row's backend number, read through `board/backend.py`'s one identity parser."""
    number = entity_number(kind, identity)
    if number is None:
        raise RuntimeError(f"restored row carries no usable {kind} identity: {identity!r}")
    return number


def _set_restore_phase(client: Any, phase: str) -> None:
    """Expose restore phase boundaries to an optional benchmark observer."""
    hook = getattr(client, "set_restore_phase", None)
    if callable(hook):
        hook(phase)


def _restore_create_payload(item: RestoreCardObligation, board_id: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": board_id,
        "title": str(item.card["title"]),
        "description": str(item.card.get("description") or ""),
        "column_id": item.column_id,
        "reference": str(item.card["reference"]),
    }
    if item.swimlane_id:
        payload["swimlane_id"] = item.swimlane_id
    return payload


def _restore_payload_error(item: Any, phase: str, error: Exception) -> Exception:
    from ummanu.tasks import TaskError

    card = item.card if isinstance(item, RestoreCardObligation) else item
    reference = str(card.get("reference") or "<unknown>")
    message = getattr(error, "message", str(error))
    return TaskError("validation", f"restored-card {phase} payload for {reference}: {message}", 2)


def refused_in_transaction(client: Any, error: BaseException) -> bool:
    """Whether a failed store call is a refusal the import's transaction cannot read past.

    The import runs in one transaction (`restore.import_normalized_board`), and PostgreSQL aborts
    a transaction at its first refused statement: every statement after it, the proving read
    included, answers only "current transaction is aborted". So a refusal there is definite, and
    re-reading to prove what landed would replace what the store said with that echo -- the
    ummanu-45 drill saw a foreign-key refusal as "batched comment write is uncertain". Only
    transport loss (`backend_unavailable`, `sql_cards._driver_error`) leaves an outcome unknown,
    and only it, or a refusal outside a transaction, takes the ambiguity path.
    """
    from ummanu.tasks import TaskError

    return (
        isinstance(error, TaskError)
        and error.code != "backend_unavailable"
        and bool(getattr(client, "_depth", 0))
    )


def store_refusal(what: str, error: BaseException) -> Exception:
    """The refusal of a restored batch, carrying what the store said."""
    from ummanu.tasks import TaskError

    message = getattr(error, "message", str(error))
    return TaskError("backend_error", f"board store refused the restored {what}: {message}", 1)


def _foreign_key_rank(item: RestoreCardObligation) -> int:
    """The position of the record's write phase in the import order (`board.import_order`).

    `issues.product_id` is an immediate foreign key, so a Product's row is written before an
    Issue's, and both before a task card's `task_issues`, whatever order the export's (column,
    swimlane, position, reference) sort put them in. The sort that uses this is stable, so records
    of one kind keep their order.
    """
    return record_rank(str(item.metadata.get("record_type") or ""))


def _store_placed(item: RestoreCardObligation) -> bool:
    """Whether the store places this record itself: a Product or Issue lives in the Issues column of
    its product's lane by construction, and the store refuses to move one (`moveTaskPosition`)."""
    return str(item.metadata.get("record_type") or "") in {"product", "issue"}


def _discard_pending_obligation(writer: Any, item: RestoreCardObligation) -> None:
    event = writer.audit.pending_event(item.request_id)
    if event is not None:
        writer.audit.discard(item.request_id, event)


def restore_cards_batched(
    writer: Any,
    cards: list[dict[str, Any]],
    *,
    board_id: int,
    columns: dict[int, str],
    swimlanes: dict[int, str],
    existing: dict[str, dict[str, Any]],
    request_prefix: str,
) -> None:
    """Create and initialize normalized cards without the interactive read cycle."""
    from ummanu.restore import _restore_board_metadata
    from ummanu.tasks import TaskError, _matching_swimlane, _now, _positive_int

    column_ids = {title: identifier for identifier, title in columns.items()}
    obligations: list[RestoreCardObligation] = []
    for card in cards:
        reference = str(card["reference"])
        column = str(card.get("column") or "")
        column_id = column_ids.get(column)
        if column_id is None:
            raise TaskError("backend_error", f"restored card has an unknown column: {column}", 1)
        lane = str(card.get("swimlane") or "")
        swimlane_id = _matching_swimlane(swimlanes, lane) or 0
        metadata = _without_retired_launch_mode(_restore_board_metadata(card))
        identity = _restore_card_identity(card, metadata)
        request_id = f"{request_prefix}card:{reference}"
        obligations.append(
            RestoreCardObligation(card, metadata, request_id, column_id, swimlane_id, identity)
        )

    _validate_restore_inventory(existing, obligations)
    missing = sorted(
        (item for item in obligations if str(item.card["reference"]) not in existing), key=_foreign_key_rank
    )
    create_entries = [(item, "createTask", _restore_create_payload(item, board_id)) for item in missing]

    _set_restore_phase(writer.client, "audit")
    for item in obligations:
        reference = str(item.card["reference"])
        committed = writer.audit.committed_event(item.request_id)
        if committed is not None:
            if committed.get("kind") == "restored":
                # Released restores used this same stable request id.  Final parity below remains
                # their authoritative compatibility proof.
                pass
            else:
                writer.audit.require_claim(
                    committed, kind="restored_bulk", reference=reference, identity=item.identity
                )
        else:
            pending = writer.audit.pending_event(item.request_id)
            if pending is not None:
                writer.audit.require_claim(
                    pending, kind="restored_bulk", reference=reference, identity=item.identity
                )
            else:
                writer.audit.stage(
                    item.request_id,
                    {
                        "event_id": "evt_" + uuid.uuid4().hex,
                        "schema_version": 1,
                        "occurred_at": _now(),
                        "actor": {"role": "steward", "id": "restore"},
                        "kind": "restored_bulk",
                        "outcome": "success",
                        "task_id": "",
                        "ref": reference,
                        "backend": {
                            "kind": BOARD_STORE_KIND,
                            "task_id": None,
                            "revision": "pending",
                        },
                        "request_id": item.request_id,
                        "payload": item.identity,
                    },
                )
    if missing:
        calls = [(method, payload) for _item, method, payload in create_entries]
        definite_create_failure = False
        _set_restore_phase(writer.client, "create")
        try:
            answers = writer.client.call_batch(calls)
            if any(_positive_int(answer) is None for answer in answers):
                definite_create_failure = True
        except TaskError as exc:
            if exc.code == "validation":
                raise _restore_payload_error(missing[0], "create", exc) from None
            if refused_in_transaction(writer.client, exc):
                raise store_refusal("card create batch", exc) from None
            # Transport loss or a malformed/incomplete aggregate answer is genuinely ambiguous.
        _set_restore_phase(writer.client, "proof")
        existing = _restore_inventory(writer.client, board_id)
        _validate_restore_inventory(existing, obligations)
        absent = [
            str(item.card["reference"]) for item in missing if str(item.card["reference"]) not in existing
        ]
        if absent:
            if definite_create_failure:
                for item in missing:
                    if str(item.card["reference"]) in absent:
                        _discard_pending_obligation(writer, item)
                absent_item = next(item for item in missing if str(item.card["reference"]) == absent[0])
                record_type = str(absent_item.card.get("metadata", {}).get("record_type") or "")
                if record_type in {"product", "issue"}:
                    raise TaskError(
                        "backend_error",
                        "could not create restored Product or Issue record: "
                        f"board store rejected create for {absent[0]}",
                        1,
                    )
                raise TaskError(
                    "backend_error",
                    f"board store rejected restored-card create for {absent[0]}",
                    1,
                )
            raise TaskError(
                "audit_pending",
                f"batched restored-card create is uncertain; retry absent reference {absent[0]}",
                4,
            )

    task_ids = [_restore_task_id(existing, item) for item in obligations]
    _set_restore_phase(writer.client, "metadata_state")
    metadata_answers = writer.client.call_batch(
        ("getTaskMetadata", {"task_id": task_id}) for task_id in task_ids
    )
    live_metadata = [_metadata_map(answer) for answer in metadata_answers]
    write_entries: list[tuple[RestoreCardObligation, str, dict[str, Any]]] = []
    for item, task_id, metadata in zip(obligations, task_ids, live_metadata, strict=True):
        committed = writer.audit.committed_event(item.request_id)
        row = existing[str(item.card["reference"])]
        if committed is not None:
            if not _metadata_holds(metadata, item.metadata) or not _restore_placement_matches(row, item):
                raise TaskError(
                    "backend_error",
                    f"committed restored card no longer matches normalized data: {item.card['reference']}",
                    1,
                )
            continue
        pending = writer.audit.pending_event(item.request_id)
        initialized = bool(
            isinstance(pending, dict)
            and str(pending.get("backend", {}).get("revision") or "").startswith("initialized")
        )
        if not _metadata_holds(metadata, item.metadata):
            write_entries.append((item, "saveTaskMetadata", {"task_id": task_id, "values": item.metadata}))
        # createTask does not accept an initial position.  Run the exported placement
        # once for every fresh obligation.  Overlapping archived positions are intentionally
        # reconciled after closure, when the active-only order is knowable.  A Product or Issue
        # is placed by the store, which refuses to move one; the proof below still checks it.
        if not _store_placed(item) and (not initialized or not _restore_placement_matches(row, item)):
            write_entries.append(
                (
                    item,
                    "moveTaskPosition",
                    {
                        "project_id": board_id,
                        "task_id": task_id,
                        "column_id": item.column_id,
                        "position": max(1, int(item.card.get("position") or 1)),
                        "swimlane_id": item.swimlane_id,
                    },
                )
            )
    write_entries.sort(key=lambda entry: _foreign_key_rank(entry[0]))
    definite_initialization_failure = False
    if write_entries:
        writes = [(method, payload) for _item, method, payload in write_entries]
        try:
            answers = writer.client.call_batch(writes)
            if any(answer is not True for answer in answers):
                definite_initialization_failure = True
        except TaskError as exc:
            if exc.code == "validation":
                raise _restore_payload_error(write_entries[0][0], "metadata/state", exc) from None
            if refused_in_transaction(writer.client, exc):
                raise store_refusal("card metadata/state batch", exc) from None
            # Reconcile genuinely uncertain aggregate outcomes from fresh evidence below.

    _set_restore_phase(writer.client, "proof")
    proved_rows = _restore_inventory(writer.client, board_id)
    _validate_restore_inventory(proved_rows, obligations)
    proved_ids = [_restore_task_id(proved_rows, item) for item in obligations]
    proved_metadata = writer.client.call_batch(
        ("getTaskMetadata", {"task_id": task_id}) for task_id in proved_ids
    )
    for item, task_id, answer in zip(obligations, proved_ids, proved_metadata, strict=True):
        if not _metadata_holds(_metadata_map(answer), item.metadata) or not _restore_placement_matches(
            proved_rows[str(item.card["reference"])], item
        ):
            if definite_initialization_failure:
                raise TaskError(
                    "backend_error",
                    f"board store rejected restored-card metadata/state for {item.card['reference']}",
                    1,
                )
            raise TaskError(
                "audit_pending",
                f"board parity check failed: restored-card initialization is incomplete for {item.card['reference']}",
                4,
            )
        event = writer.audit.pending_event(item.request_id)
        if event is not None:
            backend = BOARD_STORE_KIND
            event["task_id"] = entity_id("task", task_id)
            event["backend"]["kind"] = backend
            event["backend"]["task_id"] = task_id
            event["backend"]["revision"] = "initialized"
            writer.audit.stage(item.request_id, event)


def close_restored_cards_batched(
    client: Any,
    cards: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    *,
    board_id: int,
) -> None:
    """Close archived normalized rows in bounded calls after their comments are proved."""
    from ummanu.tasks import TaskError, _task_is_active

    entries: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for card in cards:
        row = live[str(card["reference"])]
        if card.get("closed") and not row.get("closed"):
            task_id = _entity_number("task", row["id"])
            entries.append((card, "closeTask", {"task_id": task_id}))
    if not entries:
        return
    owed = [int(payload["task_id"]) for _card, _method, payload in entries]
    definite_failure = False
    _set_restore_phase(client, "closure")
    try:
        answers = client.call_batch((method, payload) for _card, method, payload in entries)
        if any(answer is not True for answer in answers):
            definite_failure = True
    except TaskError as exc:
        if exc.code == "validation":
            raise _restore_payload_error(entries[0][0], "closure", exc) from None
        if refused_in_transaction(client, exc):
            raise store_refusal("card closure batch", exc) from None
        # Transport loss or an invalid aggregate answer needs an authoritative read.
    _set_restore_phase(client, "proof")
    rows = _restore_inventory(client, board_id)
    active_ids = {
        int(row["id"]) for row in rows.values() if _task_is_active(row) and str(row.get("id") or "").isdigit()
    }
    incomplete = [task_id for task_id in owed if task_id in active_ids]
    if incomplete:
        reference = next(
            str(card["reference"])
            for card in cards
            if _entity_number("task", live[str(card["reference"])]["id"]) == incomplete[0]
        )
        if definite_failure:
            raise TaskError("backend_error", f"board store rejected restored-card closure for {reference}", 1)
        raise TaskError("audit_pending", "restored-card closure is uncertain; retry is required", 4)


def commit_restored_cards(
    writer: Any,
    cards: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    *,
    request_prefix: str,
) -> None:
    """Publish one card obligation after its initialized row is authoritatively visible."""
    _set_restore_phase(writer.client, "audit")
    for card in cards:
        reference = str(card["reference"])
        request_id = f"{request_prefix}card:{reference}"
        if writer.audit.committed_event(request_id) is not None:
            continue
        event = writer.audit.pending_event(request_id)
        if event is None or event.get("kind") != "restored_bulk":
            raise RuntimeError(f"restored card has no durable obligation: {reference}")
        task_id = _entity_number("task", live[reference]["id"])
        backend = BOARD_STORE_KIND
        event["task_id"] = entity_id("task", task_id)
        event["backend"]["kind"] = backend
        event["backend"]["task_id"] = task_id
        event["backend"]["revision"] = "initialized:" + str(event["payload"]["content_sha256"])
        writer.audit.stage(request_id, event)
        try:
            writer.audit.append(request_id, event)
        except OSError:
            from ummanu.tasks import TaskError

            raise TaskError("audit_pending", "restored card is proved; audit repair is required", 4) from None


def _restore_card_identity(card: dict[str, Any], metadata: dict[str, str]) -> dict[str, Any]:
    content = {
        "title": str(card["title"]),
        "description": str(card.get("description") or ""),
        "column": str(card.get("column") or ""),
        "swimlane": str(card.get("swimlane") or ""),
        "position": int(card.get("position") or 0),
        "closed": bool(card.get("closed")),
        "metadata": metadata,
    }
    return {
        "content_sha256": hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "metadata_keys": sorted(metadata),
        "column": content["column"],
        "swimlane": content["swimlane"],
        "closed": content["closed"],
    }


def _restore_inventory(client: Any, board_id: int) -> dict[str, dict[str, Any]]:
    from ummanu.tasks import TaskError, all_project_cards

    result: dict[str, dict[str, Any]] = {}
    for row in all_project_cards(client, board_id):
        if not isinstance(row, dict) or not str(row.get("reference") or ""):
            continue
        reference = str(row["reference"])
        if reference in result:
            raise TaskError("backend_error", f"board contains duplicate reference: {reference}", 1)
        result[reference] = row
    return result


def _validate_restore_inventory(
    inventory: dict[str, dict[str, Any]], obligations: list[RestoreCardObligation]
) -> None:
    from ummanu.tasks import TaskError

    seen: set[str] = set()
    for key, row in inventory.items():
        reference = str(row.get("reference") or "")
        if reference and (reference in seen or str(key) != reference):
            raise TaskError("backend_error", f"board contains duplicate reference: {reference}", 1)
        if reference:
            seen.add(reference)
    for item in obligations:
        reference = str(item.card["reference"])
        row = inventory.get(reference)
        if row is None:
            continue
        if str(row.get("title") or "") != str(item.card["title"]) or str(row.get("description") or "") != str(
            item.card.get("description") or ""
        ):
            raise TaskError(
                "backend_error", f"existing restored reference has different content: {reference}", 1
            )


def _restore_task_id(inventory: dict[str, dict[str, Any]], item: RestoreCardObligation) -> int:
    from ummanu.tasks import TaskError, _positive_int

    reference = str(item.card["reference"])
    task_id = _positive_int(inventory.get(reference, {}).get("id"))
    if task_id is None:
        raise TaskError("backend_error", f"restored reference has an invalid backend row: {reference}", 1)
    return task_id


def _restore_placement_matches(row: dict[str, Any], item: RestoreCardObligation) -> bool:
    try:
        return (
            int(row.get("column_id") or 0) == item.column_id
            and int(row.get("swimlane_id") or 0) == item.swimlane_id
        )
    except (TypeError, ValueError):
        return False


def _metadata_holds(live: dict[str, str], expected: dict[str, str]) -> bool:
    """Whether a row holds the exported metadata, read in the store's vocabulary.

    In that vocabulary an empty value is no value: `saveTaskMetadata` clears a key it is handed
    empty, and a key only the extension bag carries then reads back absent. An export can still
    spell one as `""` (production's `personal_site-198` carries `"model": ""`, a bag the cutover
    import wrote verbatim), and comparing spellings called that card's initialization incomplete
    after the store had taken every write. Every non-empty value still has to match exactly.
    """
    return {key: value for key, value in live.items() if value} == {
        key: value for key, value in expected.items() if value
    }


def _metadata_map(answer: Any) -> dict[str, str]:
    from ummanu.tasks import TaskError

    if answer is not None and not isinstance(answer, dict):
        raise TaskError("backend_error", "board store returned invalid task metadata", 1)
    return {str(key): str(value) for key, value in (answer or {}).items()}


def restore_comments_batched(writer: Any, occurrences: list[RestoreCommentOccurrence]) -> None:
    """Restore ordered comment histories without the interactive writer read cycle.

    One wave contains at most one comment for any entity.  JSON-RPC does not
    promise execution order inside a batch, so this is the boundary that permits
    batching across entities without weakening order within one history.
    """
    from ummanu.tasks import _BATCH_CHUNK, TaskError, _now

    grouped: dict[str, list[RestoreCommentOccurrence]] = {}
    for item in occurrences:
        grouped.setdefault(item.reference, []).append(item)
    for items in grouped.values():
        # ``occurrence`` is the duplicate-body ordinal in the public audit
        # contract, not the history index.
        seen: dict[str, int] = {}
        for item in items:
            expected = seen.get(item.body, 0)
            if item.occurrence != expected:
                raise TaskError("validation", "restore comment occurrence identity is invalid", 2)
            seen[item.body] = expected + 1

    positions = {reference: 0 for reference in grouped}
    references = sorted(grouped)
    while any(positions[reference] < len(grouped[reference]) for reference in references):
        active = [reference for reference in references if positions[reference] < len(grouped[reference])]
        for start in range(0, len(active), _BATCH_CHUNK):
            chunk_refs = active[start : start + _BATCH_CHUNK]
            with ExitStack() as locks:
                for reference in chunk_refs:
                    locks.enter_context(writer.audit.marker_comment_lock(reference))
                histories = _read_comment_histories(
                    writer.client,
                    [(reference, grouped[reference][0].task_id) for reference in chunk_refs],
                )
                writes: list[tuple[str, dict[str, Any]]] = []
                staged: list[tuple[RestoreCommentOccurrence, dict[str, Any]]] = []
                next_items: list[RestoreCommentOccurrence] = []
                for reference in chunk_refs:
                    target = grouped[reference]
                    live = histories[reference]
                    target_bodies = [item.body for item in target]
                    if len(live) > len(target_bodies) or live != target_bodies[: len(live)]:
                        raise TaskError(
                            "backend_error",
                            f"restored comment history does not match normalized prefix for {reference}",
                            1,
                        )
                    # Reconcile every already-present prefix occurrence before
                    # considering a new write.  This closes lost replies and
                    # interruption after write without duplicating a sibling.
                    for index in range(positions[reference], len(live)):
                        item = target[index]
                        event = _restore_comment_event(writer, item, _now)
                        _commit_proven_comment(writer, item, event, len(live))
                    positions[reference] = len(live)
                    if positions[reference] == len(target):
                        continue
                    next_items.append(target[positions[reference]])
                owners = writer.audit.pending_marker_owners(
                    (item.reference, item.body, item.request_id) for item in next_items
                )
                for item in next_items:
                    event = _restore_comment_event(writer, item, _now)
                    if writer.audit.committed_event(
                        item.request_id
                    ) is not None or "restore_body" not in event.get("payload", {}):
                        raise TaskError(
                            "backend_error",
                            f"committed restore comment is absent from {reference}",
                            1,
                        )
                    owner = owners.get(item.request_id)
                    if owner is not None:
                        raise TaskError(
                            "audit_pending",
                            f"an earlier identical Card marker occurrence is pending; reconcile request {owner} first",
                            4,
                        )
                    writer.audit.stage(item.request_id, event)
                    arguments: dict[str, Any] = {
                        "task_id": item.task_id, "user_id": 0, "content": item.body
                    }
                    if item.recorded_at:
                        arguments["created_at"] = item.recorded_at
                    writes.append(("createComment", arguments))
                    staged.append((item, event))
                if not writes:
                    continue
                try:
                    answers: list[Any] | None = writer.client.call_batch(writes)
                except Exception as exc:  # noqa: BLE001 - short of a refusal, every answer is uncertain.
                    if refused_in_transaction(writer.client, exc):
                        # The refusal aborted the import transaction: nothing after it can be proved.
                        raise store_refusal(f"comment batch for {staged[0][0].reference}", exc) from None
                    answers = None
                if answers is None or any(
                    not isinstance(answer, int) or isinstance(answer, bool) or answer <= 0
                    for answer in answers
                ):
                    _reconcile_uncertain_chunk(writer, grouped, positions, staged)
                    raise TaskError(
                        "audit_pending",
                        "batched comment write is uncertain; pending occurrences were reconciled",
                        4,
                    )
                for item, event in staged:
                    _commit_proven_comment(writer, item, event, positions[item.reference] + 1)
                    positions[item.reference] += 1


def _read_comment_histories(client: Any, subjects: list[tuple[str, int]]) -> dict[str, list[str]]:
    """Read histories in the board's stable creation order.

    ``getAllComments`` answers in ``date_creation ASC, id ASC`` order.  The id tie-break is the durable creation
    order when several restores share the same one-second timestamp; the
    disposable backend contract canary is recorded in the recovery docs.
    """
    from ummanu.tasks import TaskError

    answers = client.call_batch(("getAllComments", {"task_id": task_id}) for _, task_id in subjects)
    result: dict[str, list[str]] = {}
    for (reference, _task_id), answer in zip(subjects, answers, strict=True):
        if not isinstance(answer, list) or any(not isinstance(value, dict) for value in answer):
            raise TaskError("backend_error", "board store returned invalid task comments", 1)
        result[reference] = [str(value.get("comment") or "") for value in answer]
    return result


def _restore_comment_event(writer: Any, item: RestoreCommentOccurrence, now: Any) -> dict[str, Any]:
    from ummanu.tasks import TaskError, _digest

    identity = {"body_sha256": _digest(item.body), "restore_occurrence": item.occurrence}
    committed = writer.audit.committed_event(item.request_id)
    if committed is not None:
        writer.audit.require_claim(
            committed, kind="restored_comment", reference=item.reference, identity=identity
        )
        return committed
    pending = writer.audit.pending_event(item.request_id)
    if pending is not None:
        writer.audit.require_claim(
            pending, kind="restored_comment", reference=item.reference, identity=identity
        )
        body = pending.get("payload", {}).get("restore_body")
        if body is not None and body != item.body:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        return pending
    kind = "sprint" if item.entity == "sprint" else "task"
    backend = BOARD_STORE_KIND
    return {
        "event_id": "evt_" + uuid.uuid4().hex,
        "schema_version": 1,
        "occurred_at": now(),
        "actor": {"role": "steward", "id": "restore"},
        "kind": "restored_comment",
        "outcome": "success",
        "task_id": entity_id(kind, item.task_id),
        "ref": item.reference,
        "backend": {"kind": backend, "task_id": item.task_id, "revision": "pending"},
        "request_id": item.request_id,
        "payload": {**identity, "restore_body": item.body},
    }


def _commit_proven_comment(
    writer: Any, item: RestoreCommentOccurrence, event: dict[str, Any], history_size: int
) -> None:
    from ummanu.tasks import TaskError

    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise TaskError("backend_error", "pending restore comment is invalid", 1)
    if writer.audit.committed_event(item.request_id) is not None:
        return
    event["backend"]["revision"] = f"comments:{history_size}"
    payload.pop("restore_body", None)
    writer.audit.stage(item.request_id, event)
    try:
        writer.audit.append(item.request_id, event)
    except OSError:
        raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None


def _reconcile_uncertain_chunk(
    writer: Any,
    grouped: dict[str, list[RestoreCommentOccurrence]],
    positions: dict[str, int],
    staged: list[tuple[RestoreCommentOccurrence, dict[str, Any]]],
) -> None:
    subjects = [(item.reference, item.task_id) for item, _event in staged]
    try:
        histories = _read_comment_histories(writer.client, subjects)
    except Exception:  # noqa: BLE001 - a failed evidence read must preserve every pending body.
        return
    for item, event in staged:
        target = grouped[item.reference]
        live = histories[item.reference]
        bodies = [value.body for value in target]
        if len(live) <= len(bodies) and live == bodies[: len(live)] and len(live) > positions[item.reference]:
            _commit_proven_comment(writer, item, event, len(live))
            positions[item.reference] += 1


def restore_card(
    writer: Any,
    reference: str,
    metadata: dict[str, str],
    target: str,
    position: int | None,
    swimlane: str,
    request_id: str | None,
) -> dict[str, Any]:
    """Apply an audited restore-only metadata and placement update."""
    from ummanu.tasks import (
        _STATE_BY_COLUMN,
        TaskError,
        _CommittedWriteError,
        _task_number,
    )

    if target not in _STATE_BY_COLUMN.values():
        raise TaskError("validation", "restore target is invalid", 2)
    if position is not None and position < 1:
        raise TaskError("validation", "restore position must be positive", 2)
    metadata = _without_retired_launch_mode(metadata)

    def mutation(task: dict[str, Any]) -> None:
        writer.client.call("saveTaskMetadata", task_id=_task_number(task), values=metadata)
        try:
            _restore_placement(writer, task, target, position, swimlane)
        except Exception as exc:
            raise _CommittedWriteError() from exc

    payload = {
        "target": target,
        "metadata_keys": sorted(metadata),
        "position": position,
        "swimlane": swimlane or None,
    }
    return writer._write(
        "restored",
        "steward",
        "restore",
        reference,
        request_id,
        payload,
        mutation,
        identity=payload,
    )


def reconcile_restore_order(
    writer: Any,
    column: str,
    swimlane: str,
    references: list[str],
    request_id: str,
) -> None:
    from ummanu.dispatch.cleanup import bulk_lane

    with bulk_lane(writer.data_dir, exclusive=True):
        _reconcile_restore_order(writer, column, swimlane, references, request_id)


def _reconcile_restore_order(
    writer: Any,
    column: str,
    swimlane: str,
    references: list[str],
    request_id: str,
) -> None:
    """Repair one active restore group under its own resumable audit boundary."""
    from ummanu.tasks import TaskError, _now, _positive_int

    if not column or not references or any(not reference for reference in references):
        raise TaskError("validation", "restore order identity is invalid", 2)
    digest = _restore_order_digest(references)
    identity = {
        "column": column,
        "swimlane": swimlane,
        "references_sha256": digest,
    }
    committed = writer.audit.committed_event(request_id)
    if committed is not None:
        writer.audit.require_claim(
            committed,
            kind="restored_order",
            reference=references[0],
            identity=identity,
        )
        if _live_restore_group(writer, column, swimlane)[0] != references:
            raise TaskError("backend_error", "committed restore order no longer matches the board", 1)
        return
    pending = writer.audit.pending_event(request_id)
    if pending is not None:
        writer.audit.require_claim(
            pending,
            kind="restored_order",
            reference=references[0],
            identity=identity,
        )
        payload = pending.get("payload")
        if not isinstance(payload, dict) or payload.get("references") != references:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        event = pending
    else:
        live, rows = _live_restore_group(writer, column, swimlane)
        if live == references:
            return
        if set(live) != set(references):
            raise TaskError("backend_error", "restore order group does not match normalized records", 1)
        task_id = _positive_int(rows[references[0]].get("id"))
        if task_id is None:
            raise TaskError("backend_error", "board store returned an invalid task", 1)
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": "steward", "id": "restore"},
            "kind": "restored_order",
            "outcome": "success",
            "task_id": entity_id("task", task_id),
            "ref": references[0],
            "backend": {"kind": BOARD_STORE_KIND, "task_id": task_id, "revision": "pending"},
            "request_id": request_id,
            "payload": {**identity, "references": references},
        }
        writer.audit.stage(request_id, event)
    try:
        finish_pending_restore_order(writer, event)
        event["backend"]["revision"] = f"order:{digest}"
        writer.audit.stage(request_id, event)
        writer.audit.append(request_id, event)
    except TaskError as exc:
        raise TaskError("audit_pending", f"restore order repair is pending: {exc.message}", 4) from None
    except (OSError, KeyError, TypeError, ValueError):
        raise TaskError("audit_pending", "restore order repair is pending", 4) from None


def finish_pending_restore_order(writer: Any, event: dict[str, Any]) -> None:
    """Resume a staged group, proving every uncertain move from live state."""
    from ummanu.tasks import TaskError, _positive_int

    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise TaskError("backend_error", "pending restore order is invalid", 1)
    column = payload.get("column")
    swimlane = payload.get("swimlane")
    references = payload.get("references")
    if (
        not isinstance(column, str)
        or not column
        or not isinstance(swimlane, str)
        or not isinstance(references, list)
        or not references
        or any(not isinstance(reference, str) or not reference for reference in references)
        or payload.get("references_sha256") != _restore_order_digest(references)
    ):
        raise TaskError("backend_error", "pending restore order is invalid", 1)

    from ummanu.dispatch.cleanup import bulk_lane

    with bulk_lane(writer.data_dir, exclusive=True):
        live, rows = _live_restore_group(writer, column, swimlane)
        while live != references:
            if set(live) != set(references):
                raise TaskError("backend_error", "restore order group does not match normalized records", 1)
            mismatch = next(index for index, reference in enumerate(references) if live[index] != reference)
            reference = references[mismatch]
            task_id = _positive_int(rows[reference].get("id"))
            if task_id is None:
                raise TaskError("backend_error", "board store returned an invalid task", 1)
            board_id, columns, swimlanes = writer.reader._board()
            column_id = next((identifier for identifier, title in columns.items() if title == column), None)
            swimlane_id = next(
                (identifier for identifier, name in swimlanes.items() if name == swimlane),
                0,
            )
            if column_id is None or (swimlane and not swimlane_id):
                raise TaskError("backend_error", "restored order group is unavailable", 1)
            try:
                answer = writer.client.call(
                    "moveTaskPosition",
                    project_id=board_id,
                    task_id=task_id,
                    column_id=column_id,
                    position=mismatch + 1,
                    swimlane_id=swimlane_id,
                )
            except Exception as exc:  # noqa: BLE001 - a lost move reply is reconciled below.
                if refused_in_transaction(writer.client, exc):
                    raise store_refusal(f"order move for {reference}", exc) from None
                answer = None
            if answer is True:
                live.remove(reference)
                live.insert(mismatch, reference)
                continue
            live, rows = _live_restore_group(writer, column, swimlane)
            if live[: mismatch + 1] != references[: mismatch + 1]:
                raise TaskError("backend_error", "board store move result is uncertain", 1)
        proven, _ = _live_restore_group(writer, column, swimlane)
        if proven != references:
            raise TaskError("backend_error", "restored order repair could not be verified", 1)


def _live_restore_group(
    writer: Any, column: str, swimlane: str
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Read one active group directly from the authoritative board rows.

    The restore creates every row before it closes any archived row, so each
    placement is made while the group is dense and a later close can leave
    holes, but cannot create an active-position tie. Reference is therefore
    only the canonical deterministic tie-breaker for legacy/raw anomalies. If
    restore timing ever changes and a backend tie has a different visible
    order, the final authoritative proof and parity gate disagree and fail
    closed instead of publishing this repair.
    """
    from ummanu.tasks import TaskError, _positive_int, _task_is_active, all_project_cards

    board_id, columns, swimlanes = writer.reader._board()
    column_id = next((identifier for identifier, title in columns.items() if title == column), None)
    swimlane_id = next((identifier for identifier, name in swimlanes.items() if name == swimlane), 0)
    if column_id is None or (swimlane and not swimlane_id):
        raise TaskError("backend_error", "restored order group is unavailable", 1)
    rows: dict[str, dict[str, Any]] = {}
    for row in all_project_cards(writer.client, board_id):
        if (
            not isinstance(row, dict)
            or not _task_is_active(row)
            or _positive_int(row.get("column_id")) != column_id
            or (_positive_int(row.get("swimlane_id")) or 0) != swimlane_id
        ):
            continue
        reference = row.get("reference")
        if not isinstance(reference, str) or not reference or reference in rows:
            raise TaskError("backend_error", "restore order group has an invalid reference", 1)
        rows[reference] = row
    ordered = sorted(
        rows,
        key=lambda reference: (_positive_int(rows[reference].get("position")) or 0, reference),
    )
    return ordered, rows


def _restore_order_digest(references: list[str]) -> str:
    encoded = json.dumps(references, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _without_retired_launch_mode(metadata: dict[str, str]) -> dict[str, str]:
    """The restore payload with no retired Codex launch mode left in it.

    This is the write boundary, so it is where the retirement has to be enforced: everything below
    hands `metadata` to `saveTaskMetadata` verbatim, and a checkpoint taken before the TUI-only
    rule still carries `codex_launch_mode=exec` in its raw export. Normalized readers already hide
    such a value, but hiding is not removing — restoring it would put a launch shape the product
    no longer has back onto a live board, where the next export would carry it forward again.

    The key is cleared rather than dropped: `saveTaskMetadata` is a partial update, so dropping it
    would leave a retired value already on the card exactly where it was. Every mode the product
    still has passes through untouched.
    """
    from ummanu.tasks import _CODEX_LAUNCH_MODES

    sanitized = dict(metadata)
    if (
        "codex_launch_mode" in sanitized
        and str(sanitized["codex_launch_mode"] or "") not in _CODEX_LAUNCH_MODES
    ):
        sanitized["codex_launch_mode"] = ""
    return sanitized


def restore_comment(
    writer: Any, reference: str, body: str, occurrence: int, request_id: str | None
) -> dict[str, Any]:
    """Append one comment and retain pending state when its reply is lost."""
    from ummanu.tasks import TaskError, _CommittedWriteError, _digest, _task_number

    payload: dict[str, Any] = {
        "body_sha256": _digest(body),
        "restore_occurrence": occurrence,
        "restore_body": body,
    }

    def mutation(task: dict[str, Any]) -> None:
        try:
            writer.client.call("createComment", task_id=_task_number(task), user_id=0, content=body)
        except TaskError as exc:
            if exc.code == "backend_unavailable":
                raise _CommittedWriteError() from exc
            raise
        payload.pop("restore_body", None)

    # `restore_body` is dropped from the payload once the comment is known to be on the card, so
    # it is not part of what the id claims; the body digest and the occurrence are.
    identity = {"body_sha256": payload["body_sha256"], "restore_occurrence": occurrence}
    with writer.audit.marker_comment_lock(reference):
        owner = writer.audit.pending_marker_owner(reference, body, request_id=request_id)
        if owner is not None:
            raise TaskError(
                "audit_pending",
                f"an earlier identical Card marker occurrence is pending; reconcile request {owner} first",
                4,
            )
        return writer._write(
            "restored_comment",
            "steward",
            "restore",
            reference,
            request_id,
            payload,
            mutation,
            identity=identity,
        )


def finish_pending_restore(writer: Any, event: dict[str, Any], payload: dict[str, Any]) -> None:
    """Finish a restore whose metadata committed before its placement failed."""
    from ummanu.tasks import _STATE_BY_COLUMN, TaskError

    target = payload.get("target")
    if target not in _STATE_BY_COLUMN.values():
        raise TaskError("backend_error", "pending restore is missing its target state", 1)
    ref = str(event.get("ref") or "")
    if not ref:
        raise TaskError("backend_error", "pending restore is missing its task ref", 1)
    task = writer.reader.show(ref)
    position = payload.get("position")
    if not isinstance(position, int) or position < 1:
        position = None
    swimlane = payload.get("swimlane")
    if not isinstance(swimlane, str):
        swimlane = ""
    _restore_placement(writer, task, target, position, swimlane)
    normalized = writer.reader.show(ref)
    if (
        normalized["state"] != target
        or (position is not None and normalized["position"] != position)
        or (not swimlane and normalized.get("extensions", {}).get(EXTENSION_BAG, {}).get("swimlane") is not None)
        or (swimlane and normalized.get("extensions", {}).get(EXTENSION_BAG, {}).get("swimlane") != swimlane)
    ):
        raise TaskError("backend_error", "pending restore cleanup remains incomplete", 1)


def finish_pending_restore_comment(writer: Any, event: dict[str, Any], payload: dict[str, Any]) -> None:
    """Prove or finish one ambiguous Card/Sprint comment before audit append."""
    from ummanu.tasks import TaskError, _digest, _positive_int

    ref = str(event.get("ref") or "")
    body = payload.get("restore_body")
    digest = payload.get("body_sha256")
    occurrence = payload.get("restore_occurrence")
    backend = event.get("backend") if isinstance(event.get("backend"), dict) else {}
    task_id = _positive_int(backend.get("task_id"))
    if (
        not ref
        or task_id is None
        or (body is not None and not isinstance(body, str))
        or not isinstance(digest, str)
        or not isinstance(occurrence, int)
        or occurrence < 0
    ):
        raise TaskError("backend_error", "pending restore comment is invalid", 1)
    with writer.audit.marker_comment_lock(ref):
        if isinstance(body, str):
            owner = writer.audit.pending_marker_owner(
                ref, body, request_id=str(event.get("request_id") or "")
            )
            if owner is not None:
                raise TaskError(
                    "audit_pending",
                    "an earlier identical Card/Sprint marker occurrence is pending; "
                    f"reconcile request {owner} first",
                    4,
                )

        def matching_count() -> int:
            answers = writer.client.call_batch([("getAllComments", {"task_id": task_id})])
            comments = answers[0]
            if not isinstance(comments, list) or any(not isinstance(comment, dict) for comment in comments):
                raise TaskError("backend_error", "board store returned invalid task comments", 1)
            return sum(_digest(str(comment.get("comment") or "")) == digest for comment in comments)

        matches = matching_count()
        if matches <= occurrence:
            if body is None:
                raise TaskError(
                    "audit_pending",
                    "legacy Sprint restore comment is not present and cannot be retried without its body",
                    4,
                )
            answer = writer.client.call("createComment", task_id=task_id, user_id=0, content=body)
            if not isinstance(answer, int) or isinstance(answer, bool) or answer <= 0:
                raise TaskError("backend_error", "board store rejected the restored comment", 1)
            matches = matching_count()
        if matches <= occurrence:
            raise TaskError("backend_error", "pending restore comment remains incomplete", 1)
        event["backend"]["revision"] = f"comments:{matches}"
        payload.pop("restore_body", None)


def _restore_placement(
    writer: Any, task: dict[str, Any], target: str, position: int | None, swimlane: str
) -> None:
    from ummanu.tasks import (
        TaskError,
        _nonnegative_int,
        _positive_int,
        project_card_by_reference,
    )

    board_id, _, swimlanes = writer.reader._board()
    raw = project_card_by_reference(writer.client, board_id, task["ref"])
    if not isinstance(raw, dict):
        raise TaskError("not_found", "task was not found", 2)
    swimlane_id = 0
    if swimlane:
        swimlane_id = next((identifier for identifier, name in swimlanes.items() if name == swimlane), 0)
        if not swimlane_id:
            raise TaskError("backend_error", "restored swimlane is unavailable", 1)
    raw_position = _nonnegative_int(raw.get("position"))
    if (
        task["state"] != target
        or (position is not None and raw_position != position)
        or (_positive_int(raw.get("swimlane_id")) != swimlane_id)
    ):
        writer._move_raw(task, target, position=position or 1, swimlane_id=swimlane_id)
