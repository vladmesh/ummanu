from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ummanu._fsutil import display_relative
from ummanu.data import DataExport
from ummanu.po.queue import QUEUE_DIR_NAME

ARCHIVE_ROOT = "ummanu-backup"
POSTGRES_BACKUP_VERSION = 2

BackupKind = Literal["core", "full"]
RestoreAction = Literal["restore", "exclude"]
MEMORY_MODEL_CACHE = ("memory", "fastembed-cache")
# A process-bound applied-state receipt is recreated by ``ummanu upgrade`` after a live probe;
# restoring it would let a historical process identity masquerade as the current generation.
WEB_PROCESS_RECEIPT = ("web", "process-receipt.json")
# Optional debug material a full archive carried before A20 step 9 (secretary-1726): an inventory of
# the host's Orca state. No backup writes it now and no policy requires it. An older full archive
# that has it still verifies, since its checksum covers it, and restore never extracts it (only
# `ummanu-data/` is restored). A core archive never carried it and still may not.
LEGACY_DEBUG_ORCA_INVENTORY = "debug/orca-state/inventory.json"


@dataclass(frozen=True)
class ComponentPolicy:
    name: str
    path: str
    source_export: str | None = None
    required_entries: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    restore_action: RestoreAction = "restore"


@dataclass(frozen=True)
class BackupPolicy:
    kind: BackupKind
    components: tuple[ComponentPolicy, ...]
    forbidden_entries: tuple[str, ...]
    retention_seconds: int | None
    restore_capability: str

    @property
    def required_components(self) -> tuple[str, ...]:
        return tuple(component.name for component in self.components)

    @property
    def required_entries(self) -> tuple[str, ...]:
        base = (
            f"{ARCHIVE_ROOT}/versions.json",
            f"{ARCHIVE_ROOT}/instance/instance.yaml",
            f"{ARCHIVE_ROOT}/ummanu-data/data-manifest.json",
        )
        entries: list[str] = list(base)
        for component in self.components:
            entries.append(component_archive_name(component.path))
            entries.extend(component_archive_name(entry) for entry in component.required_entries)
        return tuple(dict.fromkeys(entries))


RUNS_STATE = ComponentPolicy(
    "runs_state",
    "runs/watermarks.json",
    required_entries=("runs/cards.json", "runs/claims.json"),
    required_fields=("cards", "claims"),
)

# Facts are canon in the private repo (docs/RECOVERY.md, "Layout"), so an
# archive carries the derived export, not a nested journal.
MEMORY = ComponentPolicy(
    "memory",
    "memory/export.ndjson",
    source_export="memory",
)

BOARD = ComponentPolicy(
    "board",
    "board/cards.json",
    source_export="board",
    required_entries=("board/cards.ndjson", "board/export.json"),
)
BOARD_HISTORY = ComponentPolicy(
    "board_history",
    "board/audit.json",
    required_entries=("board/audit.ndjson",),
)

CORE_POLICY = BackupPolicy(
    kind="core",
    components=(BOARD, MEMORY, RUNS_STATE, BOARD_HISTORY),
    forbidden_entries=(
        f"{ARCHIVE_ROOT}/ummanu-data/runs/runs.ndjson",
        f"{ARCHIVE_ROOT}/ummanu-data/transcripts/inventory.json",
        f"{ARCHIVE_ROOT}/ummanu-data/artifacts/inventory.json",
        f"{ARCHIVE_ROOT}/{LEGACY_DEBUG_ORCA_INVENTORY}",
    ),
    retention_seconds=None,
    restore_capability="normalized-core",
)

FULL_POLICY = BackupPolicy(
    kind="full",
    components=(
        ComponentPolicy("postgres_dump", "engine/postgres.dump"),
        BOARD,
        MEMORY,
        RUNS_STATE,
        ComponentPolicy("runs", "runs/runs.ndjson", source_export="runs"),
        ComponentPolicy("transcripts", "transcripts/inventory.json", source_export="transcripts"),
        ComponentPolicy("artifacts", "artifacts/inventory.json", source_export="artifacts"),
        BOARD_HISTORY,
    ),
    forbidden_entries=(),
    retention_seconds=48 * 60 * 60,
    restore_capability="postgres-local-recovery",
)

POLICIES: dict[BackupKind, BackupPolicy] = {
    "core": CORE_POLICY,
    "full": FULL_POLICY,
}
BACKUP_KINDS: tuple[BackupKind, ...] = tuple(POLICIES)


def policy_for(kind: object) -> BackupPolicy | None:
    if not isinstance(kind, str):
        return None
    return POLICIES.get(kind)


