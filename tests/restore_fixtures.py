from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from ummanu import state_repo
from ummanu._fsutil import sha256_file
from ummanu.backup import BackupResult, create_backups
from ummanu.backup_policy import ARCHIVE_ROOT, POSTGRES_BACKUP_VERSION
from ummanu.board.provision import IMAGE, POSTGRES_MAJOR
from ummanu.data import DataExport, export_memory, init_layout, normalize_board_card

#: What `postgres_recovery.inspect_source` and `create_dump` report about a store, as `backup verify` requires it.
ENGINE_DUMP_METADATA: dict[str, Any] = {
    "engine": "postgresql",
    "format": "custom",
    "dump_version": 1,
    "image": IMAGE,
    "server_major": POSTGRES_MAJOR,
    "source_schema": "0007_card_transport_key",
    "alembic_head": "0007_card_transport_key",
    "restore_purpose": "local recovery",
    "source_endpoint_id": "a" * 64,
    "table_counts": {"tasks": 1},
}
#: A custom-format dump starts with this signature; verify reads nothing past it.
ENGINE_DUMP_BYTES = b"PGDMP-fixture"


def engine_dump_component() -> dict[str, Any]:
    return {
        "path": "engine/postgres.dump",
        **ENGINE_DUMP_METADATA,
        "tool_version": "(PostgreSQL) 16.10",
        "bytes": len(ENGINE_DUMP_BYTES),
    }


@contextlib.contextmanager
def fake_engine_dump() -> Iterator[mock.MagicMock]:
    """`create_backups` against a store that is not there: the source is inspected and dumped.

    The real `pg_dump` of a real store is proven in `tests/test_postgres_recovery.py`; a case here
    is about the archive around the dump, so the dump is a fixed custom-format stand-in.
    """

    def dump(_config: object, destination: Path, details: dict[str, Any]) -> dict[str, Any]:
        destination.write_bytes(ENGINE_DUMP_BYTES)
        component = engine_dump_component()
        component.pop("path")
        return {**details, **component}

    with (
        mock.patch(
            "ummanu.board.postgres_recovery.inspect_source",
            side_effect=lambda _instance: (object(), dict(ENGINE_DUMP_METADATA)),
        ),
        mock.patch("ummanu.board.postgres_recovery.create_dump", side_effect=dump) as create_dump,
    ):
        yield create_dump


def create_backup(instance_path: Path, *, backup_kind: str = "full", **kwargs) -> BackupResult:
    """One backup of one kind.

    The product asks for every kind it wants in a single pass, so this single-kind shape lives
    here rather than in `ummanu.backup`, where nothing but a test would call it.
    """
    return create_backups(instance_path, backup_kinds=(backup_kind,), **kwargs)[0]


def _write_instance(root: Path, name: str) -> Path:
    return _write_instance_to(root / "instance", name, root / "ummanu-data")


def _write_instance_to(
    instance: Path,
    name: str,
    data_dir: Path,
    *,
    host: bool = False,
    heads: bool = False,
    reindex: dict[str, object] | None = None,
) -> Path:
    instance.mkdir()
    host_block = "host:\n  unit_prefix: ummanu-\n" if host or reindex else ""
    for key, value in (reindex or {}).items():
        host_block += f"  {key}: {value}\n"
    heads_block = "heads:\n  - role: worker\n    model: test-model\n" if heads else ""
    text = (
        "version: 1\n"
        f"name: {name}\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n" + host_block + heads_block
    )
    (instance / "instance.yaml").write_text(text, encoding="utf-8")
    return instance


def _seed_instance_facts(instance_dir: Path, facts: dict[str, str]) -> Path:
    """Write canon facts where the private repo keeps them since the flatten."""
    facts_dir = state_repo.memory_facts_dir(instance_dir)
    facts_dir.mkdir(parents=True, exist_ok=True)
    for relative, text in facts.items():
        target = facts_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return facts_dir


