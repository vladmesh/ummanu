"""Checkpoint writer and pusher for the private instance repository.

Contract: docs/RECOVERY.md, sections "Layout", "Cadence and RPO", "Writers", "Validation gate",
"Failure and divergence", "Observability". The writer regenerates the normalized board and runs
exports, validates the snapshot, and commits `state/board` and `state/runs` into the private
repo. The board is validated flat in staging and published in the split layout of
`ummanu.board.checkpoint_layout`, so a commit carries only the records and log segments that
changed. The independent checkpoint service invokes it at most once in a five-minute cadence window,
or once for a due remote recovery window; it takes the instance repo writer lock so
checkpoint writes cannot overlap a green-card publish against the same checkout.

Memory (`state/memory`), knowledge (`state/knowledge`) and the secret store's exported files
(`secrets/catalog.yaml`, `secrets/installation-key.json`, `secrets/values/*.enc.json`) are written
by their writers without Git, so in this legacy mode the checkpoint stages and commits them with board and
runs (`LEGACY_LIVE_PATHS`), after the same secret scan; `state_repo_lock` keeps every writer's files
from being half-written while it does.

`SnapshotExporter` grows the same staging and validation into the writer for a live root that is
not a Git work tree: one cut per changed window (the export, `SNAPSHOT_ALLOWLIST` copied from the
live root, `snapshot-manifest.json`) committed into a bare snapshot repository with Git plumbing.
`tick_checkpoint_writer` picks one of the two for the checkpoint service.
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu import state_repo
from ummanu._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
    ensure_dir as _ensure_dir,
    ndjson_lines,
    publish_component_entries as _publish_component_entries,
    remove_path as _remove_path,
    write_text_atomic as _write_text_atomic,
)
from ummanu.board.backend import CARD, board_client
from ummanu.board.checkpoint_layout import (
    FLAT,
    LAYOUT_MARKER,
    SEGMENT_FILES,
    CheckpointBoard,
    CheckpointLayoutError,
    layout_marker_text,
    open_checkpoint_board,
    part_name,
    publish_split_board,
)
from ummanu.board.models import Event
from ummanu.data import (
    PIPELINE_STATE_DIR,
    export_board,
    export_runs,
)
from ummanu.infra.export_allowlist import SNAPSHOT_ALLOWLIST, is_exported as is_exported, matches
from ummanu.infra.github_credential import (
    CredentialError,
    RemoteExecution,
)
from ummanu.product_issues import (
    ProductIssueValidationError,
    registered_projects,
)
from ummanu.runtime.redact import redact
from ummanu.state_repo import BOARD_RUNS_PATHSPEC
from ummanu.tasks import TaskError, task_audit_for

# Canonical checkpoint entries per component. `events.ndjson` is stored history: the
# pre-2026-09-10 file journal, which nothing writes any more. It is copied while the data dir
# still holds it, because offline analytics projects pre-cutover outcomes from that copy, and
# is published empty when it does not, because the analytics seal names it. Older checkpoints
# remain readable without either events.ndjson or the analytics seal.
ANALYTICS_MANIFEST = "analytics-manifest.json"
ANALYTICS_SCHEMA = "ummanu.board.analytics-checkpoint"
ANALYTICS_VERSION = 2
LEGACY_ANALYTICS_FILES = ("events.ndjson", "cards.ndjson", "sprints.ndjson", "export.json")
ANALYTICS_FILES = (
    "events.ndjson",
    "cards.ndjson",
    "sprints.ndjson",
    "audit.ndjson",
    "export.json",
)
BOARD_ENTRIES = (
    "cards.ndjson",
    "sprints.ndjson",
    "events.ndjson",
    "audit.ndjson",
    "export.json",
    ANALYTICS_MANIFEST,
)
BOARD_REQUIRED = ("cards.ndjson", "sprints.ndjson", "audit.ndjson", "export.json")
RUNS_ENTRIES = ("runs.ndjson", "claims.json", "watermarks.json", "export.json")
RUNS_REQUIRED = RUNS_ENTRIES
# The components `_publish` stages, in the order it publishes them.
CHECKPOINT_COMPONENTS = ("board", "runs")

# Derived neighbours of the canon. They are never copied into `state/`; the
# ignore files keep them out if anything else drops them there.
BOARD_IGNORE = (
    "cards.json",
    "sprints.json",
    "audit.json",
    ".audit.lock",
)
RUNS_IGNORE = ("cards.json",)

STAGED_PATHSPEC = BOARD_RUNS_PATHSPEC

# What the legacy tick commits beside board and runs: the live-root paths the Git-free writers (memory,
# knowledge, secret store) leave uncommitted, each as its Git pathspec and the allowlist pattern it
# stands for. The secret store's three are exactly its exported paths, so `secrets/installation.key`
# and the store's undo area are never staged. Nothing else joins (docs/RECOVERY.md, "Writers").
LEGACY_LIVE_PATHS = (
    ("state/memory", "state/memory/**"),
    ("state/knowledge", "state/knowledge/**"),
    ("secrets/catalog.yaml", "secrets/catalog.yaml"),
    ("secrets/installation-key.json", "secrets/installation-key.json"),
    (":(glob)secrets/values/*.enc.json", "secrets/values/*.enc.json"),
)

# Commit runs on every tick, push on its own window. 30 minutes is the durable
# RPO the contract promises.
PUSH_INTERVAL_SECONDS = 30 * 60
DEFAULT_REMOTE = "origin"
# A normalized state repo pushes in seconds; bound a stalled remote at a minute
# and retry in the next independent checkpoint window.
PUSH_TIMEOUT_SECONDS = 60


def _oldest_pending_text(audit_owner: Any) -> str:
    """`; oldest <kind> <request id> staged since <time>`, where the audit owner can say it."""
    oldest = getattr(audit_owner, "oldest_pending", None)
    if not callable(oldest):
        return ""
    try:
        record = oldest()
    except TaskError:
        return ""
    if not record:
        return ""
    return (
        f"; oldest {record.get('kind') or 'record'} {record.get('request_id')} "
        f"staged since {record.get('staged_at')}"
    )


class CheckpointBlocked(Exception):
    """The snapshot did not pass the gate; nothing is committed this tick."""


class AnalyticsManifestError(ValueError):
    """A directory is not a sealed analytics checkpoint input."""


@dataclass(frozen=True)
class AnalyticsCheckpoint:
    """Verified metadata for one immutable analytics input, never analytics rows."""

    checkpoint_id: str
    directory: Path
    #: The reader every row read of this cut goes through, in the layout that wrote it.
    board: CheckpointBoard


def _write_analytics_manifest(directory: Path) -> None:
    """Seal the already validated board files; publication places this file last."""
    entries: list[dict[str, Any]] = []
    for name in ANALYTICS_FILES:
        path = directory / name
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CheckpointBlocked(f"could not read board/{name} for analytics manifest: {exc}") from None
        entry: dict[str, Any] = {
            "path": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
        if name.endswith(".ndjson"):
            try:
                entry["line_count"] = _analytics_line_count(payload, path)
            except AnalyticsManifestError as exc:
                raise CheckpointBlocked(str(exc)) from None
        entries.append(entry)
    payload = {
        "schema": ANALYTICS_SCHEMA,
        "version": ANALYTICS_VERSION,
        "checkpoint_id": _analytics_checkpoint_id(entries),
        "files": entries,
    }
    try:
        _write_text_atomic(
            directory / ANALYTICS_MANIFEST, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
    except RuntimeError as exc:
        raise CheckpointBlocked(f"could not write board analytics manifest: {exc}") from None


def verify_analytics_checkpoint(directory: Path) -> AnalyticsCheckpoint:
    """Verify a sealed board cut using only files beneath ``directory``.

    This deliberately has no board, dispatcher, provider, transcript, comment,
    or runtime dependency. It returns only the sealed-cut identity; a later
    projection must call it before it parses analytics rows.
    """
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        _analytics_failure(root, "analytics checkpoint directory is missing")

    try:
        board = open_checkpoint_board(root)
    except CheckpointLayoutError as exc:
        raise AnalyticsManifestError(str(exc)) from None
    manifest_path = root / ANALYTICS_MANIFEST
    manifest = _read_analytics_json(manifest_path)
    expected_top_level = {"schema", "version", "checkpoint_id", "files"}
    if set(manifest) != expected_top_level:
        _analytics_failure(
            manifest_path, "manifest must contain exactly schema, version, checkpoint_id, files"
        )
    if manifest["schema"] != ANALYTICS_SCHEMA:
        _analytics_failure(manifest_path, f"unknown manifest schema {manifest['schema']!r}")
    version = manifest["version"]
    if not _is_int(version) or version not in {1, ANALYTICS_VERSION}:
        _analytics_failure(manifest_path, f"unknown manifest version {manifest['version']!r}")
    files = ANALYTICS_FILES if version == ANALYTICS_VERSION else LEGACY_ANALYTICS_FILES
    if board.layout != FLAT and files != ANALYTICS_FILES:
        _analytics_failure(manifest_path, "a split checkpoint must carry the current manifest version")
    checkpoint_id = manifest["checkpoint_id"]
    if not isinstance(checkpoint_id, str) or not re.fullmatch(r"[0-9a-f]{64}", checkpoint_id):
        _analytics_failure(manifest_path, "checkpoint_id must be a lowercase SHA-256 digest")

    entries = manifest["files"]
    if not isinstance(entries, list):
        _analytics_failure(manifest_path, "files must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for number, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _analytics_failure(manifest_path, f"files[{number}] entry must be an object")
        relative = entry.get("path")
        if not isinstance(relative, str) or relative not in files:
            _analytics_failure(manifest_path, f"files[{number}].path must name a required analytics file")
        if relative in indexed:
            _analytics_failure(manifest_path, f"files[{number}] duplicate manifest entry for {relative}")
        required_fields = {"path", "sha256", "bytes"}
        if relative.endswith(".ndjson"):
            required_fields.add("line_count")
        if set(entry) != required_fields:
            _analytics_failure(manifest_path, f"files[{number}] entry for {relative} has invalid fields")
        digest = entry["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            _analytics_failure(manifest_path, f"files[{number}] entry for {relative} has malformed sha256")
        if not _is_int(entry["bytes"]) or entry["bytes"] < 0:
            _analytics_failure(manifest_path, f"files[{number}] entry for {relative} has malformed bytes")
        if relative.endswith(".ndjson") and (not _is_int(entry["line_count"]) or entry["line_count"] < 0):
            _analytics_failure(
                manifest_path, f"files[{number}] entry for {relative} has malformed line_count"
            )
        indexed[relative] = entry

    missing_entries = [name for name in files if name not in indexed]
    if missing_entries:
        _analytics_failure(manifest_path, f"missing manifest entry for {', '.join(missing_entries)}")
    if len(entries) != len(files):
        _analytics_failure(manifest_path, "files must list each required analytics file exactly once")
    _verify_analytics_directory_files(root, board, files)

    canonical_entries: list[dict[str, Any]] = []
    for name in files:
        path = board.path(name)
        if not board.has(name) or (board.layout == FLAT and (not path.is_file() or path.is_symlink())):
            _analytics_failure(path, "required analytics file is missing or is not a regular file")
        try:
            payload = board.read_bytes(name)
        except (OSError, CheckpointLayoutError) as exc:
            _analytics_failure(path, f"could not read analytics file: {exc}")
        entry = indexed[name]
        actual_digest = hashlib.sha256(payload).hexdigest()
        if entry["sha256"] != actual_digest:
            _analytics_failure(path, "sha256 does not match manifest")
        if entry["bytes"] != len(payload):
            _analytics_failure(path, "byte count does not match manifest")
        canonical = {"path": name, "sha256": actual_digest, "bytes": len(payload)}
        if name.endswith(".ndjson"):
            line_count = _analytics_line_count(payload, path)
            if entry["line_count"] != line_count:
                _analytics_failure(path, "line count does not match manifest")
            canonical["line_count"] = line_count
        canonical_entries.append(canonical)

    expected_id = _analytics_checkpoint_id(canonical_entries)
    if checkpoint_id != expected_id:
        _analytics_failure(manifest_path, "checkpoint_id does not match manifest file entries")
    _verify_analytics_export_summary(board)
    return AnalyticsCheckpoint(checkpoint_id=checkpoint_id, directory=root, board=board)


def _analytics_failure(path: Path, detail: str) -> None:
    raise AnalyticsManifestError(f"{path}: {detail}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_analytics_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        _analytics_failure(path, "manifest is missing or is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        _analytics_failure(path, f"could not parse manifest: {exc}")
    if not isinstance(payload, dict):
        _analytics_failure(path, "manifest must be an object")
    return payload


def _analytics_line_count(payload: bytes, path: Path | None = None) -> int:
    try:
        return sum(1 for line in ndjson_lines(payload.decode("utf-8")) if line.strip())
    except UnicodeDecodeError as exc:
        if path is not None:
            _analytics_failure(path, f"could not decode analytics NDJSON as UTF-8: {exc}")
        raise AnalyticsManifestError(f"analytics NDJSON: could not decode UTF-8: {exc}") from None


def _analytics_checkpoint_id(entries: list[dict[str, Any]]) -> str:
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_analytics_directory_files(root: Path, board: CheckpointBoard, files: tuple[str, ...]) -> None:
    if board.layout == FLAT:
        allowed = {*files, ANALYTICS_MANIFEST, ".gitignore"}
    else:
        allowed = {*board.physical_entries(), ANALYTICS_MANIFEST, ".gitignore"}
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        _analytics_failure(root, f"could not list analytics checkpoint directory: {exc}")
    for entry in entries:
        if entry.name not in allowed:
            _analytics_failure(entry, "unlisted file in analytics checkpoint directory")


def _verify_analytics_export_summary(board: CheckpointBoard) -> None:
    export_path = board.path("export.json")
    try:
        summary = json.loads(board.read_text("export.json"))
    except (OSError, CheckpointLayoutError, ValueError) as exc:
        _analytics_failure(export_path, f"could not parse export summary: {exc}")
    if not isinstance(summary, dict):
        _analytics_failure(export_path, "export summary must be an object")
    for key, name in (("card_count", "cards.ndjson"), ("sprint_count", "sprints.ndjson")):
        declared = summary.get(key)
        if not _is_int(declared) or declared < 0:
            _analytics_failure(export_path, f"export summary has malformed {key}")
        try:
            actual = _analytics_line_count(board.read_bytes(name), board.path(name))
        except (OSError, CheckpointLayoutError) as exc:
            _analytics_failure(board.path(name), f"could not read analytics file: {exc}")
        if declared != actual:
            _analytics_failure(export_path, f"stale {key}: export.json={declared} {name}={actual}")


def _canonical_run_journals(path: Path, label: str) -> dict[str, list[str]]:
    """Compare each source journal by JSON value, never incidental spelling."""
    try:
        journals: dict[str, list[str]] = {}
        for line in ndjson_lines(path.read_text(encoding="utf-8")):
            if not line.strip():
                continue
            record = json.loads(line)
            source = record.get("source") if isinstance(record, dict) else None
            if not isinstance(source, str) or not source:
                raise ValueError("run record has no source")
            journals.setdefault(source, []).append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return journals
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is unreadable: {exc}") from None


@dataclass(frozen=True)
class CheckpointResult:
    status: str
    reason: str = ""
    commit: str = ""
    board_cards: int = 0
    run_records: int = 0
    #: How long the run that produced this result took, in milliseconds. Filled by
    #: :meth:`CheckpointWriter.write`, which is the one entry point that spans a whole run;
    #: 0.0 on a result built by hand, which took no time because it never ran.
    duration_ms: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "commit": self.commit,
            "board_cards": self.board_cards,
            "run_records": self.run_records,
            "duration_ms": self.duration_ms,
        }


class CheckpointWriter:
    """Regenerate, validate and commit the normalized `state/` snapshot."""

    def __init__(
        self,
        data_dir: Path,
        instance_dir: Path,
        *,
        state_dir: Path = PIPELINE_STATE_DIR,
        client: Any | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self.state_dir = Path(state_dir)
        # `client` is the seam a test -- or a caller that already holds the installation's card
        # client -- supplies its own board through. It is not a mode: with none given the writer
        # asks the switch for this installation's own client, exactly as `export_board` does.
        self._client = client
        #: What the last run settled of the stale staged audit rows (`_settle_stale_staged`).
        self.settled: tuple[dict[str, Any], ...] = ()

    def _audit_owner(self) -> tuple[Any, Any]:
        """The card client of this installation and the audit owner that client names.

        The gate below decides whether a snapshot may be published, so it may not be answered by a
        store nobody writes: the installation is gated on its staged `requests` rows
        (`docs/BOARD_STORE.md` §7.3). A client that cannot be established blocks the checkpoint by
        name instead of falling back to a file journal, whose absence or staleness would otherwise
        report a clean board that was never read.
        """
        try:
            client = self._client if self._client is not None else board_client(
                self.instance_dir, serves=(CARD,)
            )
            return client, task_audit_for(client, self.data_dir)
        except TaskError as exc:
            raise CheckpointBlocked(
                f"the card backend of {self.instance_dir} is unavailable, so the task audit "
                f"could not be read: {exc.message}"
            ) from None

    def write(self) -> CheckpointResult:
        """One checkpoint run, and how long it took.

        The clock starts before the state-repo lock, because waiting for another writer is part of
        what a checkpoint run costs an operator, and every outcome the method can produce leaves
        through here — committed, unchanged, and the blocked one the gate raises.
        """
        started = time.perf_counter()
        try:
            with state_repo.state_repo_lock(self.instance_dir):
                result = self._write()
        except CheckpointBlocked as exc:
            result = CheckpointResult(status="blocked", reason=str(exc))
        return replace(result, duration_ms=round((time.perf_counter() - started) * 1000.0, 3))

    def _write(self) -> CheckpointResult:
        self._collect_abandoned_staging()
        board, runs, secret_values = self._open_window(self.instance_dir / "state" / "runs" / "runs.ndjson")
        self._scan_live_paths(secret_values)
        self._publish(
            "board",
            BOARD_ENTRIES,
            BOARD_REQUIRED,
            BOARD_IGNORE,
            lambda staging: _validate_board(staging, instance=self.instance_dir),
            secret_values=secret_values,
        )
        self._publish(
            "runs",
            RUNS_ENTRIES,
            RUNS_REQUIRED,
            RUNS_IGNORE,
            _validate_runs,
            secret_values=secret_values,
        )
        return self._commit(board_cards=board, run_records=runs)

    def _open_window(self, canonical_runs: Path | None) -> tuple[int, int, tuple[str, ...]]:
        """The gate every cut passes before anything is staged: settled audit, fresh exports, intact
        run history; returns the card and run counts and this installation's redaction values.

        `canonical_runs` is the run journal last published, which the live export may only extend;
        None when nothing was published yet.
        """
        _, audit_owner = self._audit_owner()
        try:
            self._settle_stale_staged(audit_owner)
            audit = audit_owner.status()
        except TaskError as exc:
            raise CheckpointBlocked(
                f"the postgres task audit could not be read: {exc.message}"
            ) from None
        if not audit["ok"]:
            raise CheckpointBlocked(
                f"the postgres task audit has {audit['pending']} unresolved pending record(s)"
                + _oldest_pending_text(audit_owner)
            )

        board, runs = self._regenerate()
        if canonical_runs is not None:
            self._prevent_run_history_loss(canonical_runs)
        from ummanu.secret_store import SecretStoreError, redaction_values

        try:
            secret_values = redaction_values(self.instance_dir)
        except SecretStoreError as exc:
            raise CheckpointBlocked(f"could not load checkpoint redaction values: {exc}") from None
        return board, runs, secret_values

    def _settle_stale_staged(self, audit_owner: Any) -> None:
        """Let this tick settle what a dead writer left staged, before the gate counts it.

        The audit owner proves an effect from its store (`SqlTaskAudit.settle_stale_staged`). A row younger
        than the owner's grace is left alone and still blocks this checkpoint, as it always has.
        """
        settle = getattr(audit_owner, "settle_stale_staged", None)
        if not callable(settle):
            return
        self.settled = tuple(settle())

    def _collect_abandoned_staging(self) -> None:
        """Remove staging an earlier run left behind.

        Called under the state-repo lock: only one writer holds it, so a staging directory that
        exists now belongs to no live run. Only directories named exactly as `_publish` names its
        staging are touched.
        """
        parent = self.instance_dir / "state"
        for component in CHECKPOINT_COMPONENTS:
            for candidate in parent.glob(f".{component}-checkpoint-*.tmp"):
                if candidate.is_dir() and not candidate.is_symlink():
                    _cleanup_staging_dir(candidate)

    def _regenerate(self) -> tuple[int, int]:
        """Rebuild the exports from the live board and pipeline runtime state."""
        try:
            board = export_board(
                self.data_dir,
                instance_dir=self.instance_dir,
            )
            # Export validates before publishing its local pair. Re-read that freshly published
            # pair here so a substituted producer cannot reach staging or Git.
            from ummanu.board.normalized_checkpoint import (
                NormalizedBoardError,
                validated_normalized_cards,
            )

            if board.path.name == "cards.json":
                try:
                    validated_normalized_cards(self.data_dir / "board")
                except NormalizedBoardError as exc:
                    raise RuntimeError(f"board export is not restorable: {exc}") from None
            runs = export_runs(self.data_dir, state_dir=self.state_dir)
        except RuntimeError as exc:
            raise CheckpointBlocked(str(exc)) from None
        return board.count, runs.count

    def _prevent_run_history_loss(self, canonical: Path) -> None:
        """Never truncate or rewrite canonical history from a live export.

        Normal operation appends history, so an empty replacement is an unsafe recovery signal, not
        routine compaction.
        """
        live = self.data_dir / "runs" / "runs.ndjson"
        try:
            existing = _canonical_run_journals(canonical, "canonical run history")
        except FileNotFoundError:
            return
        try:
            current = _canonical_run_journals(live, "live run export")
        except (OSError, UnicodeError, ValueError) as exc:
            raise CheckpointBlocked(f"could not inspect canonical run history: {exc}") from None
        for source, canonical_history in existing.items():
            live_history = current.get(source, [])
            if live_history[: len(canonical_history)] != canonical_history:
                raise CheckpointBlocked(
                    "refusing to truncate or rewrite non-empty canonical run history "
                    f"for {source} from the live export"
                )

    def _publish(
        self,
        component: str,
        entries: tuple[str, ...],
        required: tuple[str, ...],
        ignore: tuple[str, ...],
        validate: Callable[[Path], None],
        *,
        secret_values: tuple[str, ...],
    ) -> None:
        source = self.data_dir / component
        destination = self.instance_dir / "state" / component
        _ensure_dir(destination, f"checkpoint {component} dir")
        try:
            staging = Path(
                tempfile.mkdtemp(prefix=f".{component}-checkpoint-", suffix=".tmp", dir=destination.parent)
            )
        except OSError as exc:
            raise CheckpointBlocked(f"could not stage checkpoint {component}: {exc}") from None

        try:
            staged = self._stage_validated(source, staging, entries, required, component, validate)
            _scan_for_secrets(
                staging,
                staged,
                component,
                runtime_env=self.instance_dir / "runtime.env",
                secret_values=secret_values,
            )
            if component == "board":
                _publish_board(staging, destination)
            else:
                _publish_component_entries(staging, destination, list(staged), f"checkpoint {component}")
                _drop_vanished(destination, entries, staged)
        except RuntimeError as exc:
            raise CheckpointBlocked(str(exc)) from None
        finally:
            # Staging never outlives its run, whatever leaves this block: a published cut has
            # already moved out of it, and any failure leaves only a partial copy behind.
            _cleanup_staging_dir(staging)

        _write_text_atomic(destination / ".gitignore", _ignore_text(ignore))

    def _stage_validated(
        self,
        source: Path,
        staging: Path,
        entries: tuple[str, ...],
        required: tuple[str, ...],
        component: str,
        validate: Callable[[Path], None],
    ) -> tuple[str, ...]:
        """Stage one component flat, validate it and, for the board, seal it; returns what was staged."""
        staged = self._stage(source, staging, entries, required, component)
        validate(staging)
        if component == "board":
            _write_analytics_manifest(staging)
            try:
                verify_analytics_checkpoint(staging)
            except AnalyticsManifestError as exc:
                raise CheckpointBlocked(str(exc)) from None
            staged = (*staged, ANALYTICS_MANIFEST)
        return staged

    def _stage(
        self,
        source: Path,
        staging: Path,
        entries: tuple[str, ...],
        required: tuple[str, ...],
        component: str,
    ) -> tuple[str, ...]:
        staged: list[str] = []
        for entry in entries:
            if component == "board" and entry == ANALYTICS_MANIFEST:
                continue
            origin = source / entry
            if not origin.exists():
                if component == "board" and entry == "events.ndjson":
                    try:
                        _write_text_atomic(staging / entry, "")
                    except RuntimeError as exc:
                        raise CheckpointBlocked(f"could not stage {component}/{entry}: {exc}") from None
                    staged.append(entry)
                    continue
                if entry in required:
                    raise CheckpointBlocked(f"checkpoint {component} export is missing {entry}")
                continue
            try:
                _write_text_atomic(staging / entry, _read_text(origin, entry))
            except OSError as exc:
                raise CheckpointBlocked(f"could not stage {component}/{entry}: {exc}") from None
            staged.append(entry)
        return tuple(staged)

    def _commit(self, *, board_cards: int, run_records: int) -> CheckpointResult:
        return self._commit_locked(board_cards=board_cards, run_records=run_records)

    def _scan_live_paths(self, secret_values: tuple[str, ...]) -> None:
        """Every live-root file this commit takes beside board and runs passes the scan.

        `state/memory`, `state/knowledge` and the exported secret-store files leave the host with
        this commit. Their writers scan what they write; this catches anything else left there, such
        as a secret pasted into a document by hand. A hit blocks the tick by path before anything is
        published or staged.
        """
        runtime_env = self.instance_dir / "runtime.env"
        hits: list[str] = []
        for relative in self._live_files():
            path = self.instance_dir / relative
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    text = os.readlink(path)
                elif stat.S_ISREG(info.st_mode):
                    text = path.read_bytes().decode("utf-8", errors="replace")
                else:
                    continue
            except OSError as exc:
                raise CheckpointBlocked(f"could not read {relative}: {exc}") from None
            if redact(text, env_files=[runtime_env], secret_values=secret_values) != text:
                hits.append(relative)
        if hits:
            raise CheckpointBlocked(f"secret detected in {', '.join(hits)}")

    def _live_files(self) -> list[str]:
        """Every non-directory entry of the live root `LEGACY_LIVE_PATHS` names, as sorted relative paths."""
        found: set[str] = set()
        for _spec, pattern in LEGACY_LIVE_PATHS:
            *directories, last = pattern.split("/")
            base = self.instance_dir.joinpath(*directories)
            if not base.is_dir() or base.is_symlink():
                continue
            if last == "**":
                for directory, dirnames, filenames in os.walk(base):
                    dirnames.sort()
                    names = filenames + [name for name in dirnames if (Path(directory) / name).is_symlink()]
                    for name in names:
                        found.add((Path(directory) / name).relative_to(self.instance_dir).as_posix())
                continue
            try:
                names = os.listdir(base)
            except OSError as exc:
                raise CheckpointBlocked(f"could not list {'/'.join(directories)}: {exc}") from None
            for name in names:
                relative = "/".join([*directories, name])
                if matches(pattern, relative) and not (base / name).is_dir():
                    found.add(relative)
        return sorted(found)

    def _commit_locked(self, *, board_cards: int, run_records: int) -> CheckpointResult:
        try:
            self._git(["add", "--", *STAGED_PATHSPEC], "checkpoint stage")
        except CheckpointBlocked:
            # A repo that ignores `state/` fails the add with a git hint; say why.
            self._require_tracked()
            raise
        self._require_tracked()
        pathspec = ["--", *STAGED_PATHSPEC, *self._stage_live_paths()]
        status = self._git(["status", "--porcelain", *pathspec], "checkpoint status")
        if not status.stdout.strip():
            return CheckpointResult(
                status="unchanged",
                board_cards=board_cards,
                run_records=run_records,
            )

        message = f"checkpoint(state): {board_cards} card(s), {run_records} run record(s)"
        self._git(
            [*self._identity(), "commit", "--quiet", "--message", message, *pathspec],
            "checkpoint commit",
        )
        head = self._git(["rev-parse", "HEAD"], "checkpoint head")
        return CheckpointResult(
            status="committed",
            commit=head.stdout.strip(),
            board_cards=board_cards,
            run_records=run_records,
        )

    def _stage_live_paths(self) -> tuple[str, ...]:
        """Stage `LEGACY_LIVE_PATHS`; returns the pathspecs that hold a file on disk or in Git.

        Only these join board and runs: config and every other path stay out of the tick's commit,
        and the secret store joins by its exported files alone. A pathspec that matches nothing
        would fail the commit, so a live root without memory, knowledge or a store commits as
        before; one whose files were all removed still stages the removal.
        """
        specs = [spec for spec, _pattern in LEGACY_LIVE_PATHS]
        tracked = self._git(["ls-files", "-z", "--", *specs], "checkpoint live paths tracked").stdout.split("\0")
        present = [*self._live_files(), *(name for name in tracked if name)]
        staged = tuple(
            spec for spec, pattern in LEGACY_LIVE_PATHS if any(matches(pattern, name) for name in present)
        )
        if staged:
            self._git(["add", "--", *staged], "checkpoint stage live paths")
        return staged

    def _require_tracked(self) -> None:
        """An ignored `state/` stages nothing, which otherwise reads as unchanged."""
        # The board is a directory in the split layout and a set of files in the flat one, so it is
        # tracked when anything under it is.
        canon = ["state/board", "state/runs/runs.ndjson"]
        tracked = self._git(["ls-files", "--", *canon], "checkpoint tracked").stdout.split()
        missing = [
            path for path in canon if not any(name == path or name.startswith(f"{path}/") for name in tracked)
        ]
        if missing:
            raise CheckpointBlocked(f"checkpoint is not tracked by the instance repo: {', '.join(missing)}")

    def _identity(self) -> list[str]:
        return state_repo.commit_identity(self.instance_dir)

    def _git(self, args: list[str], label: str) -> subprocess.CompletedProcess[str]:
        # The instance repository owns command shape, owner crossing and the Git
        # environment; this writer owns only the semantic failure it reports.
        try:
            result = state_repo.run_git(self.instance_dir, args, label=label)
        except state_repo.StateRepoError as exc:
            raise CheckpointBlocked(str(exc)) from None
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise CheckpointBlocked(f"{label} failed: {detail[-1] if detail else 'git error'}")
        return result


# The snapshot exporter (docs/RECOVERY.md, "Layout" and "Writers"). Its identity and subject prefix
# are what a later doctor check reads to tell an exporter commit from a foreign one.
SNAPSHOT_BRANCH = "main"
SNAPSHOT_REF = f"refs/heads/{SNAPSHOT_BRANCH}"
SNAPSHOT_AUTHOR_NAME = "ummanu snapshot exporter"
SNAPSHOT_AUTHOR_EMAIL = "snapshot-exporter@ummanu.invalid"
SNAPSHOT_SUBJECT_PREFIX = "snapshot(instance): "
SNAPSHOT_MANIFEST = "snapshot-manifest.json"
SNAPSHOT_MANIFEST_FORMAT = "ummanu.instance-snapshot"
SNAPSHOT_MANIFEST_VERSION = 1
# The takeover marker: where the exporter's own history on the snapshot branch begins. A ref to a
# blob holding one line, the parent of the exporter's first commit (a seeded legacy tip) or
# `SNAPSHOT_BASE_ROOT` when that commit was a root commit. Only the exporter creates it, once, in
# the same ref transaction as that first commit; doctor checks every commit after it.
SNAPSHOT_BASE_REF = "refs/ummanu/snapshot-base"
SNAPSHOT_BASE_ROOT = "root"
# The closed set of live-root paths a cut copies is `SNAPSHOT_ALLOWLIST`, kept in `infra.export_allowlist`
# with `is_exported`, the one answer to whether a live-root path leaves the host.
_REGULAR_MODE = "100644"
_EXECUTABLE_MODE = "100755"


def live_root_is_work_tree(instance_dir: Path) -> bool:
    """Whether the live root is still a Git work tree, which keeps the tick on the legacy commit."""
    return (Path(instance_dir).expanduser() / ".git").exists()


def tick_checkpoint_writer(data_dir: Path, instance_dir: Path) -> CheckpointWriter:
    """The tick's writer: today's commit into a live root that is a work tree, else the exporter."""
    if live_root_is_work_tree(instance_dir):
        return CheckpointWriter(data_dir, instance_dir)
    return SnapshotExporter(data_dir, instance_dir)


def tick_checkpoint_pusher(writer: CheckpointWriter) -> CheckpointPusher:
    """The tick's pusher for `writer`: the live root's branch, or the exporter's snapshot branch."""
    if isinstance(writer, SnapshotExporter):
        return CheckpointPusher(writer.instance_dir, snapshot_repo=lambda: writer.snapshot_repo)
    return CheckpointPusher(writer.instance_dir)


