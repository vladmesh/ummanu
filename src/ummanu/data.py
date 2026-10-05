from __future__ import annotations

import json
import os
import shutil
import stat as stat_module
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
)
from ummanu._fsutil import (
    copy_tree as _copy_tree,
)
from ummanu._fsutil import (
    display_relative as _display_relative,
)
from ummanu._fsutil import (
    ensure_dir as _ensure_dir,
)
from ummanu._fsutil import ndjson_lines
from ummanu._fsutil import (
    publish_component_entries as _publish_component_entries,
)
from ummanu._fsutil import (
    regular_files_under as _regular_files_under,
)
from ummanu._fsutil import (
    write_json as _write_json,
)
from ummanu._fsutil import (
    write_ndjson as _write_ndjson,
)
from ummanu.board.backend import CARD, SPRINT, board_client
from ummanu.board.owner_decisions import stored_decisions
from ummanu.board.local_run import parse_local_run_exceptions
from ummanu.config import validate
from ummanu.memory_journal import export_memory_snapshot
from ummanu.tasks import TaskError, TaskReader, task_audit_for

LAYOUT_DIRS = ("board", "memory", "runs", "transcripts", "artifacts", "backups")
PIPELINE_WORKTREE = Path.home() / "orca" / "workspaces" / "ummanu" / "pipeline"
ORCA_WORKSPACES_ROOT = Path.home() / "orca" / "workspaces"
PIPELINE_STATE_DIR = PIPELINE_WORKTREE / "state" / "pipeline"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class DataLayout:
    data_dir: Path
    manifest_path: Path
    created_dirs: list[Path]


@dataclass(frozen=True)
class DataExport:
    path: Path
    count: int
    source: str


