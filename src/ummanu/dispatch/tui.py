"""What the dispatcher reads about its heads' prompts and progress from the provider's own journal.

The delivery vocabulary (`runtime.tui_delivery`) is re-exported for the dispatch modules that
classify a delivery receipt. Nothing here reads a pane: the pane half -- the Orca prompt path and
the screen fallback that guessed a started turn from a pane's text -- is gone with the pane
inventory (A20 step 6, secretary-1723).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ummanu.dispatch.worker_lifecycle import (
    ContinuationProviderCondition,
    head_run_binding,
)
from ummanu.runtime.claude_sessions import (
    claude_project_dir_name,
    claude_session_paths,
)
from ummanu.runtime.codex_home import session_roots
from ummanu.runtime.head import HeadRun, HeadRunError
from ummanu.runtime.paths import env_value
from ummanu.runtime.tui_delivery import (
    COMPOSER_EMPTY,
    COMPOSER_UNKNOWN,
    DELIVERY_ACCEPTED,
    DELIVERY_CONFIRMED,
    DELIVERY_RECEIPT_ACCEPTED,
    DELIVERY_RECEIPT_REFUSED,
    DELIVERY_RECEIPT_UNOBSERVED,
    PRE_DELIVERY_NONE,
    PRE_DELIVERY_STARTING,
    PRE_DELIVERY_UNKNOWN_DIALOG,
    PRE_DELIVERY_UPDATE_MODAL,
    READINESS_BLOCKED,
    READINESS_BUSY,
    READINESS_READY,
    READINESS_STALE_HANDLE,
    READINESS_UNAVAILABLE,
    READINESS_UNKNOWN,
    SENDABILITY_DIALOG_REFUSED,
    SENDABILITY_UNESTABLISHED,
    STAGE_ACKNOWLEDGED,
    STAGE_ENTER_ACCEPTED,
    STAGE_PAYLOAD_WRITTEN,
    STAGE_TURN_OBSERVED,
    DeliveryEvidence,
    TuiDeliveryError,
    delivery_readiness_state,
    delivery_receipt_state,
)

__all__ = [
    "COMPOSER_EMPTY",
    "COMPOSER_UNKNOWN",
    "DELIVERY_ACCEPTED",
    "DELIVERY_CONFIRMED",
    "DELIVERY_RECEIPT_ACCEPTED",
    "DELIVERY_RECEIPT_REFUSED",
    "DELIVERY_RECEIPT_UNOBSERVED",
    "PRE_DELIVERY_NONE",
    "PRE_DELIVERY_STARTING",
    "PRE_DELIVERY_UNKNOWN_DIALOG",
    "PRE_DELIVERY_UPDATE_MODAL",
    "READINESS_BLOCKED",
    "READINESS_BUSY",
    "READINESS_READY",
    "READINESS_STALE_HANDLE",
    "READINESS_UNAVAILABLE",
    "READINESS_UNKNOWN",
    "SENDABILITY_DIALOG_REFUSED",
    "SENDABILITY_UNESTABLISHED",
    "STAGE_ACKNOWLEDGED",
    "STAGE_ENTER_ACCEPTED",
    "STAGE_PAYLOAD_WRITTEN",
    "STAGE_TURN_OBSERVED",
    "DeliveryEvidence",
    "TuiDeliveryError",
    "bind_claude_provider_progress_source",
    "claude_project_dir_name",
    "delivery_readiness_state",
    "delivery_receipt_state",
    "latest_claude_user_turn_for",
    "latest_user_turn_for",
    "prepare_claude_provider_progress_source",
    "provider_progress_for_run",
    "provider_turn_started",
]


def provider_turn_started(
    workspace: str,
    since: float,
    *,
    adapter: str,
    session_root: Path | None = None,
) -> bool | None:
    """Whether the provider itself recorded a user turn in this workspace after ``since``.

    Three answers, because two would have to lie about one of them: ``True`` is a turn the provider
    wrote down, ``False`` is a journal that exists and does not hold one yet, and ``None`` is no
    journal to read — an adapter that keeps none, or a head whose sessions are not where this
    dispatcher looks. Only the third leaves a caller with no proof either way.
    """
    if not workspace or not since:
        return None
    if adapter == "claude":
        if not any(_claude_session_paths_for(workspace)):
            return None
        return bool(latest_claude_user_turn_for(workspace, since))
    if adapter == "codex":
        if not any(_session_paths_for(workspace, session_root=session_root)):
            return None
        return bool(latest_user_turn_for(workspace, since, session_root=session_root))
    return None


def latest_user_turn_for(
    workspace: str,
    since: float,
    *,
    session_root: Path | None = None,
) -> float | None:
    latest: float | None = None
    for path in _session_paths_for(workspace, session_root=session_root):
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or not _is_user_turn(record):
                        continue
                    timestamp = _record_timestamp(record)
                    if timestamp is None or timestamp <= since:
                        continue
                    if latest is None or timestamp > latest:
                        latest = timestamp
        except OSError:
            continue
    return latest


def latest_claude_user_turn_for(workspace: str, since: float) -> float | None:
    """Return the newest Claude user record for this workspace after ``since``."""
    latest: float | None = None
    for path in _claude_session_paths_for(workspace):
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "user":
                        continue
                    timestamp = _record_timestamp(record)
                    if timestamp is None or timestamp <= since:
                        continue
                    if latest is None or timestamp > latest:
                        latest = timestamp
        except OSError:
            continue
    return latest


def prepare_claude_provider_progress_source(run: HeadRun) -> HeadRun:
    """Persist the pre-pane Claude baseline for this exact HeadRun.

    The provider journal does not carry a Ummanu run id, so selecting a transcript has to start
    with the files that existed before this run's pane was opened. A missing root is a durable
    unavailable source, not permission to inspect the workspace later.
    """
    if run.spec.adapter != "claude":
        return run
    run_id, run_fingerprint = head_run_binding(run.to_json())
    root = _claude_projects_root()
    baseline: list[str] = []
    if root.is_dir():
        try:
            baseline = sorted(
                str(path.resolve(strict=False))
                for path in claude_session_paths(run.workspace, root=root)
                if path.is_file()
            )
        except OSError:
            baseline = []
    policy = dict(run.fanout_policy)
    policy["provider_progress_source"] = {
        "version": 1,
        "kind": "claude_session_jsonl",
        # Claude may create its projects root only when the pane first starts.  The empty
        # pre-pane baseline is still a real launch baseline; later binding accepts exactly one
        # new transcript beneath this recorded root and never scans another location.
        "state": "unbound",
        "reason": "",
        # The pane and its JSONL are not one atomic provider operation.  Retain a small retry
        # budget on the exact run instead of permanently downgrading the first empty scan.
        "bind_attempts": 0,
        "bind_max_attempts": 3,
        "run_id": run_id,
        "head_run_fingerprint": run_fingerprint,
        "workspace": str(Path(run.workspace).resolve(strict=False)),
        "role": run.role,
        "task_ref": run.task_ref.to_json(),
        "root": str(root.resolve(strict=False)),
        "baseline": baseline,
    }
    return run.with_fanout_policy(policy)


def bind_claude_provider_progress_source(run: HeadRun) -> HeadRun:
    """Boundedly bind one exact-run Claude transcript without a workspace fallback.

    Claude can create the transcript and write its session id after its pane comes up.  A missing
    file or temporarily blank session id is therefore retried a small, durable number of times;
    ambiguity and every identity failure remain terminal rather than choosing another workspace
    session.
    """
    source = _progress_source(run)
    pending_states = {"unbound", "awaiting_transcript", "awaiting_session_id"}
    if source.get("kind") != "claude_session_jsonl" or source.get("state") not in pending_states:
        return run
    reason = _source_matches_run(source, run)
    if reason:
        return _replace_progress_source(run, {**source, "state": "unavailable", "reason": reason})
    root = Path(str(source.get("root") or ""))
    baseline = source.get("baseline")
    if not isinstance(baseline, list) or not all(isinstance(path, str) for path in baseline):
        return _replace_progress_source(
            run,
            {
                **source,
                "state": "unavailable",
                "reason": "Claude source baseline is unavailable or malformed",
            },
        )
    if not root.is_dir():
        return _retry_claude_binding(source, run, "Claude source root is absent after launch")
    if source.get("state") == "awaiting_session_id":
        candidate = _pending_claude_candidate(source, root)
        if candidate is None:
            return _replace_progress_source(
                run,
                {
                    **source,
                    "state": "unavailable",
                    "reason": "Claude pending provider source cannot be verified",
                },
            )
    else:
        try:
            root_resolved = root.resolve(strict=True)
            candidates = [
                path.resolve(strict=True)
                for path in claude_session_paths(run.workspace, root=root)
                if str(path.resolve(strict=False)) not in set(baseline)
            ]
            candidates = [path for path in candidates if path.is_relative_to(root_resolved)]
        except (OSError, ValueError):
            return _replace_progress_source(
                run,
                {
                    **source,
                    "state": "unavailable",
                    "reason": "Claude source cannot be enumerated",
                },
            )
        if not candidates:
            return _retry_claude_binding(source, run, "Claude source is absent after launch")
        if len(candidates) != 1:
            return _replace_progress_source(
                run,
                {
                    **source,
                    "state": "unavailable",
                    "reason": "Claude source is ambiguous after launch",
                },
            )
        candidate = candidates[0]
    try:
        stat = candidate.stat()
    except OSError:
        return _replace_progress_source(
            run,
            {
                **source,
                "state": "unavailable",
                "reason": "Claude source cannot be inspected",
            },
        )
    session_id = _claude_session_id(candidate)
    if not session_id:
        pending = {
            **source,
            "state": "awaiting_session_id",
            "path": str(candidate),
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "initial_size": int(stat.st_size),
            "initial_mtime_ns": int(stat.st_mtime_ns),
        }
        return _retry_claude_binding(pending, run, "Claude source has no session id yet")
    return _replace_progress_source(
        run,
        {
            **source,
            "state": "bound",
            "path": str(candidate),
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "initial_size": int(stat.st_size),
            "initial_mtime_ns": int(stat.st_mtime_ns),
            "session_id": session_id,
        },
    )


def _retry_claude_binding(source: dict[str, Any], run: HeadRun, reason: str) -> HeadRun:
    """Persist one bounded retry without weakening the run fence."""
    try:
        attempts = int(source.get("bind_attempts", 0)) + 1
        maximum = int(source.get("bind_max_attempts", 3))
    except (TypeError, ValueError):
        return _replace_progress_source(
            run,
            {**source, "state": "unavailable", "reason": "Claude source retry budget is malformed"},
        )
    if maximum < 1 or attempts >= maximum:
        return _replace_progress_source(
            run,
            {
                **source,
                "state": "unavailable",
                "bind_attempts": attempts,
                "bind_max_attempts": maximum,
                "reason": reason,
            },
        )
    return _replace_progress_source(
        run,
        {
            **source,
            "state": (
                "awaiting_transcript"
                if source.get("state") != "awaiting_session_id"
                else "awaiting_session_id"
            ),
            "bind_attempts": attempts,
            "bind_max_attempts": maximum,
            "reason": reason,
        },
    )


def _pending_claude_candidate(source: dict[str, Any], root: Path) -> Path | None:
    """Reopen the one candidate selected before its asynchronous session id arrived."""
    try:
        root_resolved = root.resolve(strict=True)
        candidate = Path(str(source.get("path") or "")).resolve(strict=True)
        stat = candidate.stat()
        if (
            not candidate.is_relative_to(root_resolved)
            or candidate.suffix != ".jsonl"
            or int(source.get("device")) != int(stat.st_dev)
            or int(source.get("inode")) != int(stat.st_ino)
            or int(source.get("initial_size")) > int(stat.st_size)
        ):
            return None
        return candidate
    except (OSError, TypeError, ValueError):
        return None


def _claude_session_id(path: Path) -> str:
    """Read the jsonl session identifier without treating its absence as a bad transcript."""
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                for key in ("sessionId", "session_id"):
                    value = record.get(key)
                    if isinstance(value, str) and value:
                        return value
                payload = record.get("payload")
                if isinstance(payload, dict):
                    for key in ("sessionId", "session_id"):
                        value = payload.get(key)
                        if isinstance(value, str) and value:
                            return value
    except OSError:
        return ""
    return ""


def provider_progress_for_run(run: HeadRun) -> dict[str, str]:
    """Resolve one exact run-bound provider source, never a workspace activity maximum."""
    run_json = run.to_json()
    run_id, fingerprint = head_run_binding(run_json)
    if not run_id:
        return {"state": "unavailable", "reason": "persisted HeadRun is malformed"}
    if run.spec.adapter == "codex":
        return _codex_provider_progress_for_run(run, run_id, fingerprint)
    if run.spec.adapter == "claude":
        return _claude_provider_progress_for_run(run, run_id, fingerprint)
    return {"state": "unavailable", "reason": "provider adapter has no progress source contract"}


def provider_progress_for_persisted_run(run: Any) -> dict[str, str]:
    """The same run-bound cursor read, for a caller holding only a record's HeadRun payload.

    Read-only by construction: a persisted run is promoted and asked for its cursor, and nothing is
    rebound or written back. A payload that is not a HeadRun is a channel that cannot answer, which
    is a statement about the channel and never about the head.
    """
    try:
        return provider_progress_for_run(HeadRun.from_json(run))
    except (HeadRunError, AttributeError, KeyError, TypeError, ValueError):
        return {"state": "unavailable", "reason": "persisted HeadRun is unavailable"}


def _codex_provider_progress_for_run(run: HeadRun, run_id: str, fingerprint: str) -> dict[str, str]:
    bound = _codex_bound_source(run)
    if isinstance(bound, dict):
        return bound
    source, _path, stat, lines = bound
    cursor = f"{lines[-1].number}:{lines[-1].digest}"
    return _observed(
        "codex-session",
        _source_fingerprint(source),
        cursor,
        stat.st_mtime,
        run_id,
        fingerprint,
    )


def _codex_bound_source(
    run: HeadRun,
) -> tuple[dict[str, Any], Path, os.stat_result, list[Any]] | dict[str, str]:
    """This run's bound Codex session journal, re-proved and read, or the typed reason it is not.

    The one verification both run-bound Codex readers share: the provider cursor and the first-turn
    provider-failure reader (secretary-1799) must never disagree about which journal is this head's.
    """
    from ummanu.codex_provider_events import _initial_range_matches, _read_source

    source = run.fanout_policy.get("provider_source")
    if not isinstance(source, dict):
        return _unavailable("Codex provider source has no bound v1 baseline", "codex-session")
    if source.get("state") == "unbound":
        if _legacy_unbound_v1_codex_source(source, run):
            return _unavailable(
                "Codex provider source has no bound v1 baseline",
                "codex-session",
                continuation_condition=ContinuationProviderCondition.LEGACY_UNBOUND_V1,
            )
        return _unavailable("Codex provider source has no bound v1 baseline", "codex-session")
    if source.get("state") != "bound":
        return _unavailable("Codex provider source has no bound v1 baseline", "codex-session")
    reason = _source_matches_run(source, run)
    if reason:
        return _unavailable(reason, "codex-session", identity=True)
    root = Path(str(source.get("root") or ""))
    path = Path(str(source.get("path") or ""))
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
        if not path_resolved.is_relative_to(root_resolved) or path_resolved.suffix != ".jsonl":
            raise OSError
        parsed = _read_source(path_resolved)
        if parsed is None or not _initial_range_matches(source, parsed[1]):
            raise OSError
        stat = path_resolved.stat()
    except (OSError, ValueError):
        return _unavailable("Codex bound provider source cannot be verified", "codex-session")
    meta, lines = parsed
    if str(meta.get("session_id") or "") != str(source.get("session_id") or "") or not lines:
        return _unavailable("Codex bound provider source identity changed", "codex-session", identity=True)
    return source, path_resolved, stat, lines


def _claude_provider_progress_for_run(run: HeadRun, run_id: str, fingerprint: str) -> dict[str, str]:
    bound = _claude_bound_source(run)
    if isinstance(bound, dict):
        return bound
    source, _path, stat = bound
    cursor = f"{int(stat.st_size)}:{int(stat.st_mtime_ns)}"
    return _observed(
        "claude-session",
        _source_fingerprint(source),
        cursor,
        stat.st_mtime,
        run_id,
        fingerprint,
    )


def _claude_bound_source(run: HeadRun) -> tuple[dict[str, Any], Path, os.stat_result] | dict[str, str]:
    """This run's bound Claude session transcript, re-proved, or the typed reason it is not."""
    source = _progress_source(run)
    if source.get("kind") != "claude_session_jsonl" or source.get("state") != "bound":
        return _unavailable("Claude provider source has no bound v1 baseline", "claude-session")
    reason = _source_matches_run(source, run)
    if reason:
        return _unavailable(reason, "claude-session", identity=True)
    root = Path(str(source.get("root") or ""))
    path = Path(str(source.get("path") or ""))
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
        stat = path_resolved.stat()
        allowed = {
            candidate.resolve(strict=True) for candidate in claude_session_paths(run.workspace, root=root)
        }
        if (
            not path_resolved.is_relative_to(root_resolved)
            or path_resolved not in allowed
            or int(source.get("device")) != int(stat.st_dev)
            or int(source.get("inode")) != int(stat.st_ino)
            or int(source.get("initial_size")) > int(stat.st_size)
        ):
            raise OSError
    except (OSError, TypeError, ValueError):
        return _unavailable("Claude bound provider source cannot be verified", "claude-session")
    return source, path_resolved, stat


