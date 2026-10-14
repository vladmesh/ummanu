from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu import _proc
from ummanu._fsutil import sha256_file
from ummanu.backup_policy import (
    ARCHIVE_ROOT,
    BACKUP_KINDS,
    POSTGRES_BACKUP_VERSION,
    BackupKind,
    build_components_manifest,
    policy_for,
    should_skip_data_entry,
)
from ummanu.backup_retention import (
    apply_retention as _apply_retention,
    remove_path_quietly as _remove_path_quietly,
)
from ummanu.backup_verify import (
    verify_backup,  # noqa: F401  # Public compatibility re-export.
)
from ummanu.board.backend import BOARD_STORE_KIND
from ummanu.board.store import STORE_FILE
from ummanu.config import ConfigError, DataDirError, instance_data_dir, load_config
from ummanu.data import (
    DataExport,
    export_all,
    init_layout,
)
from ummanu.runtime.paths import instance_file as config_file

PIPELINE_PAUSE_REASON = "ummanu backup create"
PIPELINE_PAUSE_ACTOR = "ummanu-backup"
PRODUCT_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    manifest: dict[str, Any]


def create_backups(
    instance_path: Path,
    *,
    data_dir: Path | None = None,
    copy_transcripts: bool = False,
    allow_claimed_worker: bool = False,
    caller_workspace: Path | None = None,
    pipeline_command: list[str] | None = None,
    backup_kinds: tuple[BackupKind, ...] = ("full",),
) -> list[BackupResult]:
    kinds = tuple(dict.fromkeys(backup_kinds))
    invalid = [kind for kind in kinds if kind not in BACKUP_KINDS]
    if invalid:
        raise RuntimeError(f"unsupported backup kind: {invalid[0]}")
    if not kinds:
        raise RuntimeError("at least one backup kind is required")

    exclude_workspace: Path | None = None
    if not allow_claimed_worker:
        _reject_claimed_worker_context()
    elif caller_workspace is None:
        raise RuntimeError(
            "backup create with allow_claimed_worker needs caller_workspace so the "
            "pipeline freeze can exclude the calling worker"
        )
    else:
        exclude_workspace = caller_workspace.expanduser().resolve()
    instance_file = config_file(instance_path.expanduser())
    data_dir = (data_dir or _load_data_dir(instance_file)).expanduser().resolve()
    from ummanu.board.postgres_recovery import PostgresRecoveryError, inspect_source

    try:
        postgres_config, postgres_metadata = inspect_source(instance_file.parent)
    except PostgresRecoveryError as exc:
        raise RuntimeError(str(exc)) from None

    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    stamp = created_at.replace("+00:00", "Z").replace("-", "").replace(":", "")
    final_archives: list[Path] = []
    results: list[BackupResult] = []

    paused_by_us = False
    completed = False
    temp_paths: list[Path] = []
    with _backup_create_lock(backups_dir):
        final_archives = [
            _unique_archive_path(backups_dir / f"ummanu-backup-{kind}-{stamp}.tar") for kind in kinds
        ]
        try:
            pre_pause = _pipeline_status(instance_file=instance_file, command=pipeline_command)
            if pre_pause.get("paused"):
                raise RuntimeError("pipeline is already paused; backup create must own the freeze")
            pause_status = _pipeline_action(
                "pause",
                instance_file=instance_file,
                command=pipeline_command,
                exclude_workspace=exclude_workspace,
            )
            paused_by_us = pause_status is None or _pause_owned_by_backup(pause_status)
            if not paused_by_us:
                raise RuntimeError("pipeline pause was not owned by backup create")

            init_layout(data_dir)
            exports = export_all(data_dir, instance_file.parent, copy_transcripts=copy_transcripts)

            postgres_dump_path: Path | None = None
            if "full" in kinds:
                from ummanu.board.postgres_recovery import create_dump

                # Staging lives beside the archives, never in the system temp directory: a tmpfs or
                # a unit's PrivateTmp is another filesystem (EXDEV on the final rename) and, for the
                # dump, a path the Docker daemon cannot see.
                dump_staging = Path(
                    tempfile.mkdtemp(prefix=".ummanu-postgres-dump-", suffix=".tmp", dir=backups_dir)
                )
                dump_staging.chmod(0o700)
                temp_paths.append(dump_staging)
                postgres_dump_path = dump_staging / "postgres.dump"
                try:
                    postgres_metadata = create_dump(
                        postgres_config, postgres_dump_path, postgres_metadata
                    )
                except PostgresRecoveryError as exc:
                    raise RuntimeError(str(exc)) from None

            for kind, final_archive in zip(kinds, final_archives, strict=True):
                staging = Path(tempfile.mkdtemp(prefix=".ummanu-backup-", suffix=".tmp", dir=backups_dir))
                temp_paths.append(staging)
                payload = staging / ARCHIVE_ROOT
                payload.mkdir()

                manifest = _build_versions_manifest(
                    created_at=created_at,
                    backup_kind=kind,
                    instance_file=instance_file,
                    data_dir=data_dir,
                    exports=exports,
                    postgres_dump=postgres_metadata if kind == "full" else None,
                )
                _copy_instance_config(instance_file.parent, payload / "instance")
                _copy_data_snapshot(data_dir, payload / "ummanu-data", backup_kind=kind)
                if kind == "full" and postgres_dump_path is not None:
                    engine_dump = payload / "engine" / "postgres.dump"
                    engine_dump.parent.mkdir(parents=True)
                    shutil.copyfile(postgres_dump_path, engine_dump)
                    engine_dump.chmod(0o600)
                if kind == "core":
                    core_board_count = _filter_core_board_export(payload / "ummanu-data" / "board")
                    manifest["components"]["board"]["count"] = core_board_count
                manifest["checksums"] = _payload_checksums(payload)
                _write_json(payload / "versions.json", manifest)

                plain_archive = staging / f"{kind}.tar"
                _write_tar(plain_archive, payload)
                temp_paths.append(plain_archive)

                os.replace(plain_archive, final_archive)
                results.append(BackupResult(archive=final_archive, manifest=manifest))
            _apply_retention(backups_dir, keep=set(final_archives), now=datetime.now(UTC))
            completed = True
        finally:
            try:
                if paused_by_us:
                    _pipeline_action(
                        "resume",
                        instance_file=instance_file,
                        command=pipeline_command,
                    )
            finally:
                for path in temp_paths:
                    _remove_path_quietly(path)
            if not completed:
                for final_archive in final_archives:
                    _remove_path_quietly(final_archive)

    missing = [path for path in final_archives if not path.is_file()]
    if missing:
        raise RuntimeError("archive was not created")
    return results