def _restore_card(
    *,
    task_id: int = 12,
    reference: str = "ummanu-1",
    title: str = "Restore",
    description: str = "body",
    column: str = "Ready",
    swimlane: str = "Ummanu",
    position: int = 1,
    comments: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return normalize_board_card(
        {
            "id": task_id,
            "reference": reference,
            "title": title,
            "column": column,
            "swimlane": swimlane,
            "position": position,
            "task_type": "code",
            "project": "ummanu",
        },
        {
            "id": task_id,
            "reference": reference,
            "title": title,
            "description": description,
            "column": column,
            "task_type": "code",
            "project": "ummanu",
            "comments": comments or [],
            "metadata": {
                "record_type": "task",
                "complexity": "standard",
                "family_preference": "auto",
            },
        },
    )


def _prepare_producer_data(data_dir: Path, instance_dir: Path) -> None:
    """Seed a post-flatten producer: canon in the private repo, export derived."""
    init_layout(data_dir)
    _seed_instance_facts(instance_dir, {"fact.md": "# fact\n"})
    export_memory(data_dir, instance_dir)
    board = data_dir / "board"
    cards = [{"reference": "ummanu-1", "column": "Ready"}]
    (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
    (board / "cards.ndjson").write_text("".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8")
    (board / "export.json").write_text("{}", encoding="utf-8")
    _write_board_history(board)
    runs = data_dir / "runs"
    for name in ("watermarks.json", "cards.json", "claims.json"):
        (runs / name).write_text("{}", encoding="utf-8")
    (runs / "runs.ndjson").write_text("", encoding="utf-8")
    for component in ("transcripts", "artifacts"):
        directory = data_dir / component
        (directory / "inventory.json").write_text("{}", encoding="utf-8")
    (data_dir / "artifacts" / "report.pdf").write_bytes(b"report")


def _producer_exports(
    data_dir: Path,
    *,
    board: int = 1,
    memory: int = 1,
    artifacts: int = 1,
    source: str = "test",
) -> dict[str, DataExport]:
    return {
        "board": DataExport(data_dir / "board" / "cards.json", board, source),
        "memory": DataExport(data_dir / "memory" / "export.ndjson", memory, source),
        "runs": DataExport(data_dir / "runs" / "runs.ndjson", 0, source),
        "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 0, source),
        "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", artifacts, source),
    }


def _core_archive(root: Path, name: str) -> Path:
    payload = root / ARCHIVE_ROOT
    board = payload / "ummanu-data" / "board"
    memory = payload / "ummanu-data" / "memory" / "facts"
    runs = payload / "ummanu-data" / "runs"
    board.mkdir(parents=True)
    memory.mkdir(parents=True)
    (memory / "fact.md").write_text("# fact\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", "--initial-branch", "main"], cwd=memory, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=memory, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=memory, check=True)
    subprocess.run(["git", "add", "."], cwd=memory, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    (memory / "second-fact.md").write_text("# second fact\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=memory, check=True)
    subprocess.run(["git", "commit", "-m", "second fact"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    shutil.rmtree(memory / ".git" / "hooks")
    (memory / ".git" / "config").unlink()
    runs.mkdir(parents=True)
    (payload / "instance").mkdir()
    (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    (payload / "ummanu-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
    cards = [{"reference": "ummanu-1", "column": "Ready"}]
    (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
    (board / "cards.ndjson").write_text("".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8")
    (board / "export.json").write_text("{}", encoding="utf-8")
    _write_board_history(board)
    (payload / "ummanu-data" / "memory" / "export.ndjson").write_text(
        '{"id":"fact"}\n{"id":"second-fact"}\n', encoding="utf-8"
    )
    for filename in ("watermarks.json", "cards.json", "claims.json"):
        (runs / filename).write_text("{}", encoding="utf-8")
    manifest = {
        "version": POSTGRES_BACKUP_VERSION,
        "backup_kind": "core",
        "board_backend": "postgres",
        "instance": {"identity": {"name": name, "instance_remote": "git@example.invalid:test/instance.git"}},
        "components": {
            "board": {"path": "board/cards.json", "count": len(cards)},
            "memory": {"path": "memory/export.ndjson", "count": 2},
            "runs_state": {
                "path": "runs/watermarks.json",
                "cards": "runs/cards.json",
                "claims": "runs/claims.json",
            },
            "board_history": {"path": "board/audit.json", "count": 0},
        },
    }
    _write_checksums(payload, manifest)
    (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / "backup.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname=ARCHIVE_ROOT)
    return archive


def _full_archive(root: Path, name: str, *, legacy_orca_debug: bool = False) -> Path:
    """A full archive; `legacy_orca_debug` writes it as one from before A20 step 9 (secretary-1726),
    carrying the optional `debug_orca_state` component a full backup no longer writes."""
    _core_archive(root, name)
    payload = root / ARCHIVE_ROOT
    engine = payload / "engine"
    engine.mkdir()
    (engine / "postgres.dump").write_bytes(ENGINE_DUMP_BYTES)
    runs = payload / "ummanu-data" / "runs"
    (runs / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    for component in ("transcripts", "artifacts"):
        directory = payload / "ummanu-data" / component
        directory.mkdir()
        (directory / "inventory.json").write_text("{}", encoding="utf-8")
    if legacy_orca_debug:
        debug = payload / "debug" / "orca-state"
        debug.mkdir(parents=True)
        (debug / "inventory.json").write_text('{"version": 1, "files": []}\n', encoding="utf-8")
    manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
    manifest["backup_kind"] = "full"
    manifest["components"] = {
        "postgres_dump": engine_dump_component(),
        "board": {"path": "board/cards.json"},
        "memory": {"path": "memory/export.ndjson"},
        "runs_state": {
            "path": "runs/watermarks.json",
            "cards": "runs/cards.json",
            "claims": "runs/claims.json",
        },
        "runs": {"path": "runs/runs.ndjson"},
        "transcripts": {"path": "transcripts/inventory.json"},
        "artifacts": {"path": "artifacts/inventory.json"},
        **(
            {"debug_orca_state": {"path": "debug/orca-state/inventory.json"}}
            if legacy_orca_debug
            else {}
        ),
        "board_history": {"path": "board/audit.json", "count": 0},
    }
    _write_checksums(payload, manifest)
    (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / "full.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname=ARCHIVE_ROOT)
    return archive


def _write_board_history(board: Path) -> None:
    """The portable board history every archive carries next to the cards."""
    (board / "audit.json").write_text('{"version": 1, "events": []}\n', encoding="utf-8")
    (board / "audit.ndjson").write_text("", encoding="utf-8")


def _write_checksums(payload: Path, manifest: dict[str, object]) -> None:
    checksums: dict[str, str] = {}
    for path in sorted(payload.rglob("*")):
        if path.is_file() and path.name != "versions.json":
            checksums[path.relative_to(payload).as_posix()] = sha256_file(path)
    manifest["checksums"] = checksums


def _git_history(journal: Path) -> list[str]:
    result = subprocess.run(
        ["git", "rev-list", "HEAD"],
        cwd=journal,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.splitlines()