def _progress_source(run: HeadRun) -> dict[str, Any]:
    source = run.fanout_policy.get("provider_progress_source")
    return dict(source) if isinstance(source, dict) else {}


def _replace_progress_source(run: HeadRun, source: dict[str, Any]) -> HeadRun:
    policy = dict(run.fanout_policy)
    policy["provider_progress_source"] = source
    return run.with_fanout_policy(policy)


def _source_matches_run(source: dict[str, Any], run: HeadRun) -> str:
    run_id, fingerprint = head_run_binding(run.to_json())
    if (
        source.get("version") != 1
        or str(source.get("run_id") or "") != run_id
        or str(source.get("head_run_fingerprint") or "") != fingerprint
        or str(source.get("workspace") or "") != str(Path(run.workspace).resolve(strict=False))
        or str(source.get("role") or "") != run.role
        or source.get("task_ref") != run.task_ref.to_json()
    ):
        return "provider source binding does not match this HeadRun"
    return ""


def _source_fingerprint(source: dict[str, Any]) -> str:
    stable = {
        key: source.get(key)
        for key in (
            "version",
            "kind",
            "run_id",
            "head_run_fingerprint",
            "workspace",
            "role",
            "task_ref",
            "root",
            "path",
            "session_id",
            "parent_thread_id",
            "initial_range",
            "device",
            "inode",
            "initial_size",
            "initial_mtime_ns",
        )
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:32]