def _reject_claimed_worker_context() -> None:
    if os.environ.get("BOARD_ROLE") == "worker" or _claimed_workspace_from_cwd() is not None:
        raise RuntimeError("backup create must not run from a claimed worker; use an operator context")


def _claimed_workspace_from_cwd(cwd: Path | None = None) -> Path | None:
    current = (cwd or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if candidate.parent.parent.name != "workspaces":
            continue
        if (candidate / "TASK.md").is_file():
            return candidate
    return None


@contextmanager
def _backup_create_lock(backups_dir: Path) -> Iterator[None]:
    lock_path = backups_dir / ".create.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise RuntimeError(f"could not open backup create lock: {exc}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another backup create is already running") from None
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _build_versions_manifest(
    *,
    created_at: str,
    backup_kind: BackupKind,
    instance_file: Path,
    data_dir: Path,
    exports: dict[str, DataExport],
    postgres_dump: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy_for(backup_kind)
    if policy is None:
        raise RuntimeError(f"unsupported backup kind: {backup_kind}")
    return {
        "version": POSTGRES_BACKUP_VERSION,
        "backup_kind": backup_kind,
        "board_backend": BOARD_STORE_KIND,
        "restore_capability": policy.restore_capability,
        "created_at": created_at,
        "tool": "ummanu",
        "python": sys.version.split()[0],
        "git_commit": _git_commit(PRODUCT_REPO_ROOT),
        "instance": {
            "path": str(instance_file),
            "identity": _instance_identity(instance_file),
        },
        "data_dir": str(data_dir),
        "components": build_components_manifest(
            policy=policy,
            data_dir=data_dir,
            exports=exports,
            postgres_dump=postgres_dump,
        ),
    }


def _pipeline_action(
    action: str,
    *,
    instance_file: Path,
    command: list[str] | None,
    exclude_workspace: Path | None = None,
) -> dict[str, Any] | None:
    """Freeze or resume the pipeline through the product CLI (secretary-731).

    This used to shell out to the background agents' `pipeline pause`, which wrote a flag the production
    dispatcher never read: the backup ran while cards kept being claimed. `ummanu pause` is now
    the one door, and it mirrors the flag back to the legacy path for the background roles.
    """
    cmd = command or [sys.executable, "-P", "-m", "ummanu"]
    if action == "pause":
        args = [
            "pause",
            "freeze",
            "--instance",
            str(instance_file),
            "--reason",
            PIPELINE_PAUSE_REASON,
            "--actor",
            PIPELINE_PAUSE_ACTOR,
        ]
        if exclude_workspace is not None:
            args.extend(["--exclude-workspace", str(exclude_workspace)])
    elif action == "resume":
        args = ["resume", "--instance", str(instance_file), "--actor", PIPELINE_PAUSE_ACTOR]
    else:
        raise RuntimeError(f"unknown pipeline action: {action}")
    try:
        result = _proc.run([*cmd, *args], check=True, env=os.environ.copy())
        return json.loads(result.stdout) if result.stdout.strip() else None
    except FileNotFoundError:
        raise RuntimeError(f"pipeline command not found: {cmd[0]}") from None
    except subprocess.CalledProcessError as exc:
        if exclude_workspace is not None and _rejects_exclude_workspace(exc.stderr):
            raise RuntimeError(
                "pause command does not understand --exclude-workspace and would relaunch the "
                "calling worker on resume, so refusing to pause instead of dropping the flag"
            ) from None
        reason = (exc.stderr or exc.stdout or "pipeline command failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "pipeline command failed") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pipeline command returned invalid JSON: {exc}") from None


def _rejects_exclude_workspace(stderr: str | None) -> bool:
    text = (stderr or "").lower()
    return "unrecognized arguments" in text and "--exclude-workspace" in text


def _pipeline_status(
    *,
    instance_file: Path,
    command: list[str] | None,
) -> dict[str, Any]:
    cmd = command or [sys.executable, "-P", "-m", "ummanu"]
    try:
        result = _proc.run(
            [*cmd, "pause-status", "--instance", str(instance_file)], check=True, env=os.environ.copy()
        )
    except FileNotFoundError:
        raise RuntimeError(f"pipeline command not found: {cmd[0]}") from None
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or exc.stdout or "pipeline command failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "pipeline command failed") from None
    try:
        document = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pipeline command returned invalid JSON: {exc}") from None
    return _pause_summary(document if isinstance(document, dict) else {})


def _pause_summary(document: dict[str, Any]) -> dict[str, Any]:
    """The compact pause facts a backup decides on, out of the pause protocol document.

    `ummanu pause-status` answers with the layer's `pause_state` document (secretary-1576): the
    flag is one section of it, and every field of that section is null when the flag could not be
    read at all. That case is deliberately reported here as paused rather than as running. A backup
    must own the freeze it takes, and "nobody could read the pause flag" is not evidence that the
    pipeline is free -- the production tick reads an unreadable flag as a freeze, and a backup that
    read it as "not paused" would freeze on top of a state it cannot see.
    """
    state = document.get("state")
    state = state if isinstance(state, dict) else {}
    paused = state.get("paused")
    if not isinstance(paused, bool):
        return {
            "paused": True,
            "mode": None,
            "actor": None,
            "reason": "the pause state could not be established",
        }
    return {
        "paused": paused,
        "mode": state.get("mode"),
        "actor": state.get("actor"),
        "reason": state.get("pause_reason"),
    }


def _pause_owned_by_backup(status: dict[str, Any]) -> bool:
    return (
        status.get("paused") is True
        and status.get("actor") == PIPELINE_PAUSE_ACTOR
        and status.get("reason") == PIPELINE_PAUSE_REASON
        and (status.get("mode") == "freeze" or status.get("internal_mode") == "hard")
    )


def _skip_instance_config_entry(relative: Path) -> bool:
    return (
        ".git" in relative.parts
        or relative.name in {"runtime.env", STORE_FILE}
        or relative.name.startswith(".env")
    )


def _copy_instance_config(source: Path, destination: Path) -> None:
    _copy_tree_filtered(source, destination, skip=_skip_instance_config_entry)


def _copy_data_snapshot(data_dir: Path, destination: Path, *, backup_kind: BackupKind) -> None:
    policy = policy_for(backup_kind)
    if policy is None:
        raise RuntimeError(f"unsupported backup kind: {backup_kind}")

    def skip(relative: Path) -> bool:
        return should_skip_data_entry(relative, policy=policy)

    _copy_tree_filtered(data_dir, destination, skip=skip)


def _filter_core_board_export(board_dir: Path) -> int:
    cards_path = board_dir / "cards.json"
    ndjson_path = board_dir / "cards.ndjson"
    export_path = board_dir / "export.json"
    try:
        payload = json.loads(cards_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError(f"could not read core board export: {exc}") from None
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list):
        raise RuntimeError("core board export has no cards list")  # noqa: TRY004 - backup refusals share RuntimeError
    filtered = [
        card
        for card in cards
        if not (isinstance(card, dict) and str(card.get("column", "")).casefold() == "done")
    ]
    payload["cards"] = filtered
    _write_json(cards_path, payload)
    ndjson_path.write_text(
        "".join(json.dumps(card, sort_keys=True) + "\n" for card in filtered),
        encoding="utf-8",
    )
    try:
        export_payload = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        export_payload = {"version": 1}
    if isinstance(export_payload, dict):
        export_payload["card_count"] = len(filtered)
        export_payload["policy"] = {
            **(export_payload.get("policy") if isinstance(export_payload.get("policy"), dict) else {}),
            "done_cards": "excluded",
        }
        _write_json(export_path, export_payload)
    return len(filtered)


def _filtered_entries(source: Path, *, skip: Callable[[Path], bool]) -> Iterator[tuple[Path, Path]]:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if skip(relative):
            continue
        if path.is_symlink():
            continue
        yield path, relative


def _copy_tree_filtered(source: Path, destination: Path, *, skip: Callable[[Path], bool]) -> None:
    for path, relative in _filtered_entries(source, skip=skip):
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)


def _json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write_tar(destination: Path, source: Path) -> None:
    try:
        with tarfile.open(destination, "w") as archive:
            archive.add(source, arcname=ARCHIVE_ROOT, recursive=True)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"could not write backup tar: {exc}") from None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_text(payload), encoding="utf-8")