def component_archive_name(path: str) -> str:
    if path.startswith(("debug/", "engine/")):
        return f"{ARCHIVE_ROOT}/{path}"
    return f"{ARCHIVE_ROOT}/ummanu-data/{path}"


def restore_plan_components(policy: BackupPolicy, *, empty: bool = False) -> tuple[dict[str, str], ...]:
    components = tuple(
        {
            "name": component.name,
            "action": "initialized"
            if empty and component.restore_action == "restore"
            else component.restore_action,
        }
        for component in policy.components
    )
    if empty:
        return components
    return (
        *components,
        {"name": "memory_index", "action": "rebuild"},
        {"name": "board_restore", "action": "handoff"},
        {"name": "host_reconcile", "action": "handoff"},
    )


def build_components_manifest(
    *,
    policy: BackupPolicy,
    data_dir: Path,
    exports: dict[str, DataExport],
    postgres_dump: dict[str, Any] | None = None,
) -> dict[str, Any]:
    components: dict[str, Any] = {}
    for component in policy.components:
        if component.name == "postgres_dump":
            if postgres_dump is None:
                raise RuntimeError("PostgreSQL full backup has no engine dump")
            components[component.name] = {"path": component.path, **postgres_dump}
        elif component.name == "board_history":
            try:
                payload = json.loads((data_dir / component.path).read_text(encoding="utf-8"))
                count = len(payload.get("events", [])) if isinstance(payload, dict) else 0
            except (OSError, ValueError):
                count = 0
            components[component.name] = {"path": component.path, "count": count}
        elif component.name == "runs_state":
            components[component.name] = {
                "path": component.path,
                "cards": "runs/cards.json",
                "claims": "runs/claims.json",
                "source": exports["runs"].source,
            }
        elif component.source_export is not None:
            components[component.name] = _component_manifest(
                data_dir,
                exports[component.source_export],
            )
    return components


#: The PO service's durable input queue (`ummanu.po.queue`): inputs not yet taken as turns, and the
#: set-aside ones under `refused/`. Both archive kinds carry it (secretary-1770); its in-flight
#: temporary files are dot files and stay out like every other.
PO_QUEUE_ROOT = QUEUE_DIR_NAME


def should_skip_data_entry(relative: Path, *, policy: BackupPolicy) -> bool:
    allowed_roots = {"board", "memory", "runs", "transcripts", "artifacts", PO_QUEUE_ROOT}
    if not relative.parts:
        return False
    if relative.parts == WEB_PROCESS_RECEIPT:
        return True
    if relative.name.startswith(".env") or relative.name == "index.sqlite":
        return True
    if relative.parts[0] == "backups":
        return True
    if relative.parts[0] not in allowed_roots and relative.parts != ("data-manifest.json",):
        return True
    if is_memory_journal_git_runtime_entry(relative):
        return True
    if any(part.startswith(".") for part in relative.parts) and relative.parts[:2] != ("memory", "facts"):
        return True
    if policy.kind == "core":
        return _skip_core_data_entry(relative)
    return is_memory_model_cache_entry(relative)


def is_memory_model_cache_entry(relative: Path) -> bool:
    """The embedding model cache: a full archive leaves it out, the memory runtime downloads it again.

    Full archives written before the exclusion still carry it, so verify accepts it and restore
    drops it instead of calling such an archive unsafe.
    """
    return relative.parts[:2] == MEMORY_MODEL_CACHE


def is_memory_journal_git_entry(relative: Path) -> bool:
    return relative.parts[:3] == ("memory", "facts", ".git")


def is_memory_journal_git_runtime_entry(relative: Path) -> bool:
    if not is_memory_journal_git_entry(relative):
        return False
    git_path = relative.parts[3:]
    if git_path[:1] == ("objects",):
        return git_path[:2] == ("objects", "info") and len(git_path) > 2
    return not (git_path[:1] == ("refs",) or git_path in {("HEAD",), ("packed-refs",)})


def _skip_core_data_entry(relative: Path) -> bool:
    if not relative.parts:
        return False
    root = relative.parts[0]
    if root in {"transcripts", "artifacts"}:
        return True
    if root == "runs" and len(relative.parts) > 1:
        return relative.parts[1] not in {"watermarks.json", "cards.json", "claims.json"}
    return False


def _component_manifest(data_dir: Path, export: DataExport) -> dict[str, Any]:
    return {
        "path": display_relative(data_dir, export.path),
        "count": export.count,
        "source": export.source,
    }
