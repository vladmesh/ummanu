"""Audited repair of references duplicated by the released row-id allocator."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ummanu.board.backend import BOARD_STORE_KIND, entity_id
from ummanu.dispatch.cleanup import bulk_lane
from ummanu.runtime.references import next_reference, reference_allocation_lock
from ummanu.tasks import (
    _STATE_BY_COLUMN,
    ACTIVE_STATES,
    TaskError,
    TaskWriter,
    _positive_int,
    _task_is_active,
    _task_metadata,
    _text,
    admit_role,
    all_project_cards,
)

REPAIR_KIND = "reference_repaired"
PRODUCER_FIX = "d9e872ba4a3166486b9282611ba54699f9dd7a66"
_DEPENDENT_TASK_FIELDS = ("blocked_by", "supersedes")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _request_id(batch_request_id: str, task_id: int) -> str:
    return "reference-repair-" + hashlib.sha256(f"{batch_request_id}:{task_id}".encode()).hexdigest()


def _summary(title: Any) -> str:
    # Titles already crossed the task protocol's board redaction boundary. Keep preview bounded
    # and single-line; descriptions and comments never enter it.
    clean = " ".join(_text(title).split())
    return clean[:77] + "..." if len(clean) > 80 else clean


def _created_evidence(writer: TaskWriter, reference: str, task_id: int) -> dict[str, str] | None:
    for event in writer.audit.events(reference, kind="created"):
        backend = event.get("backend") if isinstance(event.get("backend"), dict) else {}
        if _positive_int(backend.get("task_id")) == task_id:
            event_id = _text(event.get("event_id"))
            occurred_at = _text(event.get("occurred_at"))
            if event_id and occurred_at:
                return {"event_id": event_id, "occurred_at": occurred_at}
    return None


def _companion_conflicts(writer: TaskWriter, rows: list[dict[str, Any]], refs: set[str]) -> list[str]:
    conflicts: list[str] = []
    for row in rows:
        task_id = _positive_int(row.get("id"))
        if task_id is None:
            continue
        meta = _task_metadata(writer.client.call("getTaskMetadata", task_id=task_id))
        for field in _DEPENDENT_TASK_FIELDS:
            if meta.get(field) in refs:
                conflicts.append(f"task metadata {field} on backend ID {task_id}")
    # These are the reference-keyed current run-state companions. Historical runs.ndjson is
    # append-only evidence and is disambiguated by the repair event's creation boundary.
    for name in ("cards.json", "claims.json"):
        path = writer.data_dir / "runs" / name
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            conflicts.append(f"unreadable run-state companion {name}")
            continue
        if any(ref in _strings(value) for ref in refs):
            conflicts.append(f"run-state companion {name}")
    project = writer.client.call("getProjectByName", name="Ummanu sprints")
    if isinstance(project, dict) and _positive_int(project.get("id")) is not None:
        sprint_rows = all_project_cards(writer.client, int(project["id"]))
        for row in sprint_rows:
            task_id = _positive_int(row.get("id"))
            if task_id is None:
                continue
            meta = _task_metadata(writer.client.call("getTaskMetadata", task_id=task_id))
            if meta.get("sprint_current_task") in refs:
                conflicts.append(f"sprint current_task on backend ID {task_id}")
    return sorted(set(conflicts))


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {item for child in value.values() for item in _strings(child)}
    if isinstance(value, list):
        return {item for child in value for item in _strings(child)}
    return set()


def preview_reference_repair(writer: TaskWriter, *, _allow_repair_pending: bool = False) -> dict[str, Any]:
    pending_events = writer.audit.pending_events()
    if pending_events and not (
        _allow_repair_pending and all(event.get("kind") == REPAIR_KIND for event in pending_events)
    ):
        raise TaskError("audit_pending", "reference repair requires a clean task audit", 4)
    board_id, columns, _ = writer.reader._board()
    rows = all_project_cards(writer.client, board_id)
    by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ref = _text(row.get("reference"))
        if ref:
            by_ref[ref].append(row)
    duplicate_refs = {ref for ref, group in by_ref.items() if len(group) > 1}
    metadata: dict[int, dict[str, str]] = {}
    for row in rows:
        task_id = _positive_int(row.get("id"))
        if task_id is not None:
            metadata[task_id] = _task_metadata(writer.client.call("getTaskMetadata", task_id=task_id))
    assigned_rows = [dict(row) for row in rows]
    duplicates: list[dict[str, Any]] = []
    for reference in sorted(duplicate_refs):
        group = sorted(by_ref[reference], key=lambda row: _positive_int(row.get("id")) or 0)
        match = re.fullmatch(r"(.+)-(\d+)", reference)
        suffix = int(match.group(2)) if match else None
        producer_rows = [row for row in group if _positive_int(row.get("id")) == suffix]
        reasons: list[str] = []
        if len(group) != 2 or len(producer_rows) != 1:
            reasons.append("duplicate does not have one released row-id allocator shape")
        producer = producer_rows[0] if len(producer_rows) == 1 else None
        retained = next((row for row in group if row is not producer), None) if producer else None
        ids = [_positive_int(row.get("id")) for row in group]
        if any(task_id is None for task_id in ids):
            reasons.append("backend identity is missing")
        types = []
        for task_id in ids:
            meta = metadata.get(task_id or -1, {})
            record_type = meta.get("record_type") or "task"
            types.append(record_type)
            if record_type != "task":
                reasons.append("mixed or non-task record types")
            if not meta.get("project") or not meta.get("task_type"):
                reasons.append("required task metadata is missing")
        if len(set(types)) != 1:
            reasons.append("mixed record types")
        for row in group:
            task_id = _positive_int(row.get("id")) or -1
            column = columns.get(_positive_int(row.get("column_id")) or -1, "")
            state = _STATE_BY_COLUMN.get(column)
            if _task_is_active(row) and (
                state is None or state in ACTIVE_STATES or metadata.get(task_id, {}).get("claim")
            ):
                reasons.append("duplicate contains active work")
        evidence = _created_evidence(writer, reference, suffix) if suffix is not None else None
        if producer is not None and retained is not None:
            if evidence is None:
                reasons.append("collision row has no exact creation-audit binding")
            producer_created = _positive_int(producer.get("date_creation"))
            retained_created = _positive_int(retained.get("date_creation"))
            if producer_created is None or retained_created is None or retained_created >= producer_created:
                reasons.append("retained row is not demonstrably older")
        target_ref = ""
        if producer is not None:
            project = metadata.get(suffix or -1, {}).get("project", "")
            if project:
                target_ref = next_reference(assigned_rows, f"{project}-")
                assigned_rows.append({"reference": target_ref})
        records = []
        for row in group:
            task_id = _positive_int(row.get("id")) or 0
            meta = metadata.get(task_id, {})
            column = columns.get(_positive_int(row.get("column_id")) or -1, "unknown")
            records.append(
                {
                    "backend_id": task_id,
                    "record_type": meta.get("record_type") or "task",
                    "state": column,
                    "closed": not _task_is_active(row),
                    "title_summary": _summary(row.get("title")),
                }
            )
        duplicates.append(
            {
                "reference": reference,
                "records": records,
                "retain_backend_id": _positive_int(retained.get("id")) if retained else None,
                "reassign_backend_id": _positive_int(producer.get("id")) if producer else None,
                "new_reference": target_ref,
                "evidence": {
                    "rule": "retain the older owner; reassign the later row created by the released backend-row-id allocator",
                    "producer_fixed_by": PRODUCER_FIX,
                    "creation_audit": evidence,
                    "retained_created_at": _text(retained.get("date_creation")) if retained else "",
                    "collision_created_at": _text(producer.get("date_creation")) if producer else "",
                },
                "applicable": not reasons,
                "refusals": sorted(set(reasons)),
            }
        )
    conflicts = _companion_conflicts(writer, rows, duplicate_refs) if duplicate_refs else []
    if conflicts:
        for duplicate in duplicates:
            duplicate["applicable"] = False
            duplicate["refusals"] = sorted(set([*duplicate["refusals"], *conflicts]))
    identity = {
        "schema": "ummanu.reference-repair-plan",
        "version": 1,
        "rows": [
            {
                "id": _positive_int(row.get("id")),
                "reference": _text(row.get("reference")),
                "active": _task_is_active(row),
                "modified": _text(row.get("date_modification")),
            }
            for row in sorted(rows, key=lambda row: _positive_int(row.get("id")) or 0)
        ],
        "duplicates": duplicates,
    }
    return {**identity, "plan_id": _digest(identity), "duplicate_count": len(duplicates)}


def apply_reference_repair(
    writer: TaskWriter,
    *,
    plan_id: str,
    task_ids: list[int],
    reason: str,
    request_id: str,
    actor: str = "operator",
    role: str = "po",
) -> dict[str, Any]:
    admit_role(role, actor, {"po"})
    if not plan_id or not request_id or not reason.strip():
        raise TaskError("validation", "apply requires plan id, request id and non-empty reason", 2)
    safe_reason = writer._redact_for_board(reason.strip())
    if safe_reason != reason.strip():
        raise TaskError("validation", "repair reason contains credential material", 2)
    if len(task_ids) != len(set(task_ids)):
        raise TaskError("validation", "apply backend IDs must be unique", 2)
    with bulk_lane(writer.data_dir, exclusive=True), reference_allocation_lock(writer.data_dir):
        pending = [writer.audit.event(_request_id(request_id, task_id)) for task_id in task_ids]
        existing = [event for event in pending if event is not None]
        if existing and len(existing) == len(task_ids):
            events = existing
            if any(
                event.get("payload", {}).get("plan_id") != plan_id
                or event.get("payload", {}).get("reason") != safe_reason
                or event.get("actor", {}).get("id") != actor
                for event in events
            ):
                raise TaskError("validation", "request id belongs to another repair plan", 2)
        else:
            plan = preview_reference_repair(writer, _allow_repair_pending=bool(existing))
            if plan["plan_id"] != plan_id:
                raise TaskError("stale_plan", "reference repair plan is stale; preview again", 3)
            proposed = [item for item in plan["duplicates"] if item["applicable"]]
            if len(proposed) != len(plan["duplicates"]):
                raise TaskError("repair_refused", "reference repair plan contains unresolved evidence", 3)
            expected = sorted(int(item["reassign_backend_id"]) for item in proposed)
            if sorted(task_ids) != expected or len(task_ids) != len(set(task_ids)):
                raise TaskError("validation", "apply must select every proposed backend ID exactly once", 2)
            by_task_id = {int(event.get("backend", {}).get("task_id")): event for event in existing}
            events = []
            for item in proposed:
                task_id = int(item["reassign_backend_id"])
                rid = _request_id(request_id, task_id)
                event = by_task_id.get(task_id) or {
                    "event_id": "evt_" + uuid.uuid4().hex,
                    "schema_version": 1,
                    "occurred_at": datetime.now(UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "actor": {"role": role, "id": actor},
                    "kind": REPAIR_KIND,
                    "outcome": "success",
                    "task_id": entity_id("task", task_id),
                    "ref": item["new_reference"],
                    "backend": {"kind": BOARD_STORE_KIND, "task_id": task_id, "revision": "pending"},
                    "request_id": rid,
                    "payload": {
                        "plan_id": plan_id,
                        "old_reference": item["reference"],
                        "new_reference": item["new_reference"],
                        "retain_backend_id": item["retain_backend_id"],
                        "reason": safe_reason,
                        "producer_fix": PRODUCER_FIX,
                        "history_boundary_event_id": item["evidence"]["creation_audit"]["event_id"],
                    },
                }
                writer.audit.stage(rid, event)
                events.append(event)
        repaired = []
        for event in events:
            if writer.audit.committed_event(str(event["request_id"])) is not None:
                repaired.append({"backend_id": event["backend"]["task_id"], "reference": event["ref"]})
                continue
            finish_pending_reference_repair(writer, event, _allocation_locked=True)
            writer.audit.stage(str(event["request_id"]), event)
            writer.audit.append(str(event["request_id"]), event)
            repaired.append({"backend_id": event["backend"]["task_id"], "reference": event["ref"]})
        return {"action": "reference_repaired", "plan_id": plan_id, "repaired": repaired}


def finish_pending_reference_repair(
    writer: TaskWriter, event: dict[str, Any], *, _allocation_locked: bool = False
) -> None:
    with bulk_lane(writer.data_dir, exclusive=True):
        if _allocation_locked:
            _finish_pending_reference_repair_locked(writer, event)
            return
        with reference_allocation_lock(writer.data_dir):
            _finish_pending_reference_repair_locked(writer, event)


def _finish_pending_reference_repair_locked(writer: TaskWriter, event: dict[str, Any]) -> None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    task_id = _positive_int(event.get("backend", {}).get("task_id"))
    old_ref, new_ref = _text(payload.get("old_reference")), _text(payload.get("new_reference"))
    if task_id is None or not old_ref or not new_ref:
        raise TaskError("backend_error", "pending reference repair is incomplete", 1)
    board_id, _, _ = writer.reader._board()
    rows = all_project_cards(writer.client, board_id)
    target = next((row for row in rows if _positive_int(row.get("id")) == task_id), None)
    if target is None or _text(target.get("reference")) not in {old_ref, new_ref}:
        raise TaskError("backend_error", "pending reference repair target changed", 1)
    claimants = [
        row
        for row in rows
        if _text(row.get("reference")) == new_ref and _positive_int(row.get("id")) != task_id
    ]
    if claimants:
        if _text(target.get("reference")) != old_ref:
            raise TaskError("validation", f"target reference {new_ref} is already claimed", 2)
        meta = _task_metadata(writer.client.call("getTaskMetadata", task_id=task_id))
        project = meta.get("project", "")
        if not project:
            raise TaskError("backend_error", "pending reference repair target metadata is incomplete", 1)
        replacement = next_reference(rows, f"{project}-")
        superseded = payload.get("superseded_allocations")
        history = list(superseded) if isinstance(superseded, list) else []
        history.append(new_ref)
        payload["new_reference"] = replacement
        payload["superseded_allocations"] = history
        event["ref"] = replacement
        writer.audit.stage(str(event["request_id"]), event)
        new_ref = replacement
    if _text(target.get("reference")) == old_ref and not writer.client.call(
        "updateTask", id=task_id, reference=new_ref
    ):
        raise TaskError("backend_error", "reference repair write was rejected", 1)
    provenance = json.dumps(
        {
            "version": 1,
            "old_reference": old_ref,
            "new_reference": new_ref,
            "plan_id": payload.get("plan_id"),
            "reason": payload.get("reason"),
            "history_boundary_event_id": payload.get("history_boundary_event_id"),
            "superseded_allocations": payload.get("superseded_allocations", []),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    writer.client.call("saveTaskMetadata", task_id=task_id, values={"reference_repair": provenance})
    rows = all_project_cards(writer.client, board_id)
    target = next((row for row in rows if _positive_int(row.get("id")) == task_id), None)
    meta = _task_metadata(writer.client.call("getTaskMetadata", task_id=task_id))
    if (
        target is None
        or _text(target.get("reference")) != new_ref
        or meta.get("reference_repair") != provenance
    ):
        raise TaskError("backend_error", "reference repair could not be verified", 1)
    event["backend"]["revision"] = _text(target.get("date_modification")) or "verified"