def _observed(
    source: str,
    source_fingerprint: str,
    cursor: str,
    observed_at: float,
    run_id: str,
    run_fingerprint: str,
) -> dict[str, str]:
    return {
        "state": "observed",
        "admission": "accepted",
        "source": source,
        "source_fingerprint": source_fingerprint,
        "cursor": cursor,
        "observed_at": str(float(observed_at)),
        "head_run_id": run_id,
        "head_run_fingerprint": run_fingerprint,
    }


def _legacy_unbound_v1_codex_source(source: dict[str, Any], run: HeadRun) -> bool:
    """Whether ``source`` is the exact v1 preflight descriptor left unbound on a legacy run.

    Checks structure and the HeadRun fence rather than a diagnostic string: an unbound descriptor
    whose baseline is malformed, or whose immutable fields belong to another run, cannot start a
    replacement.
    """
    root = source.get("root")
    baseline = source.get("baseline")
    return (
        run.spec.adapter == "codex"
        and run.fanout_policy.get("provider_source_required") is True
        and source.get("version") == 1
        and source.get("kind") == "codex_session_event_jsonl"
        and source.get("state") == "unbound"
        and _is_canonical_absolute_path(root)
        and isinstance(baseline, list)
        and all(
            _is_canonical_absolute_path(path) and Path(path).is_relative_to(Path(root)) for path in baseline
        )
        and not _source_matches_run(source, run)
    )