def _commit_fields(output: str) -> list[list[str]]:
    """`_COMMIT_FORMAT` records out of `rev-list --format` output (its `commit` headers dropped)."""
    return [line.split("\0") for line in output.splitlines() if "\0" in line]


# sha, parents, author name/email, committer name/email, subject.
_COMMIT_FORMAT = "%H%x00%P%x00%an%x00%ae%x00%cn%x00%ce%x00%s"


def _exporter_made(fields: list[str]) -> bool:
    """Whether a `_COMMIT_FORMAT` record carries the exporter's identity and subject prefix."""
    if len(fields) != 7:
        return False
    _sha, _parents, author, author_email, committer, committer_email, subject = fields
    return (
        author == committer == SNAPSHOT_AUTHOR_NAME
        and author_email == committer_email == SNAPSHOT_AUTHOR_EMAIL
        and subject.startswith(SNAPSHOT_SUBJECT_PREFIX)
    )


class SnapshotExporter(CheckpointWriter):
    """Commit one cut of the installation per changed window into a bare snapshot repository.

    A cut is the board and runs export, staged and validated as the legacy writer does; a byte copy
    of `SNAPSHOT_ALLOWLIST` from the live root; and `snapshot-manifest.json`. It is built in staging
    beside the snapshot repository, scanned for secrets file by file, and turned into a tree with Git
    plumbing through a temporary index, so the snapshot never has a work tree. The only Git calls go
    to the snapshot repository; the live root is read, never written, and its `.git` (if any) is not
    used. The exporter does not push; `CheckpointPusher` publishes the snapshot branch.
    """

    def __init__(
        self,
        data_dir: Path,
        instance_dir: Path,
        *,
        snapshot_repo: Path | None = None,
        state_dir: Path = PIPELINE_STATE_DIR,
        client: Any | None = None,
        product_revision: str | None = None,
        board_schema_head: str | None = None,
    ) -> None:
        super().__init__(data_dir, instance_dir, state_dir=state_dir, client=client)
        self._snapshot_repo = Path(snapshot_repo).expanduser().resolve() if snapshot_repo else None
        self._product_revision = product_revision
        self._board_schema_head = board_schema_head

    @property
    def snapshot_repo(self) -> Path:
        """`offsite.snapshot_repo` of the live root, unless the caller named one."""
        if self._snapshot_repo is None:
            from ummanu.config import DataDirError, instance_snapshot_repo

            try:
                self._snapshot_repo = instance_snapshot_repo(self.instance_dir, self.data_dir)
            except DataDirError as exc:
                raise CheckpointBlocked(f"could not resolve the snapshot repository: {exc}") from None
        return self._snapshot_repo

    def _write(self) -> CheckpointResult:
        repo = self.snapshot_repo
        self._ensure_repo(repo)
        self._collect_abandoned_staging()
        # The compare-and-swap base: whatever moves the branch after this read loses the window.
        tip = self._tip(repo)
        try:
            work = Path(tempfile.mkdtemp(prefix=f".{repo.name}-cut-", suffix=".tmp", dir=repo.parent))
        except OSError as exc:
            raise CheckpointBlocked(f"could not stage snapshot cut: {exc}") from None
        try:
            self._hand_to_git_child(work, repo)
            board, runs, secret_values = self._open_window(self._published_runs(repo, tip, work))
            cut = work / "cut"
            modes = self._stage_state(cut, work, repo, tip)
            modes.update(self._copy_allowlist(cut))
            self._write_manifest(cut, modes)
            modes[SNAPSHOT_MANIFEST] = _REGULAR_MODE
            self._scan_cut(cut, sorted(modes), secret_values)
            self._hand_to_git_child(work, repo)
            tree = self._build_tree(repo, cut, modes, work / "index")
            if tip and tree == self._repo_git(repo, ["rev-parse", f"{tip}^{{tree}}"], "snapshot tree").strip():
                return CheckpointResult(status="unchanged", board_cards=board, run_records=runs)
            commit = self._commit_cut(repo, tree, tip, board_cards=board, run_records=runs)
        except (OSError, RuntimeError) as exc:
            raise CheckpointBlocked(f"snapshot cut failed: {exc}") from None
        finally:
            _cleanup_staging_dir(work)
        return CheckpointResult(status="committed", commit=commit, board_cards=board, run_records=runs)

    # -- the snapshot repository -------------------------------------------------------------------

    def _ensure_repo(self, repo: Path) -> None:
        """Create and initialise the bare repository when it is absent; refuse anything else there."""
        if repo.is_symlink() or (repo.exists() and not repo.is_dir()):
            raise CheckpointBlocked(f"snapshot repository {repo} is not a directory")
        try:
            populated = repo.is_dir() and any(repo.iterdir())
        except OSError as exc:
            raise CheckpointBlocked(f"could not read snapshot repository {repo}: {exc}") from None
        if populated:
            probe = self._repo_run(repo, ["rev-parse", "--is-bare-repository"], "snapshot repository")
            if probe.returncode != 0 or probe.stdout.strip() != "true":
                raise CheckpointBlocked(f"snapshot repository {repo} exists but is not a bare Git repository")
            return
        try:
            repo.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CheckpointBlocked(f"could not create snapshot repository {repo}: {exc}") from None
        self._hand_to_git_child(repo, self.instance_dir)
        result = state_repo.run_git(
            repo, ["init", "--quiet", "--bare", "--initial-branch", SNAPSHOT_BRANCH], label="snapshot init"
        )
        if result.returncode != 0:
            raise CheckpointBlocked(f"snapshot init failed: {_last_line(result)}")

    def _tip(self, repo: Path) -> str:
        result = self._repo_run(
            repo, ["rev-parse", "--verify", "--quiet", f"{SNAPSHOT_REF}^{{commit}}"], "snapshot tip"
        )
        if result.returncode == 1 and not result.stdout.strip():
            return ""
        if result.returncode != 0:
            raise CheckpointBlocked(f"snapshot tip failed: {_last_line(result)}")
        return result.stdout.strip()

    def _published_runs(self, repo: Path, tip: str, work: Path) -> Path | None:
        """The run journal the tip carries, which the live export may only extend."""
        if not tip:
            return None
        spec = f"{tip}:state/runs/runs.ndjson"
        if self._repo_run(repo, ["cat-file", "-e", spec], "snapshot runs").returncode != 0:
            return None
        canonical = work / "published-runs.ndjson"
        _write_text_atomic(canonical, self._repo_git(repo, ["cat-file", "blob", spec], "snapshot runs"))
        return canonical

    def _commit_cut(self, repo: Path, tree: str, tip: str, *, board_cards: int, run_records: int) -> str:
        message = f"{SNAPSHOT_SUBJECT_PREFIX}{board_cards} card(s), {run_records} run record(s)"
        parents = ["-p", tip] if tip else []
        identity = {
            "GIT_AUTHOR_NAME": SNAPSHOT_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": SNAPSHOT_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": SNAPSHOT_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": SNAPSHOT_AUTHOR_EMAIL,
        }
        commit = self._repo_git(
            repo,
            ["commit-tree", "--no-gpg-sign", tree, *parents, "-m", message],
            "snapshot commit",
            extra_env=identity,
        ).strip()
        # Compare-and-swap against the tip this window started from; an all-zero old value means
        # "the branch must not exist yet".
        expected = tip or "0" * len(commit)
        updates = [f"update {SNAPSHOT_REF} {commit} {expected}\n"]
        base = self._takeover_base(repo, tip)
        if base:
            marker = self._repo_git(repo, ["hash-object", "-w", "--stdin"], "snapshot base", input=f"{base}\n")
            # `create` refuses an existing marker, so it is written once, with the commit it
            # describes or not at all.
            updates.append(f"create {SNAPSHOT_BASE_REF} {marker.strip()}\n")
        moved = self._repo_run(
            repo, ["update-ref", "-m", message, "--stdin"], "snapshot ref", input="".join(updates)
        )
        if moved.returncode != 0:
            raise CheckpointBlocked(
                f"snapshot branch moved during the window (expected {expected[:12]}): {_last_line(moved)}"
            )
        return commit

    def _takeover_base(self, repo: Path, tip: str) -> str:
        """What the takeover marker records for a commit on `tip`, or "" when it needs none.

        The marker exists once the exporter has taken the branch over. Without it, the first commit
        on an empty branch is a root commit and one on a tip the exporter did not make (a seeded
        legacy tip) starts the exporter's history; a tip the exporter made itself without a marker
        predates the marker, and its base is not the exporter's to guess (doctor says so).
        """
        known = self._repo_run(repo, ["rev-parse", "--verify", "--quiet", SNAPSHOT_BASE_REF], "snapshot base")
        if known.returncode == 0:
            return ""
        if not tip:
            return SNAPSHOT_BASE_ROOT
        listing = self._repo_git(
            repo, ["rev-list", "--no-walk", f"--format={_COMMIT_FORMAT}", tip], "snapshot tip identity"
        )
        records = _commit_fields(listing)
        return "" if records and _exporter_made(records[0]) else tip

    def seed(self, legacy_dir: Path) -> dict[str, Any]:
        """Fetch the legacy checkpoint's branch tip into an empty snapshot repository (one-shot).

        The cutover runs this once, after the final legacy checkpoint and push, so the exporter's
        next window commits a fast-forward child of that tip. The fetch is shallow: the push needs
        only the tip (the remote already holds its history), and the legacy history is large. A
        repository already at that tip is left as it is; one with exporter commits, or at another
        tip, is refused.
        """
        legacy = Path(legacy_dir).expanduser().resolve()
        try:
            with state_repo.state_repo_lock(self.instance_dir):
                return self._seed(legacy)
        except CheckpointBlocked as exc:
            return {"status": "blocked", "reason": str(exc)}

    def _seed(self, legacy: Path) -> dict[str, Any]:
        if not (legacy / ".git").exists():
            raise CheckpointBlocked(f"legacy instance {legacy} is not a Git work tree")
        try:
            branch = state_repo.git(
                legacy, ["symbolic-ref", "--quiet", "--short", "HEAD"], label="legacy branch"
            ).strip()
            legacy_tip = state_repo.git(
                legacy, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], label="legacy tip"
            ).strip()
        except state_repo.StateRepoError as exc:
            raise CheckpointBlocked(f"could not read the legacy branch tip: {exc}") from None
        repo = self.snapshot_repo
        self._ensure_repo(repo)
        tip = self._tip(repo)
        # A marker, or a tip the exporter made, means the exporter has committed here.
        if self._takeover_base(repo, tip) != (tip or SNAPSHOT_BASE_ROOT):
            raise CheckpointBlocked(f"snapshot repository {repo} already has exporter commits")
        if tip:
            if tip != legacy_tip:
                raise CheckpointBlocked(
                    f"snapshot repository {repo} is seeded at {tip[:12]}, but the legacy tip is "
                    f"{legacy_tip[:12]}; seed into an empty repository"
                )
            return {"status": "unchanged", "commit": tip, "legacy_branch": branch}
        # `-c safe.directory` reaches the upload-pack Git runs in the legacy repository too.
        fetched = self._repo_run(
            repo,
            [
                "-c",
                f"safe.directory={legacy}",
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--depth",
                "1",
                legacy.as_uri(),
                legacy_tip,
            ],
            "snapshot seed fetch",
        )
        if fetched.returncode != 0:
            raise CheckpointBlocked(f"snapshot seed fetch failed: {_last_line(fetched)}")
        moved = self._repo_run(
            repo,
            ["update-ref", "-m", f"seed from {legacy}", SNAPSHOT_REF, legacy_tip, "0" * len(legacy_tip)],
            "snapshot seed ref",
        )
        if moved.returncode != 0:
            raise CheckpointBlocked(f"snapshot seed ref failed: {_last_line(moved)}")
        return {"status": "seeded", "commit": legacy_tip, "legacy_branch": branch}

    def _build_tree(self, repo: Path, cut: Path, modes: dict[str, str], index: Path) -> str:
        """Write every cut file as a blob and the whole cut as one tree, through a temporary index."""
        paths = sorted(modes)
        hashed = self._repo_git(
            repo,
            ["hash-object", "-w", "--no-filters", "--stdin-paths"],
            "snapshot blobs",
            input="".join(f"{cut / path}\n" for path in paths),
        ).split()
        if len(hashed) != len(paths):
            raise CheckpointBlocked(f"snapshot blobs: git hashed {len(hashed)} of {len(paths)} file(s)")
        # `git_env` strips an inherited GIT_INDEX_FILE, so the temporary one is named explicitly.
        index_env = {"GIT_INDEX_FILE": str(index)}
        records = "".join(f"{modes[path]} {oid}\t{path}\0" for path, oid in zip(paths, hashed, strict=True))
        self._repo_git(repo, ["update-index", "-z", "--index-info"], "snapshot index", input=records, extra_env=index_env)
        return self._repo_git(repo, ["write-tree"], "snapshot tree", extra_env=index_env).strip()

    def _repo_run(
        self,
        repo: Path,
        args: list[str],
        label: str,
        *,
        input: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # `--git-dir` names the bare repository outright, so Git never discovers another one.
        try:
            return state_repo.run_git(
                repo, ["--git-dir", str(repo), *args], label=label, input=input, extra_env=extra_env
            )
        except state_repo.StateRepoError as exc:
            raise CheckpointBlocked(str(exc)) from None

    def _repo_git(
        self,
        repo: Path,
        args: list[str],
        label: str,
        *,
        input: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        result = self._repo_run(repo, args, label, input=input, extra_env=extra_env)
        if result.returncode != 0:
            raise CheckpointBlocked(f"{label} failed: {_last_line(result)}")
        return result.stdout

    def _hand_to_git_child(self, path: Path, owner_of: Path) -> None:
        """A root run hands what it created to the identity its Git children run as."""
        if os.getuid() != 0:
            return
        try:
            child = state_repo.git_child_identity(owner_of)
        except state_repo.StateRepoError as exc:
            raise CheckpointBlocked(str(exc)) from None
        if child.uid == 0:
            return
        try:
            for root, directories, files in os.walk(path):
                for name in (root, *(os.path.join(root, entry) for entry in (*directories, *files))):
                    os.chown(name, child.uid, child.gid, follow_symlinks=False)
        except OSError as exc:
            raise CheckpointBlocked(f"could not hand {path} to the snapshot repository owner: {exc}") from None

    def _collect_abandoned_staging(self) -> None:
        """Remove cut staging an earlier run left beside the snapshot repository (under the lock)."""
        repo = self.snapshot_repo
        for candidate in repo.parent.glob(f".{repo.name}-cut-*.tmp"):
            if candidate.is_dir() and not candidate.is_symlink():
                _cleanup_staging_dir(candidate)

    # -- the cut ---------------------------------------------------------------------------------

    def _stage_state(self, cut: Path, work: Path, repo: Path, tip: str) -> dict[str, str]:
        """`state/board` and `state/runs` of the cut, from the export, in the legacy writer's layout."""
        modes: dict[str, str] = {}
        flat = work / "board"
        flat.mkdir()
        self._stage_validated(
            self.data_dir / "board",
            flat,
            BOARD_ENTRIES,
            BOARD_REQUIRED,
            "board",
            lambda staging: _validate_board(staging, instance=self.instance_dir),
        )
        board = cut / "state" / "board"
        board.mkdir(parents=True)
        self._seed_segments(board, flat, repo, tip)
        try:
            publish_split_board(flat, board)
        except CheckpointLayoutError as exc:
            raise CheckpointBlocked(f"could not stage snapshot board: {exc}") from None
        _write_text_atomic(board / ANALYTICS_MANIFEST, _read_text(flat / ANALYTICS_MANIFEST, ANALYTICS_MANIFEST))
        _write_text_atomic(board / ".gitignore", _ignore_text(BOARD_IGNORE))

        runs_flat = work / "runs"
        runs_flat.mkdir()
        staged = self._stage_validated(
            self.data_dir / "runs", runs_flat, RUNS_ENTRIES, RUNS_REQUIRED, "runs", _validate_runs
        )
        runs = cut / "state" / "runs"
        runs.mkdir(parents=True)
        for entry in staged:
            _write_text_atomic(runs / entry, _read_text(runs_flat / entry, entry))
        _write_text_atomic(runs / ".gitignore", _ignore_text(RUNS_IGNORE))

        for path in sorted(p for p in (cut / "state").rglob("*") if p.is_file()):
            modes[path.relative_to(cut).as_posix()] = _REGULAR_MODE
        return modes

    def _seed_segments(self, board: Path, flat: Path, repo: Path, tip: str) -> None:
        """Lay the tip's log segments out again, so a log that grew gains one segment, as in Git.

        A segment is written back only from the new log's own bytes, and only while every one of
        them hashes to the blob the tip holds; anything else leaves the log to be rewritten as one
        segment, which is what the legacy writer does with history that does not extend.
        """
        if not tip:
            return
        listing = self._repo_git(
            repo,
            ["ls-tree", "-r", "-l", "--full-tree", tip, "--", "state/board/audit", "state/board/events"],
            "snapshot segments",
        )
        parts: dict[str, list[tuple[str, str, int]]] = {}
        for line in listing.splitlines():
            meta, _, path = line.partition("\t")
            fields = meta.split()
            if len(fields) != 4 or fields[1] != "blob" or not fields[3].isdigit():
                return
            directory = path.split("/")[2]
            parts.setdefault(directory, []).append((path, fields[2], int(fields[3])))
        object_format = self._repo_git(repo, ["rev-parse", "--show-object-format"], "snapshot format").strip()
        # The marker makes `publish_split_board` treat the seeded segments as the committed log.
        _write_text_atomic(board / LAYOUT_MARKER, layout_marker_text())
        for name, (directory, suffix) in SEGMENT_FILES.items():
            entries = sorted(parts.get(directory, []))
            payload = (flat / name).read_bytes()
            expected = [f"state/board/{directory}/{part_name(index, suffix).as_posix()}" for index in range(len(entries))]
            if [path for path, _, _ in entries] != expected:
                continue
            offset = 0
            slices: list[tuple[str, bytes]] = []
            for path, oid, size in entries:
                piece = payload[offset : offset + size]
                if len(piece) != size or _git_blob_id(piece, object_format) != oid:
                    slices = []
                    break
                slices.append((path, piece))
                offset += size
            for path, piece in slices:
                target = board / Path(path).relative_to("state/board")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(piece)

    def _copy_allowlist(self, cut: Path) -> dict[str, str]:
        """Copy every allowlisted regular file of the live root into the cut; refuse anything else.

        Enumeration and copy both walk from one descriptor of the live root, and every byte copied is
        read through `_open_beneath`, so a directory swapped for a symlink after enumeration blocks
        the window instead of leading the copy out of the live root.
        """
        modes: dict[str, str] = {}
        root = _open_live_root(self.instance_dir)
        try:
            for relative in _allowlisted_entries(root):
                descriptor = _open_beneath(root, relative.split("/"), want_directory=False)
                assert descriptor is not None
                try:
                    status = os.fstat(descriptor)
                    with os.fdopen(descriptor, "rb", closefd=False) as handle:
                        payload = handle.read()
                finally:
                    os.close(descriptor)
                target = cut / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                modes[relative] = _EXECUTABLE_MODE if status.st_mode & stat.S_IXUSR else _REGULAR_MODE
        finally:
            os.close(root)
        return modes

    def _write_manifest(self, cut: Path, modes: dict[str, str]) -> None:
        """`snapshot-manifest.json`: format, digests of every other file, revision, schema head; no clock."""
        manifest = {
            "format": SNAPSHOT_MANIFEST_FORMAT,
            "version": SNAPSHOT_MANIFEST_VERSION,
            "product_revision": self._revision(),
            "board_schema_head": self._schema_head(),
            "files": {path: hashlib.sha256((cut / path).read_bytes()).hexdigest() for path in sorted(modes)},
        }
        _write_text_atomic(cut / SNAPSHOT_MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def _revision(self) -> str:
        if self._product_revision is None:
            from ummanu.head_registry import product_revision

            self._product_revision = product_revision(Path(__file__).resolve().parents[2])
        return self._product_revision

    def _schema_head(self) -> str:
        if self._board_schema_head is None:
            from ummanu.board.migrate import head_revision
            from ummanu.board.store import BoardStoreError

            try:
                self._board_schema_head = head_revision()
            except (BoardStoreError, ImportError, OSError) as exc:
                raise CheckpointBlocked(f"could not read the board schema head: {exc}") from None
        return self._board_schema_head

    def _scan_cut(self, cut: Path, paths: list[str], secret_values: tuple[str, ...]) -> None:
        """The whole cut leaves the host, so every file of it passes the secret scan."""
        hits: list[str] = []
        runtime_env = self.instance_dir / "runtime.env"
        for path in paths:
            text = (cut / path).read_bytes().decode("utf-8", errors="replace")
            if redact(text, env_files=[runtime_env], secret_values=secret_values) != text:
                hits.append(path)
        if hits:
            raise CheckpointBlocked(f"secret detected in snapshot: {', '.join(hits)}")


def exported_files(live_root: Path) -> list[str]:
    """Every live-root file the next cut copies, as sorted relative paths, read as the exporter reads
    them: nothing is followed, and a symlink or non-regular entry raises `CheckpointBlocked`."""
    return _allowlisted_files(live_root)


def _allowlisted_files(live_root: Path) -> list[str]:
    """Every live-root file `SNAPSHOT_ALLOWLIST` names, as sorted relative paths."""
    root = _open_live_root(live_root)
    try:
        return _allowlisted_entries(root)
    finally:
        os.close(root)


def _open_live_root(live_root: Path) -> int:
    try:
        return os.open(live_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise CheckpointBlocked(f"could not open the live root {live_root}: {exc}") from None


def _allowlisted_entries(root: int) -> list[str]:
    """`SNAPSHOT_ALLOWLIST` expanded beneath the live-root descriptor `root`.

    Nothing is followed: a symlink or any non-regular entry at an allowlisted path, or on the way
    to one, blocks the window by its path. Directories are entered only through `_open_beneath`.
    """
    found: set[str] = set()
    for pattern in SNAPSHOT_ALLOWLIST:
        *directories, last = pattern.split("/")
        base = _open_beneath(root, directories, want_directory=True, missing_ok=True)
        if base is None:
            continue
        try:
            if last == "**":
                found.update(_regular_files_below(base, directories))
            elif any(character in last for character in "*?["):
                for name in _names(base, directories):
                    if fnmatch.fnmatchcase(name, last):
                        found.add(_require_regular(base, directories, name))
            elif _entry_status(base, last) is not None:
                found.add(_require_regular(base, directories, last))
        finally:
            os.close(base)
    return sorted(found)


def _open_beneath(
    directory: int,
    names: list[str],
    *,
    want_directory: bool,
    prefix: list[str] | None = None,
    missing_ok: bool = False,
) -> int | None:
    """Open `names` beneath the descriptor `directory` without following any symlink.

    The one way the exporter opens live-root content: each component is opened relative to the
    descriptor of its parent, `O_DIRECTORY | O_NOFOLLOW` for every directory and `O_NOFOLLOW` for a
    file leaf (`O_NONBLOCK`, so a FIFO swapped in cannot hang the window), and every descriptor is
    checked with `fstat`. Returns a new descriptor the caller closes; None only for a missing
    component when `missing_ok`. Anything else blocks the window by the path it reached.
    """
    current = os.dup(directory)
    try:
        for index, name in enumerate(names):
            as_directory = want_directory or index < len(names) - 1
            shown = "/".join([*(prefix or []), *names[: index + 1]])
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            flags |= os.O_DIRECTORY if as_directory else os.O_NONBLOCK
            try:
                opened = os.open(name, flags, dir_fd=current)
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise CheckpointBlocked(f"snapshot refuses {shown}: it vanished during the window") from None
            except OSError as exc:
                # A refused symlink reads as ELOOP or, under O_DIRECTORY, ENOTDIR; name what is there.
                status = _entry_status(current, name)
                if status is not None and stat.S_ISLNK(status.st_mode):
                    reason = "symlink at an allowlisted path"
                elif exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    reason = "not a plain directory" if as_directory else "not a regular file"
                else:
                    reason = exc.strerror or str(exc)
                raise CheckpointBlocked(f"snapshot refuses {shown}: {reason}") from None
            os.close(current)
            current = opened
            mode = os.fstat(current).st_mode
            if as_directory and not stat.S_ISDIR(mode):
                raise CheckpointBlocked(f"snapshot refuses {shown}: not a plain directory")
            if not as_directory and not stat.S_ISREG(mode):
                raise CheckpointBlocked(f"snapshot refuses {shown}: not a regular file")
        opened, current = current, -1
        return opened
    finally:
        if current >= 0:
            os.close(current)


def _regular_files_below(directory: int, parts: list[str]) -> list[str]:
    found: list[str] = []
    for name in _names(directory, parts):
        status = _entry_status(directory, name)
        if status is not None and stat.S_ISDIR(status.st_mode):
            child = _open_beneath(directory, [name], want_directory=True, prefix=parts)
            assert child is not None
            try:
                found.extend(_regular_files_below(child, [*parts, name]))
            finally:
                os.close(child)
        else:
            found.append(_require_regular(directory, parts, name))
    return found


def _names(directory: int, parts: list[str]) -> list[str]:
    try:
        return sorted(os.listdir(directory))
    except OSError as exc:
        raise CheckpointBlocked(f"could not list {'/'.join(parts) or '.'}: {exc}") from None


def _entry_status(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_regular(directory: int, parts: list[str], name: str) -> str:
    relative = "/".join([*parts, name])
    status = _entry_status(directory, name)
    if status is not None and stat.S_ISLNK(status.st_mode):
        raise CheckpointBlocked(f"snapshot refuses {relative}: symlink at an allowlisted path")
    if status is None or not stat.S_ISREG(status.st_mode):
        raise CheckpointBlocked(f"snapshot refuses {relative}: not a regular file")
    try:
        relative.encode("utf-8")
    except UnicodeError:
        raise CheckpointBlocked(f"snapshot refuses {relative!r}: the name is not UTF-8") from None
    if "\n" in relative or "\0" in relative:
        raise CheckpointBlocked(f"snapshot refuses {relative!r}: the name holds a line break")
    return relative


def _git_blob_id(payload: bytes, object_format: str) -> str:
    return hashlib.new(object_format, b"blob %d\0" % len(payload) + payload).hexdigest()


def _ignore_text(ignore: tuple[str, ...]) -> str:
    return "".join(f"{line}\n" for line in ignore)


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return detail[-1] if detail else "git error"


class _GitFailure(Exception):
    """A git command the pusher needs did not run or did not succeed."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output or message


@dataclass(frozen=True)
class _PublishedRepo:
    """The repository whose branch the pusher publishes and doctor measures.

    The live root while it is a work tree (`HEAD`), else the bare snapshot repository, which every
    command names with `--git-dir` so Git never discovers another repository around it.
    """

    path: Path
    bare: bool

    @property
    def ref(self) -> str:
        return SNAPSHOT_REF if self.bare else "HEAD"

    def args(self, args: list[str]) -> list[str]:
        return ["--git-dir", str(self.path), *args] if self.bare else list(args)


@dataclass(frozen=True)
class PushOutcome:
    status: str
    reason: str = ""
    commit: str = ""
    credential: dict[str, Any] | None = None


class CheckpointPusher:
    """Send the committed checkpoint to the remote, fast-forward only.

    Fail-closed on the checkpoint, not on the work: a failed push leaves its reason and a growing lag
    in state while the dispatcher keeps running. A remote holding commits the local repo does not
    have stops the push and raises `remote diverged`; there is no force-push path here.

    The repository whose branch is pushed and the live root are the same `instance_dir` until the
    live root stops being a work tree. With `snapshot_repo` the pusher publishes `SNAPSHOT_REF` of
    that bare repository instead, to a remote it points at `offsite.instance_remote` itself; the
    lock, the managed credential and the push state still belong to the live root.
    """

    def __init__(
        self,
        instance_dir: Path,
        *,
        snapshot_repo: Path | Callable[[], Path] | None = None,
        remote: str = DEFAULT_REMOTE,
        interval_seconds: float = PUSH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self._snapshot_repo = snapshot_repo
        self.remote = remote
        self.interval_seconds = float(interval_seconds)
        self._clock = clock
        self._credential: dict[str, Any] = {"state": "ambient/manual-bypass", "reason": "not verified"}
        self._published = _PublishedRepo(self.instance_dir, bare=False)

    def due(self, state: dict[str, Any] | None = None, *, now: float | None = None) -> bool:
        """Whether the next remote publication window is due.

        The dispatcher asks this before preparing its periodic checkpoint, so a due
        remote window can share one fresh, verified preparation with the ordinary
        five-minute cadence.  Keep the decision beside ``push``: callers must not
        grow a second interpretation of a 30-minute window.
        """
        stamp = float(self._clock()) if now is None else float(now)
        return self._due(dict(state or {}), stamp)

    def push(self, state: dict[str, Any] | None = None, *, now: float | None = None) -> dict[str, Any]:
        """Run the push if its window is due; return the new push state."""
        current = dict(state or {})
        stamp = float(self._clock()) if now is None else float(now)
        if not self._due(current, stamp):
            return current
        return self._record(current, self._attempt(), stamp)

    def _due(self, state: dict[str, Any], now: float) -> bool:
        return is_push_due(state, now, interval_seconds=self.interval_seconds)

    def _attempt(self) -> PushOutcome:
        try:
            with state_repo.state_repo_lock(self.instance_dir):
                if self._snapshot_repo is not None:
                    skipped = self._prepare_snapshot()
                    if skipped:
                        return PushOutcome("skipped", skipped)
                    branch = SNAPSHOT_BRANCH
                else:
                    branch = self._branch()
                    if not branch:
                        return PushOutcome("skipped", "instance repo has no checked-out branch")
                    if not self._has_remote():
                        return PushOutcome("skipped", f"instance repo has no remote '{self.remote}'")
                remote_url = self._git(["remote", "get-url", self.remote], "checkpoint remote URL").strip()
                remote_git = self._remote_execution(remote_url)
                head = self._git(["rev-parse", self._published.ref], "checkpoint head").strip()
                remote_head = self._remote_head(branch, remote_git)
                if remote_head and remote_head == head:
                    return PushOutcome("unchanged", commit=head)
                if remote_head and not self._fast_forward(remote_head, head):
                    history = "snapshot" if self._published.bare else "checkpoint"
                    return PushOutcome(
                        "diverged",
                        f"remote {self.remote}/{branch} is at {remote_head[:12]}, "
                        f"which the {history} history does not contain",
                    )
                self._remote_git(
                    remote_git,
                    ["push", "--quiet", self.remote, f"{self._published.ref}:refs/heads/{branch}"],
                    "checkpoint push",
                    timeout=PUSH_TIMEOUT_SECONDS,
                )
                return PushOutcome("pushed", commit=head)
        except _GitFailure as exc:
            # The remote can move between the probe and the push. Git rejects the
            # non-ff itself; read that rejection as the divergence it is.
            if any(mark in exc.output for mark in ("non-fast-forward", "fetch first", "[rejected]")):
                return PushOutcome("diverged", str(exc))
            return PushOutcome("failed", str(exc))

    def _record(self, state: dict[str, Any], outcome: PushOutcome, now: float) -> dict[str, Any]:
        state.update(
            {
                "remote": self.remote,
                "status": outcome.status,
                "reason": outcome.reason,
                "attempted_epoch": now,
                "attempted_at": _rfc3339(now),
            }
        )
        if outcome.credential is not None:
            state["credential"] = outcome.credential
        elif self._credential:
            credential = dict(self._credential)
            previous = _object_field(state, "credential")
            if credential.get("state") == "managed-ready":
                credential["last_verified_epoch"] = now
                credential["last_verified_at"] = _rfc3339(now)
            else:
                for field in ("last_verified_epoch", "last_verified_at"):
                    if field in previous:
                        credential[field] = previous[field]
            state["credential"] = credential
        state.setdefault("failures", 0)
        state.setdefault("last_push_at", "")
        state.setdefault("last_push_commit", "")
        if outcome.status in ("pushed", "unchanged"):
            state.update(
                {
                    "last_push_epoch": now,
                    "last_push_at": _rfc3339(now),
                    "last_push_commit": outcome.commit,
                    "failures": 0,
                    "remote_diverged": False,
                }
            )
            state.pop("retry_pending", None)
        elif outcome.status == "skipped":
            state["remote_diverged"] = False
            state.pop("retry_pending", None)
        else:
            state["failures"] = int(_float_field(state, "failures")) + 1
            state["remote_diverged"] = outcome.status == "diverged"
        return state

    def _prepare_snapshot(self) -> str:
        """Select the snapshot repository and point its remote at `offsite.instance_remote`.

        Returns why there is nothing to push yet, or "". The remote URL is rewritten whenever it
        differs from the configured one, so a new `offsite.instance_remote` re-points the next push.
        """
        source = self._snapshot_repo
        assert source is not None
        try:
            repo = Path(source() if callable(source) else source).expanduser().resolve()
        except CheckpointBlocked as exc:
            raise _GitFailure(str(exc)) from None
        if not (repo / "HEAD").is_file():
            return f"snapshot repository {repo} does not exist yet"
        self._published = _PublishedRepo(repo, bare=True)
        from ummanu.config import DataDirError, instance_offsite_remote

        try:
            remote_url = instance_offsite_remote(self.instance_dir)
        except DataDirError as exc:
            raise _GitFailure(f"could not read offsite.instance_remote: {exc}") from None
        if not remote_url:
            return "instance.yaml has no offsite.instance_remote"
        key = f"remote.{self.remote}.url"
        current = self._run(["config", "--get-all", key], timeout=120)
        if current.returncode not in (0, 1):
            self._raise_git_failure(current, "snapshot remote")
        if current.stdout.strip() != remote_url:
            self._git(["config", "--replace-all", key, remote_url], "snapshot remote")
        tip = self._run(["rev-parse", "--verify", "--quiet", f"{SNAPSHOT_REF}^{{commit}}"], timeout=120)
        if tip.returncode != 0 or not tip.stdout.strip():
            return f"snapshot repository {repo} has no commit yet"
        return ""

    def _branch(self) -> str:
        result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], timeout=120)
        if result.returncode == 0:
            return result.stdout.strip()
        # `symbolic-ref --quiet` uses 1 for the expected detached-HEAD case.
        # Any other result is a Git failure, not evidence that the repository
        # has no branch.  In particular, a root process that cannot read a
        # runtime-user checkout must preserve that actionable cause.
        if result.returncode == 1:
            return ""
        self._raise_git_failure(result, "checkpoint branch discovery")

    def _has_remote(self) -> bool:
        remotes = self._git(["remote"], "checkpoint remote").split()
        return self.remote in remotes

    def _remote_execution(self, remote_url: str) -> RemoteExecution:
        """Select a transport once; only the boundary may start remote Git."""
        remote_git = RemoteExecution(remote_url, "checkpoint", instance_dir=self.instance_dir)
        readiness = remote_git.credential_state
        if remote_git.transport != "github-https":
            if remote_git.transport == "https-unsupported":
                self._credential = {
                    "state": "missing/unavailable",
                    "reason": "private instance HTTPS remote is unsupported; only https://github.com is managed",
                    "source": "none",
                }
                raise _GitFailure(
                    "private instance HTTPS remote is unsupported; only https://github.com is managed"
                )
            self._credential = {
                "state": "ambient/manual-bypass",
                "reason": "checkpoint remote is not HTTPS github.com",
                "source": "manual-bypass",
            }
            return remote_git
        self._credential = {"state": readiness.state, "reason": readiness.reason, "source": "managed-store"}
        if not readiness.ready:
            raise _GitFailure(f"managed GitHub credential {readiness.state}: {readiness.reason}")
        return remote_git

    def _remote_head(self, branch: str, remote_git: RemoteExecution) -> str:
        """The remote branch tip, or "" when the remote does not carry it yet."""
        listing = self._remote_git(
            remote_git,
            ["ls-remote", "--heads", self.remote, f"refs/heads/{branch}"],
            "checkpoint ls-remote",
            timeout=PUSH_TIMEOUT_SECONDS,
        )
        for line in listing.splitlines():
            sha = line.split("\t")[0].strip()
            if sha:
                return sha
        return ""

    def _fast_forward(self, remote_head: str, head: str) -> bool:
        """True when the remote tip is already in the local history.

        A tip the local repo has never even seen is divergence, not a missing object.
        """
        known = self._run(["cat-file", "-e", f"{remote_head}^{{commit}}"], timeout=120)
        # Git reports an object absent from this clone as either 1 or 128
        # depending on its version.  That is the expected divergence case;
        # another 128 (for example an ownership/configuration refusal) is a
        # failed probe with a cause the operator needs to see.
        if known.returncode == 1 or "not a valid object name" in self._git_output(known).lower():
            return False
        if known.returncode != 0:
            self._raise_git_failure(known, "checkpoint remote reachability")
        ancestor = self._run(["merge-base", "--is-ancestor", remote_head, head], timeout=120)
        if ancestor.returncode == 0:
            return True
        if ancestor.returncode == 1:
            return False
        self._raise_git_failure(ancestor, "checkpoint ancestry check")

    def _git(self, args: list[str], label: str, *, timeout: float = 120) -> str:
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            self._raise_git_failure(result, label)
        return result.stdout

    def _remote_git(
        self, remote_git: RemoteExecution, args: list[str], label: str, *, timeout: float = 120
    ) -> str:
        try:
            result = remote_git.run_instance(
                self._published.path, self._published.args(args), label=label, timeout=timeout
            )
        except CredentialError as exc:
            raise _GitFailure(str(exc)) from None
        if result.returncode != 0:
            self._raise_git_failure(result, label)
        if remote_git.source:
            self._credential["source"] = remote_git.source
        return result.stdout

    def _run(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return state_repo.run_git(
                self._published.path,
                self._published.args(args),
                label=f"checkpoint {args[0]}",
                timeout=timeout,
            )
        except state_repo.StateRepoError as exc:
            raise _GitFailure(str(exc)) from None

    @staticmethod
    def _raise_git_failure(result: subprocess.CompletedProcess[str], label: str) -> None:
        output = CheckpointPusher._git_output(result)
        detail = output.splitlines()
        raise _GitFailure(f"{label} failed: {detail[-1] if detail else 'git error'}", output)

    @staticmethod
    def _git_output(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr or result.stdout or "").strip()


def is_push_due(state: dict[str, Any], now: float, *, interval_seconds: float) -> bool:
    """The public remote-window predicate shared by production and its fakes.

    A diverged remote is intentionally rechecked promptly, while a regular
    failed delivery retains the normal publication interval. Keeping this
    state-only rule separate lets the dispatcher coordinate preparation
    cadence without growing a second interpretation of the pusher window.
    """
    # A due remote window that was withheld because fresh preparation failed
    # must retry with that preparation on the next bounded dispatcher tick.
    # Treating its recorded attempt as a completed 30-minute window would
    # silently extend the RPO after a local checkpoint failure.
    if state.get("retry_pending"):
        return True
    if state.get("remote_diverged") or state.get("status") == "diverged":
        return True
    attempted = _float_field(state, "attempted_epoch")
    if attempted <= 0:
        return True
    # A clock that jumped backwards must not park the push forever.
    return now < attempted or now - attempted >= interval_seconds


def checkpoint_snapshot(
    instance_dir: Path,
    *,
    write_state: dict[str, Any] | None = None,
    push_state: dict[str, Any] | None = None,
    now: float | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Checkpoint freshness for `status` and `doctor`.

    The commit and lag rows read the repository the pusher publishes: the live root while it is a
    work tree, else the snapshot repository (`data_dir` roots a relative or default location; it
    is read from `instance.yaml` when omitted).
    """
    write = dict(write_state) if isinstance(write_state, dict) else {}
    push = dict(push_state) if isinstance(push_state, dict) else {}
    stamp = time.time() if now is None else float(now)
    write_status = str(write.get("status") or "pending")
    # A pre-cadence payload had one outcome only.  It is deliberately not
    # upgraded into a successful preparation: the coordinator makes the first
    # upgraded tick fresh.  Status still renders the old result honestly.
    successful_at = str(write.get("last_success_at") or "")
    successful_epoch = _float_field(write, "last_success_epoch")
    successful_status = str(write.get("last_success_status") or "")
    if not successful_at and write_status in {"committed", "unchanged"}:
        successful_at = str(write.get("at") or "")
        successful_epoch = _float_field(write, "attempted_epoch")
        successful_status = write_status
    successful_age = (
        max(0, int((stamp - successful_epoch) // 60))
        if successful_epoch > 0
        else (_age_minutes(successful_at, stamp) if successful_at else None)
    )
    failed_at = str(write.get("last_failure_at") or "")
    failed_epoch = _float_field(write, "last_failure_epoch")
    failure_reason = str(write.get("last_failure_reason") or "")
    if not failure_reason and write_status == "blocked":
        failed_at = str(write.get("at") or "")
        failed_epoch = _float_field(write, "attempted_epoch")
        failure_reason = str(write.get("reason") or "")
    failing_since_at = str(write.get("failing_since_at") or "") if failure_reason else ""
    failing_since_epoch = _float_field(write, "failing_since_epoch") if failure_reason else 0.0
    if failure_reason and failing_since_epoch <= 0:
        failing_since_at, failing_since_epoch = failed_at, failed_epoch
    skipped_at = str(write.get("skip_at") or "")
    skipped_epoch = _float_field(write, "skip_epoch")
    next_due_at = str(write.get("next_due_at") or "")
    next_due_epoch = _float_field(write, "next_due_epoch")
    published = published_repository(Path(instance_dir), data_dir)
    commit, commit_at = _last_commit(published)
    pushed = str(push.get("last_push_commit") or "")
    lag_commits, oldest_at = _unpushed(published, pushed)
    attempted_at = str(push.get("attempted_at") or "")
    attempted_epoch = _float_field(push, "attempted_epoch")
    attempt_age = (
        max(0, int((stamp - attempted_epoch) // 60))
        if attempted_epoch > 0
        else (_age_minutes(attempted_at, stamp) if attempted_at else None)
    )
    lag_minutes = _age_minutes(oldest_at, stamp)
    unpublished = _unpublished_minutes(
        lag_minutes,
        failing=bool(failure_reason),
        successful_age=successful_age,
        failing_since_epoch=failing_since_epoch,
        now=stamp,
    )
    rpo_reason = _rpo_reason(
        failure_reason=failure_reason,
        failing_since_at=failing_since_at,
        push=push,
        attempted_at=attempted_at,
    )
    return {
        "last_commit": commit,
        "last_commit_at": commit_at,
        "checkpoint_status": write_status,
        "last_checkpoint_prepared_at": successful_at,
        "last_checkpoint_prepared_epoch": successful_epoch,
        "last_checkpoint_prepared_status": successful_status,
        "last_checkpoint_prepared_commit": str(write.get("last_success_commit") or ""),
        "last_checkpoint_prepared_age_minutes": successful_age,
        # How long the run that `checkpoint_status` describes took. It is overwritten by every
        # outcome the coordinator records, including the cheap not-due decision, so it can never be
        # the previous run's cost carried forward under this run's status.
        "checkpoint_duration_ms": _float_field(write, "duration_ms"),
        "checkpoint_attempted_at": str(write.get("attempted_at") or ""),
        "checkpoint_attempted_epoch": _float_field(write, "attempted_epoch"),
        "checkpoint_skipped_at": skipped_at,
        "checkpoint_skipped_epoch": skipped_epoch,
        "checkpoint_next_due_at": next_due_at,
        "checkpoint_next_due_epoch": next_due_epoch,
        "checkpoint_retry_pending": bool(write.get("retry_pending")),
        "checkpoint_last_failure_at": failed_at,
        "checkpoint_last_failure_epoch": failed_epoch,
        "checkpoint_last_failure_reason": failure_reason,
        "last_push_at": str(push.get("last_push_at") or ""),
        "last_push_commit": pushed,
        "push_attempted_at": attempted_at,
        "push_attempted_epoch": attempted_epoch,
        "push_attempt_age_minutes": attempt_age,
        "push_attempt_freshness": (
            "unknown"
            if attempt_age is None
            else ("fresh" if attempt_age * 60 < PUSH_INTERVAL_SECONDS else "stale")
        ),
        "lag_commits": lag_commits,
        # The RPO exposure is the age of the oldest change the remote lacks, not
        # the time since the last push: a quiet instance with nothing to push is
        # not behind.
        "lag_minutes": lag_minutes,
        # How long no checkpoint has reached the remote: the unpushed lag, or, while preparation
        # fails, the time since the last preparation that succeeded -- a blocked gate commits
        # nothing, so the lag alone would read zero for as long as the gate stays shut.
        "unpublished_minutes": unpublished,
        "rpo_exceeded": unpublished is not None and unpublished > PUSH_INTERVAL_SECONDS // 60,
        "rpo_reason": rpo_reason,
        "checkpoint_failing_since_at": failing_since_at,
        "push_status": str(push.get("status") or "pending"),
        "push_reason": str(push.get("reason") or ""),
        "push_failures": int(_float_field(push, "failures")),
        "remote_diverged": bool(push.get("remote_diverged")),
        "blocked_reason": failure_reason,
        "credential": _credential_snapshot(
            Path(instance_dir), _object_field(push, "credential"), stamp, published=published
        ),
        # Empty while the live root is the published repository, so legacy rows read as before.
        "snapshot_repo": str(published.path) if published is not None and published.bare else "",
    }


def _unpublished_minutes(
    lag_minutes: int | None,
    *,
    failing: bool,
    successful_age: int | None,
    failing_since_epoch: float,
    now: float,
) -> int | None:
    exposure = [lag_minutes] if lag_minutes is not None else []
    if failing:
        if successful_age is not None:
            exposure.append(successful_age)
        elif failing_since_epoch > 0:
            exposure.append(max(0, int((now - failing_since_epoch) // 60)))
    return max(exposure) if exposure else None


def _rpo_reason(
    *, failure_reason: str, failing_since_at: str, push: dict[str, Any], attempted_at: str
) -> str:
    """Why the remote lacks a checkpoint, in the words of the step that stopped it."""
    if failure_reason:
        since = f" since {failing_since_at}" if failing_since_at else ""
        return f"checkpoint gate blocked{since}: {failure_reason}"
    push_reason = str(push.get("reason") or "")
    if push.get("remote_diverged") or push.get("status") == "diverged":
        return f"remote diverged: {push_reason or 'push stopped, resolve by hand'}"
    if push.get("status") == "failed":
        return f"checkpoint push failed at {attempted_at or 'unknown time'}: {push_reason or 'reason unavailable'}"
    if push_reason:
        return f"checkpoint push {push.get('status') or 'pending'}: {push_reason}"
    last = str(push.get("last_push_at") or "")
    return f"no push has delivered the pending commits (last push {last or 'never'})"


def rpo_problem(snapshot: dict[str, Any]) -> str:
    """The sentence of a checkpoint past its RPO, or "" when it is inside it."""
    if not snapshot.get("rpo_exceeded"):
        return ""
    return (
        f"checkpoint has not published for {snapshot.get('unpublished_minutes')} min, past the "
        f"{PUSH_INTERVAL_SECONDS // 60} min RPO: {snapshot.get('rpo_reason') or 'reason unavailable'}"
    )


def _object_field(value: dict[str, Any], name: str) -> dict[str, Any]:
    field = value.get(name)
    return dict(field) if isinstance(field, dict) else {}


def _credential_snapshot(
    instance_dir: Path,
    recorded: dict[str, Any],
    now: float,
    *,
    published: _PublishedRepo | None | bool = True,
) -> dict[str, Any]:
    """Non-secret credential health. A locked store never implies equality.

    The store is the live root's; the remote is the one of the published repository (`True`: the
    live root itself, `None`: unknown).
    """
    if published is True:
        published = _PublishedRepo(Path(instance_dir).expanduser().resolve(), bare=False)
    remote_git = RemoteExecution("", "checkpoint", instance_dir=instance_dir)
    current = remote_git.managed_credential_state
    state = current.state
    reason = current.reason
    transport = "unknown"
    try:
        # Read the declared URL, not `git remote get-url`: the latter applies url.*.insteadOf
        # rewrites and would collapse a separately reported ambient bypass into current managed
        # credential readiness.
        if not isinstance(published, _PublishedRepo):
            raise state_repo.StateRepoError("the published repository is unknown")
        remote = state_repo.git(
            published.path,
            published.args(["config", "--get", f"remote.{DEFAULT_REMOTE}.url"]),
            label="inspect checkpoint remote",
        ).strip()
        remote_git = RemoteExecution(remote, "checkpoint", instance_dir=instance_dir)
        transport = remote_git.transport
    except state_repo.StateRepoError:
        transport = "unknown"
    if recorded.get("state") == "ambient/manual-bypass" or transport in {"local", "ssh", "unmanaged"}:
        state = "ambient/manual-bypass"
        reason = str(recorded.get("reason") or "checkpoint remote is not HTTPS github.com")
    elif transport == "https-unsupported":
        state = "missing/unavailable"
        reason = str(recorded.get("reason") or "private instance HTTPS remote is unsupported")
    verified_at = str(recorded.get("last_verified_at") or "")
    verified_epoch = _float_field(recorded, "last_verified_epoch")
    return {
        "state": state,
        "reason": reason,
        "store": "available"
        if current.ready
        else ("locked" if current.state == "locked/unverifiable" else "unavailable"),
        "source": "encrypted-store"
        if state == "managed-ready"
        else ("remote" if state == "ambient/manual-bypass" else "none"),
        "consumer": "native-git-credential-helper"
        if state == "managed-ready"
        else ("ambient/manual-bypass" if state == "ambient/manual-bypass" else "not-ready"),
        "last_verified_at": verified_at,
        "last_verified_age_minutes": max(0, int((now - verified_epoch) // 60))
        if verified_epoch > 0
        else None,
    }


def render_checkpoint_lines(snapshot: dict[str, Any]) -> list[str]:
    """The freshness block `doctor` prints, indented by its caller."""
    lag_commits = snapshot.get("lag_commits")
    lag_minutes = snapshot.get("lag_minutes")
    lag = "unknown" if lag_commits is None else f"{lag_commits} commit(s)"
    if lag_minutes is not None:
        lag = f"{lag}, {lag_minutes} min"
    lines = [f"snapshot repository: {snapshot['snapshot_repo']}"] if snapshot.get("snapshot_repo") else []
    lines += [
        f"last commit: {snapshot.get('last_commit') or '(none)'} "
        f"{snapshot.get('last_commit_at') or ''}".strip(),
        f"last preparation: {snapshot.get('last_checkpoint_prepared_at') or '(never)'}"
        + (
            f" ({snapshot['last_checkpoint_prepared_age_minutes']} min ago, "
            f"{snapshot.get('last_checkpoint_prepared_status')})"
            if snapshot.get("last_checkpoint_prepared_age_minutes") is not None
            else ""
        ),
        f"checkpoint: {snapshot.get('checkpoint_status') or 'pending'}"
        + f" in {snapshot.get('checkpoint_duration_ms') or 0.0:.0f} ms"
        + (" (retry pending)" if snapshot.get("checkpoint_retry_pending") else ""),
        f"last push: {snapshot.get('last_push_at') or '(never)'}",
        f"last push attempt: {snapshot.get('push_attempted_at') or '(never)'}"
        + (
            f" ({snapshot['push_attempt_age_minutes']} min ago, {snapshot.get('push_attempt_freshness')})"
            if snapshot.get("push_attempt_age_minutes") is not None
            else ""
        ),
        f"lag: {lag}",
        f"push: {snapshot.get('push_status') or 'pending'}",
    ]
    reason = snapshot.get("push_reason")
    if reason:
        lines.append(f"push reason: {reason}")
    skipped = snapshot.get("checkpoint_skipped_at")
    if skipped:
        lines.append(f"checkpoint last skipped: {skipped} (not due)")
    next_due = snapshot.get("checkpoint_next_due_at")
    if next_due:
        lines.append(f"checkpoint next due: {next_due}")
    blocked = snapshot.get("blocked_reason")
    if blocked:
        lines.append(f"blocked: {blocked}")
    if snapshot.get("remote_diverged"):
        lines.append("alarm: remote diverged")
    return lines


def published_repository(instance_dir: Path, data_dir: Path | None = None) -> _PublishedRepo | None:
    """The repository whose branch is published, or None when the snapshot one cannot be named."""
    if live_root_is_work_tree(instance_dir):
        return _PublishedRepo(Path(instance_dir).expanduser().resolve(), bare=False)
    from ummanu.config import DataDirError, instance_data_dir, instance_snapshot_repo

    try:
        root = Path(data_dir) if data_dir is not None else instance_data_dir(instance_dir)
        return _PublishedRepo(instance_snapshot_repo(instance_dir, root), bare=True)
    except DataDirError:
        return None


def snapshot_foreign_commits(instance_dir: Path, data_dir: Path | None = None) -> str:
    """Why the snapshot branch holds history the exporter did not make, or "" when it holds none.

    The finding behind doctor's red `snapshot.foreign_commit`. It is absent ("") in legacy mode and
    while there is no snapshot repository. Otherwise every commit after the takeover marker must
    carry the exporter's author and committer identity and subject prefix, exactly one parent (the
    exporter's root commit none) and a `snapshot-manifest.json`; the tip's manifest must also
    match its tree file by file. Exporter commits without a marker are a finding as well.
    """
    published = published_repository(Path(instance_dir), data_dir)
    if published is None or not published.bare or not (published.path / "HEAD").is_file():
        return ""
    try:
        return _SnapshotAudit(published).problem()
    except state_repo.StateRepoError as exc:
        return f"could not check the snapshot branch in {published.path}: {exc}"


class _SnapshotAudit:
    """Read-only checks of one snapshot repository for `snapshot_foreign_commits`."""

    # How many offending commits a finding names before it counts the rest.
    NAMED = 10

    def __init__(self, published: _PublishedRepo) -> None:
        self.published = published

    def problem(self) -> str:
        repo = self.published.path
        tip = self._resolve(f"{SNAPSHOT_REF}^{{commit}}")
        marker = self._resolve(SNAPSHOT_BASE_REF)
        if not marker:
            if tip and self._has_exporter_commit(tip):
                return (
                    f"snapshot branch in {repo} has exporter commits but no takeover marker "
                    f"{SNAPSHOT_BASE_REF}, so its history cannot be checked"
                )
            return ""
        if not tip:
            return f"snapshot repository {repo} has a takeover marker but no {SNAPSHOT_REF}"
        base = self._git(["cat-file", "blob", marker], "snapshot base").strip()
        if base == SNAPSHOT_BASE_ROOT:
            scope = [tip]
        elif not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", base):
            return f"takeover marker {SNAPSHOT_BASE_REF} in {repo} does not name a commit: {base[:80]!r}"
        elif self._run(["merge-base", "--is-ancestor", base, tip]).returncode != 0:
            return f"snapshot branch in {repo} no longer descends from its takeover base {base[:12]}"
        else:
            scope = [f"{base}..{tip}"]
        records = _commit_fields(
            self._git(["rev-list", "--topo-order", f"--format={_COMMIT_FORMAT}", *scope], "snapshot history")
        )
        manifests = self._manifests([fields[0] for fields in records])
        offending: list[str] = []
        for index, fields in enumerate(records):
            # Only the oldest commit of a history the exporter began from nothing may be a root.
            root_allowed = base == SNAPSHOT_BASE_ROOT and index == len(records) - 1
            reasons = self._commit_reasons(fields, root_allowed=root_allowed, has_manifest=fields[0] in manifests)
            if fields[0] == tip:
                reasons += self._tip_reasons(tip, manifests.get(tip, ""))
            if reasons:
                offending.append(f"{fields[0][:12]} ({', '.join(reasons)})")
        if not offending:
            return ""
        named = "; ".join(offending[: self.NAMED])
        more = f"; and {len(offending) - self.NAMED} more" if len(offending) > self.NAMED else ""
        return f"snapshot branch in {repo} holds {len(offending)} commit(s) the exporter did not make: {named}{more}"

    @staticmethod
    def _commit_reasons(fields: list[str], *, root_allowed: bool, has_manifest: bool) -> list[str]:
        if len(fields) != 7:
            return ["unreadable commit"]
        _sha, parents, author, author_email, committer, committer_email, subject = fields
        reasons: list[str] = []
        if (author, author_email) != (SNAPSHOT_AUTHOR_NAME, SNAPSHOT_AUTHOR_EMAIL):
            reasons.append(f"author {author} <{author_email}>")
        if (committer, committer_email) != (SNAPSHOT_AUTHOR_NAME, SNAPSHOT_AUTHOR_EMAIL):
            reasons.append(f"committer {committer} <{committer_email}>")
        if not subject.startswith(SNAPSHOT_SUBJECT_PREFIX):
            reasons.append("subject without the exporter prefix")
        count = len(parents.split())
        if count != 1 and not (count == 0 and root_allowed):
            reasons.append(f"{count} parents")
        if not has_manifest:
            reasons.append(f"no {SNAPSHOT_MANIFEST}")
        return reasons

    def _tip_reasons(self, tip: str, manifest_oid: str) -> list[str]:
        """The tip's manifest against its tree: the same paths, and each file's digest."""
        if not manifest_oid:
            return []
        listing = self._git(["ls-tree", "-r", "-z", "--full-tree", tip], "snapshot tip tree")
        tree: dict[str, str] = {}
        irregular: list[str] = []
        for entry in filter(None, listing.split("\0")):
            meta, _, path = entry.partition("\t")
            mode, kind, oid = (meta.split() + ["", "", ""])[:3]
            if path == SNAPSHOT_MANIFEST:
                continue
            if kind != "blob" or mode not in (_REGULAR_MODE, _EXECUTABLE_MODE):
                irregular.append(path)
            tree[path] = oid
        if irregular:
            return [f"non-file tree entries {', '.join(sorted(irregular)[:3])}"]
        try:
            manifest = json.loads(self._blobs([manifest_oid])[manifest_oid])
            files = manifest["files"]
            if not isinstance(files, dict):
                raise TypeError("files is not an object")
        except (KeyError, TypeError, ValueError) as exc:
            return [f"unreadable manifest ({type(exc).__name__})"]
        if set(files) != set(tree):
            changed = sorted(set(files) ^ set(tree))
            return [f"manifest and tree list different files: {', '.join(changed[:3])}"]
        blobs = self._blobs(sorted(set(tree.values())))
        changed = sorted(path for path, oid in tree.items() if hashlib.sha256(blobs[oid]).hexdigest() != files[path])
        if changed:
            return [f"manifest digest does not match {', '.join(changed[:3])}"]
        return []

    def _has_exporter_commit(self, tip: str) -> bool:
        for field in ("--author", "--committer"):
            found = self._git(
                ["rev-list", "--max-count=1", "--fixed-strings", f"{field}=<{SNAPSHOT_AUTHOR_EMAIL}>", tip],
                "snapshot exporter commits",
            )
            if found.strip():
                return True
        return False

    def _manifests(self, commits: list[str]) -> dict[str, str]:
        """commit -> blob id of its manifest, for the commits whose tree holds one as a file."""
        if not commits:
            return {}
        output = self._git(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            "snapshot manifests",
            input="".join(f"{commit}:{SNAPSHOT_MANIFEST}\n" for commit in commits),
        )
        found: dict[str, str] = {}
        for commit, line in zip(commits, output.splitlines(), strict=False):
            oid, _, kind = line.partition(" ")
            if kind == "blob":
                found[commit] = oid
        return found

    def _blobs(self, oids: list[str]) -> dict[str, bytes]:
        """The bytes of each blob, read in one `cat-file --batch` (the text runner would alter them)."""
        result = state_repo.run_git_bytes(
            self.published.path,
            self.published.args(["cat-file", "--batch"]),
            label="snapshot blobs",
            input="".join(f"{oid}\n" for oid in oids).encode(),
        )
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise state_repo.StateRepoError(f"snapshot blobs failed: {detail[-1] if detail else 'git error'}")
        payload, offset = result.stdout, 0
        blobs: dict[str, bytes] = {}
        for oid in oids:
            end = payload.index(b"\n", offset)
            header = payload[offset:end].decode("ascii", "replace").split()
            if len(header) != 3 or header[1] != "blob" or not header[2].isdigit():
                raise state_repo.StateRepoError(f"snapshot blobs failed: {oid} is not a blob")
            size = int(header[2])
            blobs[oid] = payload[end + 1 : end + 1 + size]
            offset = end + 1 + size + 1
        return blobs

    def _resolve(self, name: str) -> str:
        result = self._run(["rev-parse", "--verify", "--quiet", name])
        if result.returncode == 1 and not result.stdout.strip():
            return ""
        if result.returncode != 0:
            raise state_repo.StateRepoError(f"could not resolve {name}: {_last_line(result)}")
        return result.stdout.strip()

    def _run(self, args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return state_repo.run_git(
            self.published.path, self.published.args(args), label=f"snapshot doctor {args[0]}", input=input
        )

    def _git(self, args: list[str], label: str, *, input: str | None = None) -> str:
        result = self._run(args, input=input)
        if result.returncode != 0:
            raise state_repo.StateRepoError(f"{label} failed: {_last_line(result)}")
        return result.stdout


def _last_commit(published: _PublishedRepo | None) -> tuple[str, str]:
    if published is None:
        return "", ""
    out = _read_git(published.path, published.args(["log", "-1", "--format=%H %cI", published.ref]))
    parts = out.strip().split(" ", 1)
    if not parts or not parts[0]:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def _unpushed(published: _PublishedRepo | None, pushed: str) -> tuple[int | None, str]:
    """Count the commits the remote lacks and stamp the oldest of them."""
    if published is None:
        return None, ""
    path, head = published.path, published.ref
    # A recorded tip this history no longer holds leaves every commit unpushed,
    # which is the honest reading: nothing local is known to be on the remote.
    known = (
        pushed
        and _read_git(path, published.args(["cat-file", "-e", f"{pushed}^{{commit}}"]), ok_only=True)
        is not None
    )
    scope = [f"{pushed}..{head}"] if known else [head]
    out = _read_git(path, published.args(["log", "--format=%cI", *scope]), ok_only=True)
    if out is None:
        return None, ""
    stamps = [line.strip() for line in out.splitlines() if line.strip()]
    if not stamps:
        return 0, ""
    return len(stamps), stamps[-1]


def _age_minutes(stamp: str, now: float) -> int | None:
    if not stamp:
        return 0
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, int((now - moment.timestamp()) // 60))


def _read_git(instance_dir: Path, args: list[str], *, ok_only: bool = False) -> Any:
    try:
        result = state_repo.run_git(instance_dir, args, label=f"checkpoint snapshot {args[0]}")
    except state_repo.StateRepoError:
        return None if ok_only else ""
    if result.returncode != 0:
        return None if ok_only else ""
    return result.stdout


def _rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _float_field(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except OverflowError:
        return 0.0
    return number if math.isfinite(number) else 0.0


def _publish_board(staging: Path, destination: Path) -> None:
    """Publish the validated flat board cut into `state/board` in the split layout.

    The staging directory keeps the flat files the gate validated, sealed and scanned; what reaches
    the instance repository is their split form (`ummanu.board.checkpoint_layout`), written part by
    part so only what changed becomes a new Git object. The seal leaves first and arrives last, so a
    reader never verifies a half-written cut.
    """
    seal = destination / ANALYTICS_MANIFEST
    try:
        if seal.exists() or seal.is_symlink():
            _remove_path(seal)
    except OSError as exc:
        raise CheckpointBlocked(f"could not unseal checkpoint board: {exc}") from None
    try:
        publish_split_board(staging, destination)
    except CheckpointLayoutError as exc:
        raise CheckpointBlocked(f"could not publish checkpoint board: {exc}") from None
    _publish_component_entries(
        staging, destination, [ANALYTICS_MANIFEST], "checkpoint board", publish_last=ANALYTICS_MANIFEST
    )


def _drop_vanished(destination: Path, entries: tuple[str, ...], staged: tuple[str, ...]) -> None:
    """An optional entry the source no longer has must leave the checkpoint too.

    Otherwise a once-written `events.ndjson` would stay in `state/board` forever and keep getting
    committed as if it were current.
    """
    for entry in entries:
        if entry in staged:
            continue
        stale = destination / entry
        if not stale.exists():
            continue
        try:
            _remove_path(stale)
        except OSError as exc:
            raise CheckpointBlocked(f"could not drop stale {destination.name}/{entry}: {exc}") from None


def _scan_for_secrets(
    staging: Path,
    staged: tuple[str, ...],
    component: str,
    *,
    runtime_env: Path,
    secret_values: tuple[str, ...],
) -> None:
    """`state/` is what leaves the host, so a pasted token stops the commit here."""
    for entry in staged:
        text = _read_text(staging / entry, entry)
        # Scan against this installation rather than the process home default:
        # a recovery/doctor can intentionally point at another instance, and a
        # credential in that instance must still fail closed.
        scrubbed = redact(
            text,
            env_files=[runtime_env],
            secret_values=secret_values,
        )
        if scrubbed != text:
            raise CheckpointBlocked(f"secret detected in state/{component}/{entry}")


def _validate_board(
    staging: Path,
    *,
    instance: Path | None = None,
    registered_project_ids: set[str] | None = None,
) -> None:
    summary = _read_json(staging / "export.json", "board export.json")
    declared = _int_field(summary, "card_count", "board export.json")
    actual = _count_lines(staging / "cards.ndjson", "board cards.ndjson")
    if declared != actual:
        raise CheckpointBlocked(f"board export count mismatch: export.json={declared} cards.ndjson={actual}")
    try:
        cards = _read_ndjson(staging / "cards.ndjson", "board cards.ndjson")
        typed_records = any(
            isinstance(card.get("metadata"), dict)
            and card["metadata"].get("record_type") in {"product", "issue"}
            for card in cards
        )
        if typed_records and registered_project_ids is None and instance is not None:
            try:
                registered_project_ids = registered_projects(instance)
            except TaskError as exc:
                raise CheckpointBlocked(f"cannot validate Product projects: {exc.message}") from None
        from ummanu.board.normalized_checkpoint import NormalizedBoardError, validate_card_records

        validate_card_records(cards, registered_project_ids=registered_project_ids)
    except (ProductIssueValidationError, NormalizedBoardError) as exc:
        raise CheckpointBlocked(f"invalid restorable board record: {exc}") from None
    declared_sprints = _int_field(summary, "sprint_count", "board export.json")
    actual_sprints = _count_lines(staging / "sprints.ndjson", "board sprints.ndjson")
    if declared_sprints != actual_sprints:
        raise CheckpointBlocked(
            f"board sprint count mismatch: export.json={declared_sprints} sprints.ndjson={actual_sprints}"
        )
    events_path = staging / "events.ndjson"
    if events_path.exists():
        _validate_board_events(events_path)


def _validate_board_events(path: Path) -> None:
    """Validate the typed records of the stored file journal; its generic rows pass as they are."""
    for number, record in enumerate(_read_ndjson(path, "board events.ndjson"), start=1):
        if record.get("record_type") != Event.RECORD_TYPE:
            continue
        try:
            Event.from_record(record)
        except ValueError as exc:
            raise CheckpointBlocked(
                f"invalid board protocol event at board events.ndjson line {number}: {exc}"
            ) from None


def _validate_runs(staging: Path) -> None:
    summary = _read_json(staging / "export.json", "runs export.json")
    declared = _int_field(summary, "run_record_count", "runs export.json")
    actual = _count_lines(staging / "runs.ndjson", "runs runs.ndjson")
    if declared != actual:
        raise CheckpointBlocked(f"runs export count mismatch: export.json={declared} runs.ndjson={actual}")

    watermarks = _read_json(staging / "watermarks.json", "runs watermarks.json")
    files = watermarks.get("files")
    if not isinstance(files, list):
        raise CheckpointBlocked("runs watermarks.json has no file list")
    declared = _int_field(summary, "watermark_count", "runs export.json")
    if declared != len(files):
        raise CheckpointBlocked(
            f"runs watermark count mismatch: export.json={declared} watermarks.json={len(files)}"
        )

    claims = _read_json(staging / "claims.json", "runs claims.json")
    entries = claims.get("claims")
    if not isinstance(entries, dict):
        raise CheckpointBlocked("runs claims.json has no claim mapping")
    declared = _int_field(summary, "claim_count", "runs export.json")
    if declared != len(entries):
        raise CheckpointBlocked(
            f"runs claim count mismatch: export.json={declared} claims.json={len(entries)}"
        )


def _count_lines(path: Path, label: str) -> int:
    return sum(1 for line in ndjson_lines(_read_text(path, label)) if line.strip())


def _read_ndjson(path: Path, label: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(ndjson_lines(_read_text(path, label)), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise CheckpointBlocked(f"could not parse {label} line {number}: {exc}") from None
        if not isinstance(record, dict):
            raise CheckpointBlocked(f"{label} line {number} must be an object")
        records.append(record)
    return records


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(_read_text(path, label))
    except ValueError as exc:
        raise CheckpointBlocked(f"could not parse {label}: {exc}") from None
    if not isinstance(payload, dict):
        raise CheckpointBlocked(f"{label} must be an object")
    return payload


def _int_field(payload: dict[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckpointBlocked(f"{label} has no integer {key}")
    return value


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckpointBlocked(f"could not read {label}: {exc}") from None
    except UnicodeError as exc:
        raise CheckpointBlocked(f"could not decode {label}: {exc}") from None