def manifest_for(data_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    return {
        "version": 1,
        "data_dir": str(data_dir),
        "components": {
            "board": {"path": "board"},
            "memory": {
                "path": "memory",
                "facts": "state/memory/facts",
                "export": "memory/export.ndjson",
                "index": "memory/index.sqlite",
            },
            "runs": {"path": "runs"},
            "transcripts": {"path": "transcripts"},
            "artifacts": {"path": "artifacts"},
            "backups": {"path": "backups"},
        },
    }


def init_layout(data_dir: Path) -> DataLayout:
    data_dir = data_dir.expanduser().resolve()
    created_dirs: list[Path] = []
    try:
        for relative in LAYOUT_DIRS:
            directory = data_dir / relative
            existed = directory.is_dir()
            directory.mkdir(parents=True, exist_ok=True)
            if not existed:
                created_dirs.append(directory)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare ummanu-data layout: {exc}") from None

    manifest_path = data_dir / "data-manifest.json"
    _write_data_manifest(manifest_path, manifest_for(data_dir))
    return DataLayout(
        data_dir=data_dir,
        manifest_path=manifest_path,
        created_dirs=created_dirs,
    )


def export_board(
    data_dir: Path,
    *,
    instance_dir: Path,
    reader: TaskReader | None = None,
    sprint_client: Any = None,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    board_dir = data_dir / "board"
    _ensure_dir(board_dir, "board data dir")
    try:
        task_reader = (
            reader if reader is not None else TaskReader(board_client(instance_dir, serves=(CARD,)))
        )
        task_client = getattr(task_reader, "client", None)
        audit_owner = task_audit_for(task_client, data_dir)
        audit = audit_owner.status()
        if not audit["ok"]:
            raise RuntimeError(
                f"board export blocked by {audit['pending']} unresolved pending audit record(s)"
            )
        cards = task_reader.export()
        history = audit_owner.events()
    except TaskError as exc:
        raise RuntimeError(f"ummanu task export failed: {exc.message}") from None
    if not isinstance(cards, list):
        raise RuntimeError("ummanu task export did not return a card list")

    normalized = []
    for card in sorted(cards, key=lambda item: str(item.get("reference", ""))):
        if not isinstance(card, dict):
            raise RuntimeError("ummanu task export returned an invalid card")
        if not str(card.get("reference") or ""):
            continue
        normalized.append(normalize_board_card(card, card))

    # Sprint entities live on their own board and never reach the task board export, so the
    # checkpoint reads them separately instead of inferring them from linked cards.
    owned_sprint_client = None
    if sprint_client is None:
        from ummanu.sprints import sprint_client as resolve_sprint_client

        owned_sprint_client = resolve_sprint_client(instance_dir)
        sprint_client = owned_sprint_client
    try:
        sprints = export_sprint_entities(instance_dir, sprint_client)
    finally:
        if owned_sprint_client is not None:
            owned_sprint_client.connection.close()

    summary = {
        "version": 1,
        "source": "ummanu task",
        "card_count": len(normalized),
        "sprint_count": len(sprints),
    }
    try:
        staging = Path(tempfile.mkdtemp(prefix=".board-export-", suffix=".tmp", dir=board_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create board export staging: {exc}") from None
    try:
        _write_json(staging / "cards.json", {"version": 1, "cards": normalized})
        _write_ndjson(staging / "cards.ndjson", normalized)
        _write_json(staging / "sprints.json", {"version": 1, "sprints": sprints})
        _write_ndjson(staging / "sprints.ndjson", sprints)
        _write_json(staging / "audit.json", {"version": 1, "events": history})
        _write_ndjson(staging / "audit.ndjson", history)
        _write_json(staging / "export.json", summary)
        # Validate the exact pair restore consumes before replacing the last good live export.
        from ummanu.board.normalized_checkpoint import NormalizedBoardError, validated_normalized_cards

        try:
            validated_normalized_cards(staging)
        except NormalizedBoardError as exc:
            raise RuntimeError(f"board export is not restorable: {exc}") from None
        _publish_component_entries(
            staging,
            board_dir,
            [
                "cards.json", "cards.ndjson", "sprints.json", "sprints.ndjson",
                "audit.json", "audit.ndjson", "export.json",
            ],
            "board export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    if reader is None:
        task_client.connection.close()
    return DataExport(path=board_dir / "cards.json", count=len(normalized), source=summary["source"])


def normalize_board_card(list_card: dict[str, Any], shown_card: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint record for one card from its task export projection or list/show pair."""
    metadata = shown_card.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    comments = shown_card.get("comments")
    if not isinstance(comments, list):
        comments = []
    return {
        "id": _int_or_none(shown_card.get("id", list_card.get("id"))),
        "reference": str(shown_card.get("reference") or list_card.get("reference") or ""),
        "title": str(shown_card.get("title") or list_card.get("title") or ""),
        "description": str(shown_card.get("description") or ""),
        "swimlane": str(list_card.get("swimlane") or ""),
        "column": str(shown_card.get("column") or list_card.get("column") or ""),
        "position": _int_or_none(list_card.get("position")) or 0,
        "date_moved": _int_or_none(list_card.get("date_moved")),
        "closed": bool(shown_card.get("closed", list_card.get("closed", False))),
        "metadata": {str(k): str(v) for k, v in sorted(metadata.items())},
        "fields": {
            "task_type": str(shown_card.get("task_type") or list_card.get("task_type") or ""),
            "project": str(shown_card.get("project") or list_card.get("project") or ""),
            "blocked_by": str(shown_card.get("blocked_by") or list_card.get("blocked_by") or ""),
            "head": str(shown_card.get("head") or list_card.get("head") or ""),
            "effective_head": str(shown_card.get("effective_head") or list_card.get("effective_head") or ""),
            "review_head": str(shown_card.get("review_head") or list_card.get("review_head") or ""),
            "effective_review_head": str(
                shown_card.get("effective_review_head") or list_card.get("effective_review_head") or ""
            ),
            "claim": str(shown_card.get("claim") or list_card.get("claim") or ""),
            "slug": str(shown_card.get("slug") or list_card.get("slug") or ""),
            "base_branch": str(shown_card.get("base_branch") or list_card.get("base_branch") or ""),
        },
        "comments": [
            {
                "ts": str(comment.get("ts", "")),
                "text": str(comment.get("text", "")),
            }
            for comment in comments
            if isinstance(comment, dict)
        ],
    }


def export_sprint_entities(instance_dir: Path, client: Any = None) -> list[dict[str, Any]]:
    """Read the sprint board into deterministic checkpoint records."""
    from ummanu.sprints import SprintReader
    from ummanu.tasks import TaskError

    try:
        sprint_board = client if client is not None else board_client(instance_dir, serves=(SPRINT,))
        reader = SprintReader(sprint_board)
        return [normalize_sprint_entity(sprint) for sprint in reader.export()]
    except TaskError as exc:
        raise RuntimeError(f"sprint export failed: {exc.message}") from None


def normalize_sprint_entity(sprint: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint record for one sprint entity.

    The record describes the contract, not the board row it currently sits on:
    a restored sprint gets a new task id, and comparing it back to the export has
    to stay possible. Budget totals and thresholds are left out; they are derived
    from `by_type` and from installation config.
    """
    audit = sprint.get("audit")
    audit = audit if isinstance(audit, dict) else {}
    budget = sprint.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    by_type = budget.get("by_type")
    by_type = by_type if isinstance(by_type, dict) else {}
    # The uncharged counts are carried only where a sprint has any, so a record of a sprint that
    # never had one stays byte-identical to the record this export always wrote.
    uncharged = budget.get("uncharged")
    uncharged = (
        {
            str(key): _int_or_none(value) or 0
            for key, value in sorted(uncharged.items())
            if _int_or_none(value)
        }
        if isinstance(uncharged, dict)
        else {}
    )
    resume = sprint.get("resume")
    comments = sprint.get("comments")
    local_run_exceptions = [
        entry.to_document() for entry in parse_local_run_exceptions(
            sprint.get("local_run_exceptions", []), projects=sprint.get("reservations", [])
        )
    ]
    return {
        "reference": str(sprint.get("ref") or ""),
        "goal": str(sprint.get("goal") or ""),
        "definition_of_done": str(sprint.get("definition_of_done") or ""),
        "repositories": [str(repo) for repo in sprint.get("repositories") or []],
        # A sprint that predates ownership has none of the three fields, and the record
        # keeps them absent rather than storing an empty value it was never given.
        **({"product": str(sprint["product"])} if "product" in sprint else {}),
        **({"issues": [str(issue) for issue in sprint["issues"] or []]} if "issues" in sprint else {}),
        **(
            {"reservations": [str(project) for project in sprint["reservations"] or []]}
            if "reservations" in sprint
            else {}
        ),
        # A key present and `None` is a value that is not one of the four tagged forms: restore
        # refuses the whole set on it rather than guessing a repair.
        **({"observer": sprint["observer"]} if "observer" in sprint else {}),
        # The two optional executor pins, carried only where the row declares one, so a record of
        # a sprint that pins nobody stays byte-identical to the record this export always wrote.
        **_sprint_executors(sprint),
        # Carried only where the sprint has them (0016), so a record of a sprint opened before
        # them stays byte-identical to the record this export always wrote.
        **({"po_session": str(sprint["po_session"])} if sprint.get("po_session") else {}),
        **({"local_run_exceptions": local_run_exceptions} if local_run_exceptions else {}),
        **({"owner_decisions": stored_decisions(sprint["owner_decisions"])} if sprint.get("owner_decisions") else {}),
        **({"e2e": {key: sprint["e2e"][key] for key in ("budget", "used", "charges")}}
           if sprint.get("e2e") and (sprint["e2e"]["budget"] != 3 or sprint["e2e"]["used"] or sprint["e2e"]["charges"]) else {}),
        **(
            {"allowed_productions": [str(project) for project in sprint["allowed_productions"]]}
            if sprint.get("allowed_productions")
            else {}
        ),
        "status": str(sprint.get("status") or ""),
        "budget": {
            "by_type": {str(key): _int_or_none(value) or 0 for key, value in sorted(by_type.items())},
            **({"uncharged": uncharged} if uncharged else {}),
        },
        "current_task": str(sprint.get("current_task") or ""),
        "resume": (
            {str(key): (dict(value) if key == "po_request" and isinstance(value, dict) else str(value))
             for key, value in sorted(resume.items())}
            if isinstance(resume, dict)
            else None
        ),
        "audit": _sprint_audit(audit),
        "comments": [
            {"ts": str(comment.get("created_at") or ""), "text": str(comment.get("body") or "")}
            for comment in (comments if isinstance(comments, list) else [])
            if isinstance(comment, dict)
        ],
    }


def _sprint_executors(sprint: dict[str, Any]) -> dict[str, str | None]:
    """The worker and reviewer pins a row declares, and nothing for a role it pins nobody on.

    Absence has to survive recovery as absence: a record that carried a value for every role would
    restore a sprint the owner left free as one pinned to whatever the reader answered, which is the
    substitution this field exists to prevent. A key present and `None` is a stored value that is
    not a profile at all; restore refuses the set on it rather than recovering corruption as "the
    owner pinned nobody".
    """
    from ummanu.sprint_observer import EXECUTOR_FIELDS, EXECUTOR_PINNED, EXECUTOR_UNSET

    states = sprint.get("executors")
    states = states if isinstance(states, dict) else {}
    record: dict[str, str | None] = {}
    for role in EXECUTOR_FIELDS:
        state = states.get(role)
        state = state if isinstance(state, dict) else {"state": EXECUTOR_UNSET}
        if state.get("state") == EXECUTOR_UNSET:
            continue
        record[role] = str(state.get("profile")) if state.get("state") == EXECUTOR_PINNED else None
    return record


def _sprint_audit(audit: dict[str, Any]) -> dict[str, str]:
    """Prefer the audit a restored sprint came from over its recovery row."""
    from ummanu.sprints import SPRINT_BOARD_NAME

    source = audit.get("source")
    if isinstance(source, dict) and any(source.values()):
        return {
            "created_at": str(source.get("created_at") or ""),
            "updated_at": str(source.get("updated_at") or ""),
            "board": str(source.get("board") or SPRINT_BOARD_NAME),
        }
    backend = audit.get("backend")
    backend = backend if isinstance(backend, dict) else {}
    return {
        "created_at": str(audit.get("created_at") or ""),
        "updated_at": str(audit.get("updated_at") or ""),
        "board": str(backend.get("board") or SPRINT_BOARD_NAME),
    }


def export_memory(data_dir: Path, instance_dir: Path) -> DataExport:
    """Export the canon facts of ``instance_dir``, the only source there is."""
    data_dir = data_dir.expanduser().resolve()
    result = export_memory_snapshot(data_dir, instance_dir)
    return DataExport(
        path=result.path,
        count=result.count,
        source=result.source,
    )


def export_runs(
    data_dir: Path,
    *,
    state_dir: Path = PIPELINE_STATE_DIR,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    if not state_dir.is_dir():
        raise RuntimeError(f"state source not found: {state_dir}")

    runs_dir = data_dir / "runs"
    _ensure_dir(runs_dir, "runs data dir")
    records: list[dict[str, Any]] = []
    watermarks: list[dict[str, Any]] = []
    try:
        snapshot = Path(tempfile.mkdtemp(prefix=".state-", suffix=".tmp", dir=runs_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create runs snapshot: {exc}") from None
    try:
        _copy_tree(state_dir, snapshot)
        for path, file_stat in _regular_files_under(snapshot, context="runs snapshot"):
            if path.name.endswith(".lock") or path.suffix not in {".json", ".jsonl"}:
                continue
            relative = path.relative_to(snapshot).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
                lines = ndjson_lines(text) if path.suffix == ".jsonl" else text.splitlines()
            except OSError as exc:
                raise RuntimeError(f"could not read state file {relative}: {exc}") from None
            except UnicodeError as exc:
                raise RuntimeError(f"could not decode state file {relative}: {exc}") from None
            watermarks.append(
                {
                    "path": relative,
                    "bytes": file_stat.st_size,
                    "mtime": int(file_stat.st_mtime),
                    "lines": len(lines),
                }
            )
            if path.suffix == ".jsonl":
                for number, line in enumerate(lines, start=1):
                    if not line.strip():
                        continue
                    records.append(
                        {
                            "source": relative,
                            "line": number,
                            "record": _parse_jsonl_line(line, relative, number),
                        }
                    )

        cards_path = snapshot / "pipeline" / "cards.json"
        cards = _read_json_file_strict(cards_path) if cards_path.is_file() else {}
        if not isinstance(cards, dict):
            raise RuntimeError(f"state card mapping must be an object: {cards_path}")
        claims_path = snapshot / "pipeline" / "claims.json"
        claims = _read_json_file_strict(claims_path) if claims_path.is_file() else {}
        if not isinstance(claims, dict):
            raise RuntimeError(f"state claims must be an object: {claims_path}")
    finally:
        _cleanup_staging_dir(snapshot)

    try:
        staging = Path(tempfile.mkdtemp(prefix=".runs-export-", suffix=".tmp", dir=runs_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create runs export staging: {exc}") from None
    try:
        _write_ndjson(staging / "runs.ndjson", records)
        _write_json(staging / "watermarks.json", {"version": 1, "files": watermarks})
        _write_json(staging / "cards.json", {"version": 1, "cards": cards})
        _write_json(staging / "claims.json", {"version": 1, "claims": claims})
        _write_json(
            staging / "export.json",
            {
                "version": 1,
                "source": str(state_dir),
                "run_record_count": len(records),
                "watermark_count": len(watermarks),
                "card_mapping_count": len(cards) if isinstance(cards, dict) else 0,
                "claim_count": len(claims),
            },
        )
        _publish_component_entries(
            staging,
            runs_dir,
            ["runs.ndjson", "watermarks.json", "cards.json", "claims.json", "export.json"],
            "runs export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(path=runs_dir / "runs.ndjson", count=len(records), source=str(state_dir))


def export_transcripts(
    data_dir: Path,
    *,
    roots: list[Path] | None = None,
    copy: bool = False,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    roots = roots or [CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR]
    transcripts_dir = data_dir / "transcripts"
    _ensure_dir(transcripts_dir, "transcripts data dir")

    entries = []
    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            continue
        for path, file_stat in _regular_files_under(root, context="transcript source"):
            if path.suffix != ".jsonl":
                continue
            entry = {
                "path": str(path),
                "root": str(root),
                "relative_path": path.relative_to(root).as_posix(),
                "bytes": file_stat.st_size,
                "mtime": int(file_stat.st_mtime),
            }
            entries.append(entry)

    try:
        staging = Path(tempfile.mkdtemp(prefix=".transcripts-export-", suffix=".tmp", dir=transcripts_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create transcripts export staging: {exc}") from None
    try:
        _write_json(staging / "inventory.json", {"version": 1, "transcripts": entries})
        _write_ndjson(staging / "inventory.ndjson", entries)
        if copy:
            copy_dir = staging / "copies"
            try:
                copy_dir.mkdir(parents=True, exist_ok=True)
                for entry in entries:
                    destination = copy_dir / _safe_relative_copy_path(
                        entry["root"],
                        entry["relative_path"],
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry["path"], destination, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"could not copy transcripts: {exc}") from None
        _publish_component_entries(
            staging,
            transcripts_dir,
            ["inventory.json", "inventory.ndjson", "copies"],
            "transcripts export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(
        path=transcripts_dir / "inventory.json", count=len(entries), source=", ".join(str(p) for p in roots)
    )


def export_artifacts(
    data_dir: Path,
    *,
    workspaces_root: Path = ORCA_WORKSPACES_ROOT,
) -> DataExport:
    data_dir = data_dir.expanduser().resolve()
    artifacts_dir = data_dir / "artifacts"
    _ensure_dir(artifacts_dir, "artifacts data dir")

    entries = []
    for path, file_stat in _regular_files_under(artifacts_dir, context="artifact source"):
        relative = path.relative_to(artifacts_dir)
        if _skip_artifact_relative(relative):
            continue
        entries.append(
            {
                "kind": "existing",
                "relative_path": relative.as_posix(),
                "bytes": file_stat.st_size,
                "mtime": int(file_stat.st_mtime),
            }
        )

    task_docs = _task_artifact_docs(workspaces_root)
    entries.extend(task_docs)

    try:
        staging = Path(tempfile.mkdtemp(prefix=".artifacts-export-", suffix=".tmp", dir=artifacts_dir))
    except OSError as exc:
        raise RuntimeError(f"could not create artifacts export staging: {exc}") from None
    try:
        _write_json(
            staging / "inventory.json",
            {
                "version": 1,
                "policy": {
                    "project_worktrees": "only TASK.md and REVIEW.md root docs are copied",
                    "project_env": "excluded",
                },
                "artifacts": entries,
            },
        )
        _write_ndjson(staging / "inventory.ndjson", entries)
        for entry in task_docs:
            source = Path(entry["path"])
            destination = staging / "task-docs" / entry["relative_path"]
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"could not copy task artifact {entry['relative_path']}: {exc}") from None
        _publish_component_entries(
            staging,
            artifacts_dir,
            ["inventory.json", "inventory.ndjson", "task-docs"],
            "artifacts export",
        )
    except RuntimeError:
        _cleanup_staging_dir(staging)
        raise
    return DataExport(
        path=artifacts_dir / "inventory.json",
        count=len(entries),
        source=str(workspaces_root),
    )


def export_all(
    data_dir: Path,
    instance_dir: Path,
    *,
    copy_transcripts: bool = False,
) -> dict[str, DataExport]:
    return {
        "memory": export_memory(data_dir, instance_dir),
        "board": export_board(data_dir, instance_dir=instance_dir),
        "runs": export_runs(data_dir),
        "transcripts": export_transcripts(data_dir, copy=copy_transcripts),
        "artifacts": export_artifacts(data_dir),
    }


def _write_data_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
            dir=manifest_path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        os.close(fd)
        temp_path.write_text(payload, encoding="utf-8")
        try:
            candidate = json.loads(temp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError(f"generated invalid data manifest: {exc}") from None
        errors = validate(candidate, "data-manifest", manifest_path.name)
        if errors:
            details = "; ".join(str(error) for error in errors)
            raise RuntimeError(f"generated invalid data manifest: {details}") from None
        os.replace(temp_path, manifest_path)
    except OSError as exc:
        raise RuntimeError(f"could not write data manifest: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _parse_jsonl_line(line: str, relative: str, number: int) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSONL in state file {relative}:{number}: {exc}") from None


def _read_json_file_strict(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"could not read state file {path}: {exc}") from None
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError(f"invalid JSON in state file {path}: {exc}") from None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_relative_copy_path(root: str, relative: str) -> Path:
    prefix = root.strip("/").replace("/", "__") or "root"
    return Path(prefix) / relative


def _task_artifact_docs(workspaces_root: Path) -> list[dict[str, Any]]:
    workspaces_root = workspaces_root.expanduser().resolve()
    if not workspaces_root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    try:
        projects = sorted(path for path in workspaces_root.iterdir() if path.is_dir())
    except OSError as exc:
        raise RuntimeError(f"could not list workspaces root {workspaces_root}: {exc}") from None
    for project_dir in projects:
        if project_dir.name.startswith("."):
            continue
        try:
            workspaces = sorted(path for path in project_dir.iterdir() if path.is_dir())
        except OSError:
            continue
        for workspace in workspaces:
            if workspace.name.startswith("."):
                continue
            for name in ("TASK.md", "REVIEW.md"):
                path = workspace / name
                try:
                    file_stat = path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    relative = _display_relative(workspaces_root, path)
                    raise RuntimeError(f"could not inspect task artifact {relative}: {exc}") from None
                mode = file_stat.st_mode
                if not stat_module.S_ISREG(mode):
                    continue
                relative = path.relative_to(workspaces_root)
                if _skip_artifact_relative(relative):
                    continue
                entries.append(
                    {
                        "kind": "task-doc",
                        "path": str(path),
                        "relative_path": relative.as_posix(),
                        "bytes": file_stat.st_size,
                        "mtime": int(file_stat.st_mtime),
                    }
                )
    return entries


def _skip_artifact_relative(relative: Path) -> bool:
    return (
        relative.name in {"inventory.json", "inventory.ndjson"}
        or (relative.parts and relative.parts[0] == "task-docs")
        or ".git" in relative.parts
        or any(part.startswith(".") for part in relative.parts)
        or any(part.startswith(".env") for part in relative.parts)
        or any(part.endswith(".service") or part.endswith(".timer") for part in relative.parts)
        or "index.sqlite" in relative.parts
        or "backups" in relative.parts
    )
