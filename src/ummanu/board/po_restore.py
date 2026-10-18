"""Inert PO import with frozen host bindings and parity in the caller's transaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ummanu._fsutil import directory_lock, fsync_directory, write_bytes_atomic
from ummanu.board.po_checkpoint import PO_COLUMNS, PoCheckpointError, export_po_records, validate_po_records
from ummanu.board.sql_rows import parse_rfc3339, rfc3339
from ummanu.po.runner import runs_dir
from ummanu.po.workspace import workspace_dir

INTERRUPTION_REASON = (
    "Captured running turn interrupted by restore; completion and external effects are unknown; "
    "no automatic retry was performed."
)
_KEYS = {"po_sessions": ("session_id",), "po_turns": ("session_id", "seq"),
         "po_feed": ("entry_id",), "po_requests": ("request_id",)}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class PoRestorePlan:
    """Persist these values before import; retries use the same source, time and native ids."""

    source_sha256: str
    data_dir: str
    interrupted_at: str
    native_bindings: tuple[tuple[str, str | None], ...]


def _plan_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(dict(pairs)) != len(pairs):
        raise PoCheckpointError("PO restore plan JSON has duplicate fields")
    return dict(pairs)


def _plan_constant(value: str) -> None:
    raise PoCheckpointError("PO restore plan JSON has a nonfinite number")


def _load_plan(payload: bytes, records: dict[str, list[dict[str, Any]]], data_dir: Path) -> PoRestorePlan:
    try:
        saved = json.loads(payload.decode("utf-8"), object_pairs_hook=_plan_object,
                           parse_constant=_plan_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise PoCheckpointError("PO restore plan JSON is invalid") from None
    fields = {"version", "source_sha256", "data_dir", "interrupted_at", "native_bindings"}
    if type(saved) is not dict or set(saved) != fields:
        raise PoCheckpointError("PO restore plan fields are invalid")
    if type(saved["version"]) is not int or saved["version"] != 1:
        raise PoCheckpointError("PO restore plan version is unsupported")
    for field in ("source_sha256", "data_dir", "interrupted_at"):
        if type(saved[field]) is not str:
            raise PoCheckpointError(f"PO restore plan field {field} must be a string")
    if not re.fullmatch(r"[0-9a-f]{64}", saved["source_sha256"]):
        raise PoCheckpointError("PO restore plan field source_sha256 is invalid")
    if saved["data_dir"] != str(data_dir):
        raise PoCheckpointError("PO restore plan field data_dir differs from target")
    bindings = saved["native_bindings"]
    if type(bindings) is not list or any(
        type(binding) is not list or len(binding) != 2 or type(binding[0]) is not str
        or (binding[1] is not None and type(binding[1]) is not str) for binding in bindings
    ):
        raise PoCheckpointError("PO restore plan field native_bindings is invalid")
    plan = PoRestorePlan(saved["source_sha256"], saved["data_dir"], saved["interrupted_at"],
                         tuple(tuple(binding) for binding in bindings))
    normalized_po_records(records, plan)
    return plan


def _check_plan_path(info: os.stat_result, *, directory: bool, private: bool) -> None:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (not kind(info.st_mode) or info.st_uid != os.geteuid()
            or (private and stat.S_IMODE(info.st_mode) != mode)
            or (not private and info.st_mode & 0o022)
            or (not directory and info.st_nlink != 1)):
        raise PoCheckpointError("PO restore plan path type, owner or permissions are unsafe")


def prepare_po_restore(records: dict[str, list[dict[str, Any]]], data_dir: Path,
                       *, interrupted_at: datetime) -> PoRestorePlan:
    """Persist or strictly reload one frozen plan under an existing canonical, owned target.

    Only a missing plan permits creation; callers import in a later SQL transaction.
    """
    validate_po_records(records)
    if interrupted_at.tzinfo is None or interrupted_at.utcoffset() is None:
        raise PoCheckpointError("PO restore plan requires an aware interruption time")
    try:
        if not data_dir.is_absolute() or data_dir.resolve(strict=True) != data_dir:
            raise PoCheckpointError("PO restore plan target must be canonical without symlinks")
        _check_plan_path(data_dir.lstat(), directory=True, private=False)
        state = data_dir / ".po-restore"
        state.mkdir(mode=0o700, exist_ok=True)
        _check_plan_path(state.lstat(), directory=True, private=True)
        with directory_lock(state):
            fsync_directory(data_dir)
            path = state / "plan.json"
            try:
                info = path.lstat()
            except FileNotFoundError:
                plan = plan_po_restore(records, data_dir, interrupted_at=interrupted_at)
                payload = {"version": 1, **asdict(plan)}
                write_bytes_atomic(path, (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                     allow_nan=False) + "\n").encode("utf-8"), mode=0o600)
            else:
                _check_plan_path(info, directory=False, private=True)
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as handle:
                    _check_plan_path(os.fstat(handle.fileno()), directory=False, private=True)
                    plan = _load_plan(handle.read(), records, data_dir)
            # A failed directory sync after rename must be completed by the next retry too.
            fsync_directory(state)
            return plan
    except (OSError, RuntimeError) as exc:
        raise PoCheckpointError(f"PO restore plan storage failed ({type(exc).__name__})") from None


def plan_po_restore(records: dict[str, list[dict[str, Any]]], data_dir: Path,
                    *, interrupted_at: datetime) -> PoRestorePlan:
    """Freeze a canonical target and fresh Claude ids; Codex starts without a provider context."""
    validate_po_records(records)
    if interrupted_at.tzinfo is None:
        raise PoCheckpointError("PO restore plan requires an aware interruption time")
    plan = PoRestorePlan(_digest(records), str(data_dir.resolve()),
                         rfc3339(interrupted_at, microseconds=True),
                         tuple((row["session_id"], str(uuid.uuid4()) if row["cli"] == "claude" else None)
                               for row in records["po_sessions"]))
    normalized_po_records(records, plan)
    return plan


def normalized_po_records(records: dict[str, list[dict[str, Any]]],
                          plan: PoRestorePlan) -> dict[str, list[dict[str, Any]]]:
    """Describe every allowed change without accessing source files or generating retry values."""
    validate_po_records(records)
    moment = parse_rfc3339(plan.interrupted_at)
    if (_digest(records) != plan.source_sha256 or not os.path.isabs(plan.data_dir)
            or os.path.normpath(plan.data_dir) != plan.data_dir or moment is None
            or moment.tzinfo is None or rfc3339(moment, microseconds=True) != plan.interrupted_at):
        raise PoCheckpointError("PO restore plan source, target or time is invalid")
    sessions = records["po_sessions"]
    if (type(plan.native_bindings) is not tuple
            or any(type(binding) is not tuple or len(binding) != 2 for binding in plan.native_bindings)
            or tuple(key for key, _ in plan.native_bindings) != tuple(row["session_id"] for row in sessions)):
        raise PoCheckpointError("PO restore plan native binding coverage is invalid")
    bindings = dict(plan.native_bindings)
    old_ids = {row["cli_session_id"] for row in sessions if row["cli_session_id"] is not None}
    fresh_ids = set()
    for row in sessions:
        identity = row["session_id"]
        if Path(identity).name != identity or identity in {".", ".."}:
            raise PoCheckpointError("PO restore plan session path is invalid")
        binding = bindings[identity]
        if row["cli"] == "claude":
            try:
                valid = isinstance(binding, str) and str(uuid.UUID(binding)) == binding
            except ValueError:
                valid = False
            if not valid or binding in old_ids or binding in fresh_ids:
                raise PoCheckpointError("PO restore plan native binding is invalid")
            fresh_ids.add(binding)
        elif binding is not None:
            raise PoCheckpointError("PO restore plan Codex binding must be absent")
    result = json.loads(json.dumps(records))
    for row in result["po_sessions"]:
        row["cwd"] = str(workspace_dir(plan.data_dir))
        row["cli_session_id"] = bindings[row["session_id"]]
    for row in result["po_turns"]:
        row["stdout_path"] = str(runs_dir(plan.data_dir) / row["session_id"]
                                 / f"turn-{row['seq']:04d}.stdout")
        row["pid"] = row["process_identity"] = None
        if row["state"] == "running":
            if moment < parse_rfc3339(row["started_at"]):
                raise PoCheckpointError("PO restore plan interruption precedes turn start")
            reason = INTERRUPTION_REASON if row["reason"] is None else row["reason"] + "\n" + INTERRUPTION_REASON
            row.update(state="interrupted", finished_at=plan.interrupted_at, reason=reason)
    validate_po_records(result)
    return result


def _key(table: str, row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[field] for field in _KEYS[table])


def _safe(value: Any) -> str:
    return "null" if value is None else f"<redacted:{type(value).__name__}>"


def _mismatch(table: str, key: tuple[Any, ...], field: str, expected: Any, actual: Any) -> None:
    raise PoCheckpointError(
        f"PO restore {table} identity {_digest(key)[:16]} field {field}: "
        f"expected={_safe(expected)} actual={_safe(actual)}"
    )


def _row_parity(table: str, expected: dict[str, Any], actual: dict[str, Any]) -> None:
    key = _key(table, expected)
    if set(actual) != set(PO_COLUMNS[table]):
        _mismatch(table, key, "fields", tuple(PO_COLUMNS[table]), tuple(actual))
    for field in PO_COLUMNS[table]:
        if _digest(expected[field]) != _digest(actual[field]):
            _mismatch(table, key, field, expected[field], actual[field])


def require_po_parity(expected: dict[str, list[dict[str, Any]]],
                      actual: dict[str, list[dict[str, Any]]]) -> None:
    """Compare full ordered durable rows, redacting values and hashing identities in diagnostics."""
    if set(actual) != set(PO_COLUMNS):
        raise PoCheckpointError("PO restore table coverage mismatch")
    for table in PO_COLUMNS:
        if len(expected[table]) != len(actual[table]):
            raise PoCheckpointError(f"PO restore {table} row count: expected={len(expected[table])} "
                                    f"actual={len(actual[table])}")
        for source, target in zip(expected[table], actual[table], strict=True):
            _row_parity(table, source, target)


class PoRestoreStore:
    """PO SQL restore over a SqlCardClient, without committing or activating provider contexts."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def export(self) -> dict[str, list[dict[str, Any]]]:
        """Read all durable PO fields on the client's current connection and snapshot."""
        return export_po_records(self.client)

    def restore(self, records: dict[str, list[dict[str, Any]]], plan: PoRestorePlan) -> None:
        """Insert missing identities under table locks, refusing conflicts and all extra target rows."""
        from psycopg.types.json import Jsonb

        if not self.client._depth:
            raise PoCheckpointError("PO restore requires a caller-owned transaction")
        expected = normalized_po_records(records, plan)
        self.client._execute("LOCK TABLE po_sessions, po_turns, po_feed, po_requests "
                             "IN SHARE ROW EXCLUSIVE MODE")
        existing = self.export()
        for table, columns in PO_COLUMNS.items():
            wanted = {_key(table, row): row for row in expected[table]}
            found = {_key(table, row): row for row in existing[table]}
            for key, row in found.items():
                if key not in wanted:
                    _mismatch(table, key, "identity", None, key)
                _row_parity(table, wanted[key], row)
            for key, row in wanted.items():
                if key in found:
                    continue
                values = tuple(Jsonb(row[field]) if field == "metadata" and row[field] is not None
                               else row[field] for field in columns)
                self.client._execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) OVERRIDING SYSTEM VALUE "
                    f"VALUES ({', '.join('%s' for _ in columns)})", values,
                )
        require_po_parity(expected, self.export())
        # Explicit identity inserts bypass nextval; never rewind a sequence, even after rollback.
        if expected["po_feed"]:
            self.client._query(
                "SELECT setval('po_feed_entry_id_seq', GREATEST(last_value, %s), true) "
                "FROM po_feed_entry_id_seq", (expected["po_feed"][-1]["entry_id"],),
            )