def _is_canonical_absolute_path(value: Any) -> bool:
    """Whether a persisted preflight path has the exact spelling its constructor writes."""
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        return path.is_absolute() and str(path.resolve(strict=False)) == value
    except (OSError, RuntimeError, ValueError):
        return False


def _unavailable(
    reason: str,
    source: str,
    *,
    identity: bool = False,
    continuation_condition: ContinuationProviderCondition | None = None,
) -> dict[str, str]:
    result = {
        "state": "identity_mismatch" if identity else "unavailable",
        "source": source,
        "reason": reason,
    }
    if continuation_condition is not None:
        result["continuation_condition"] = continuation_condition.value
    return result


def _claude_session_paths_for(workspace: str):
    return claude_session_paths(workspace, root=_claude_projects_root())


def _claude_projects_root() -> Path:
    configured = env_value("UMMANU_CLAUDE_PROJECTS", nonempty=True)
    return Path(configured) if configured else Path.home() / ".claude" / "projects"


def _session_paths_for(workspace: str, *, session_root: Path | None = None):
    roots = [session_root] if session_root is not None else _sessions_roots()
    wanted = str(Path(workspace).resolve(strict=False))
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for day_dir in _recent_day_dirs(root):
            try:
                files = list(day_dir.glob("*.jsonl"))
            except OSError:
                continue
            for path in files:
                key = path.resolve(strict=False)
                if key in seen or _session_cwd(path) != wanted:
                    continue
                seen.add(key)
                yield path