def _payload_checksums(payload: Path) -> dict[str, str]:
    """Return checksums for every regular payload file except its manifest."""
    checksums: dict[str, str] = {}
    manifest = payload / "versions.json"
    for path in sorted(payload.rglob("*")):
        if not path.is_file() or path.is_symlink() or path == manifest:
            continue
        checksums[path.relative_to(payload).as_posix()] = sha256_file(path)
    return checksums


def _load_data_dir(instance_file: Path) -> Path:
    try:
        return instance_data_dir(instance_file)
    except DataDirError as exc:
        raise RuntimeError(str(exc)) from None


def _instance_identity(instance_file: Path) -> dict[str, str]:
    try:
        instance = load_config(instance_file)
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from None
    offsite = instance.get("offsite") if isinstance(instance, dict) else None
    name = instance.get("name") if isinstance(instance, dict) else None
    remote = offsite.get("instance_remote") if isinstance(offsite, dict) else None
    if not isinstance(name, str) or not isinstance(remote, str):
        raise RuntimeError("instance.yaml has no usable identity")  # noqa: TRY004 - backup refusals share RuntimeError
    return {"name": name, "instance_remote": remote}


def _git_commit(repo_root: Path) -> str | None:
    try:
        result = _proc.run(["git", "rev-parse", "HEAD"], check=True, cwd=repo_root)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _unique_archive_path(path: Path) -> Path:
    suffix = 1
    while True:
        candidate = path if suffix == 1 else _suffixed_path(path, suffix)
        if not candidate.exists():
            return candidate
        suffix += 1


def _suffixed_path(path: Path, suffix: int) -> Path:
    name = path.name
    if name.endswith(".tar"):
        return path.with_name(f"{name.removesuffix('.tar')}-{suffix}.tar")
    return path.with_name(f"{path.stem}-{suffix}{path.suffix}")