def _sessions_roots() -> list[Path]:
    """`UMMANU_CODEX_SESSIONS` (legacy `TA_CODEX_SESSIONS`) alone when set, else every home a live Codex
    head may be writing into (`codex_home.session_roots`), not only the current one."""
    root = env_value("UMMANU_CODEX_SESSIONS", nonempty=True)
    if root:
        return [Path(root)]
    return session_roots()


def _session_cwd(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for index, line in enumerate(source):
                if index >= 20:
                    return None
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                cwd = payload.get("cwd") or record.get("cwd")
                return cwd if isinstance(cwd, str) and cwd else None
    except OSError:
        return None
    return None


def _recent_day_dirs(root: Path, limit: int = 2) -> list[Path]:
    try:
        years = sorted(
            (path for path in root.iterdir() if path.is_dir() and path.name.isdigit()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return [root]
    if not years:
        return [root]
    days: list[Path] = []
    for year in years:
        try:
            months = sorted(
                (path for path in year.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True
            )
        except OSError:
            continue
        for month in months:
            try:
                leaves = sorted(
                    (path for path in month.iterdir() if path.is_dir()),
                    key=lambda path: path.name,
                    reverse=True,
                )
            except OSError:
                continue
            for day in leaves:
                days.append(day)
                if len(days) >= limit:
                    return days
    return days or [root]


def _record_timestamp(record: dict[str, Any]) -> float | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _is_user_turn(record: dict[str, Any]) -> bool:
    """Whether one Codex rollout record is a user turn, in either shape Codex writes them.

    `event_msg`/`user_message` is what `codex exec` writes. The interactive Codex TUI never
    writes it: at cli 0.147.0 the same submission is persisted as `response_item`/`message` with
    `role: "user"`, and only the `originator` in the session header tells the two apart. Reading
    the first shape alone therefore answered "no user turn" for every interactive head this product
    runs — checked across the eleven observer sessions of 2026-08-15, where `user_message` appears
    0 times and the user-role message 5 to 19 times — and left the durable half of the delivery
    proof dead while the screen fallback quietly carried it.

    A user-role message is written when a turn begins, so it is proof of a submission and not of a
    session merely existing: a rollout whose head was never prompted holds developer records and
    nothing else.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    if record.get("type") == "event_msg":
        return payload.get("type") == "user_message"
    return (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    )
