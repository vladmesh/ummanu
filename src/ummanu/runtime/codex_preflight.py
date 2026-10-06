"""Pre-pane preparation of a workspace for an interactive Codex head.

A Codex TUI blocks on the trust dialog with nobody at the pane, so the answer is written before the
pane exists. Order for every launcher: ensure trust, create the pane, wait for readiness, deliver the
prompt, confirm the turn (the last three are `tui_delivery`). A failure raises here, before any pane.
Fan-out telemetry: `docs/PROTOCOLS.md` "Codex provider-internal fan-out policy".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .head_run_binding import head_run_binding

if TYPE_CHECKING:  # Avoid a runtime import cycle with head.command.
    from .head.run import HeadRun

# The installation-owned home, `<data_dir>/codex-home`.
CODEX_HOME_DATA_DIRNAME = "codex-home"
# Codex's login. Without it in the data-dir home no Codex head of this installation can start.
CODEX_AUTH_FILE = "auth.json"
# Which rung of `resolve_codex_home` answered.
CODEX_HOME_PROFILE = "profile"
CODEX_HOME_ENV = "env"
CODEX_HOME_DATA_DIR = "data-dir"
# The file codex reads trust from, inside the CODEX_HOME the head runs with.
CODEX_CONFIG_FILE = "config.toml"
# Codex's update-check state in the same CODEX_HOME. "Skip until next version" on the update modal
# writes `dismissed_version = latest_version` here, so the modal can be answered before the pane.
CODEX_VERSION_FILE = "version.json"

# What `ensure_codex_update_modal_dismissed` did, as an answer a caller can record.
UPDATE_MODAL_PREVENTED = "prevented"
UPDATE_MODAL_ALREADY_DISMISSED = "already-dismissed"
UPDATE_MODAL_NOT_PENDING = "not-pending"
UPDATE_MODAL_UNPREVENTABLE = "unpreventable"

# Provider-schema protocol version, not a Codex version.
FANOUT_ATTESTATION_VERSION = 1
FANOUT_SCHEMA_ABSENT = "schema_absent"
FANOUT_SCHEMA_UNKNOWN = "schema_unknown"
FANOUT_SCHEMA_ALLOWED = "no_callable_child_spawn_surface"
FANOUT_TERMINAL_CLEAN = "clean"
FANOUT_TERMINAL_UNKNOWN = "unknown"
FANOUT_TERMINAL_VIOLATION = "violation"

EVENT_COLLABORATION_CALL = "collaboration_call"
EVENT_CHILD_THREAD_EDGE = "child_thread_edge"
EVENT_UNKNOWN_THREAD_EDGE = "unknown_thread_edge"
EVENT_UNPARSEABLE_PROVIDER_EVENT = "unparseable_provider_event"
PROVIDER_EVENT_TYPES = (
    EVENT_COLLABORATION_CALL,
    EVENT_CHILD_THREAD_EDGE,
    EVENT_UNKNOWN_THREAD_EDGE,
    EVENT_UNPARSEABLE_PROVIDER_EVENT,
)

# Classifiers never grant allow evidence; unknown tools remain unknown.
KNOWN_COLLABORATION_TOOLS = frozenset(
    {
        "spawn_agent",
        "create_agent",
        "create_child_thread",
        "fork_thread",
        "delegate",
        "collaboration",
        "collaboration_call",
        "wait",
        "wait_agent",
    }
)


class CodexPreflightError(RuntimeError):
    """A workspace could not be prepared; raised only before a pane exists, so nothing launched."""


class CodexHomeLoginMissing(CodexPreflightError):
    """No CODEX_HOME a head may run with: no profile `codex_home`, no `TA_CODEX_HOME`, no data-dir login.

    `home` is the data-dir home the login belongs in, or None when no data dir is named.
    """

    def __init__(self, home: Path | None) -> None:
        self.home = home
        target = "<data_dir>/codex-home" if home is None else shlex.quote(str(home))
        unnamed = "" if home is not None else " (this process names no data dir: UMMANU_DATA_DIR)"
        super().__init__(
            f"no Codex login for this installation{unnamed}: log in under {target} "
            f"(`CODEX_HOME={target} codex login`), or copy an {CODEX_AUTH_FILE} there"
        )


class CodexFanoutPolicyError(CodexPreflightError):
    """The exact Codex run has no independently acceptable no-fan-out attestation."""

    def __init__(self, message: str, *, run: HeadRun) -> None:
        super().__init__(message)
        self.run = run


class CodexFanoutRecordingError(CodexPreflightError):
    """A provider-edge result could not be durably written before a consequential action."""

    def __init__(self, message: str, *, run: HeadRun, event: dict[str, Any]) -> None:
        super().__init__(message)
        self.run = run
        self.event = dict(event)


@dataclass(frozen=True)
class ProviderEventOutcome:
    """One typed provider event and the run state written before its caller acts."""

    run: HeadRun
    event: dict[str, Any]

    @property
    def terminal_state(self) -> str:
        return str(self.run.fanout_policy.get("terminal_state") or FANOUT_TERMINAL_UNKNOWN)

    @property
    def blocked(self) -> bool:
        return self.terminal_state != FANOUT_TERMINAL_CLEAN


@dataclass(frozen=True)
class CodexHome:
    """The CODEX_HOME a head runs with, and which rung of the resolver chose it."""

    path: str
    kind: str


def resolve_codex_home(
    profile: Mapping[str, Any], *, data_dir: str | os.PathLike[str] | None = None
) -> CodexHome:
    """Which CODEX_HOME a head with this profile runs with, resolved at call time.

    In order: the profile's `codex_home`, `TA_CODEX_HOME`, then `<data_dir>/codex-home` when it holds
    a non-empty `auth.json`. Otherwise raises `CodexHomeLoginMissing`; there is no other fallback.
    """
    configured = profile.get("codex_home")
    if configured:
        return CodexHome(str(configured), CODEX_HOME_PROFILE)
    override = os.environ.get("TA_CODEX_HOME")
    if override:
        return CodexHome(override, CODEX_HOME_ENV)
    data_home = data_dir_codex_home(data_dir)
    if data_home is not None and codex_home_logged_in(data_home):
        return CodexHome(str(data_home), CODEX_HOME_DATA_DIR)
    raise CodexHomeLoginMissing(data_home)


def codex_home(profile: Mapping[str, Any], *, data_dir: str | os.PathLike[str] | None = None) -> str:
    """The CODEX_HOME path for this profile; the launch command names the same home."""
    return resolve_codex_home(profile, data_dir=data_dir).path


def data_dir_codex_home(data_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """`<data_dir>/codex-home` of this installation, or None when no data dir is named.

    A named data dir wins, then `UMMANU_DATA_DIR`. This module imports nothing else of `ummanu`, so
    head launchers bind `UMMANU_DATA_DIR` (`ummanu.runtime.codex_home.bound_data_dir`).
    """
    if data_dir is not None:
        return Path(data_dir).expanduser() / CODEX_HOME_DATA_DIRNAME
    configured = os.environ.get("UMMANU_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / CODEX_HOME_DATA_DIRNAME
    return None


def codex_home_logged_in(home: Path) -> bool:
    """Whether a CODEX_HOME holds a login: `auth.json` present and non-empty."""
    try:
        info = (home / CODEX_AUTH_FILE).stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def codex_trust_paths(workspace: str) -> list[str]:
    """The paths codex asks about for a head started in `workspace`.

    The workspace plus, inside a git repo, the repository root codex keys trust on (a worktree
    inherits its repo's answer). Trust overrides and the config write both render from this list.
    """
    workspace_path = Path(workspace).resolve(strict=False)
    paths = [workspace_path]
    repo_root = _codex_repository_trust_root(workspace_path)
    if repo_root is not None:
        paths.append(repo_root)
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def ensure_codex_workspace_trusted(
    profile: Mapping[str, Any],
    workspace: str,
    config: Path | None = None,
) -> None:
    """Record codex trust for one workspace before a head starts in it.

    Launch `-c projects...trust_level` overrides do not reach the dialog (codex 0.145), so trust is
    written to the head's `config.toml`, as codex does for "Yes, continue". Existing trust is kept; a
    path held at another trust level is refused, never overwritten.
    """
    config_path = config or Path(codex_home(profile)) / CODEX_CONFIG_FILE
    text = _read_codex_config(config_path)
    projects = _codex_config_projects(text, config_path)
    additions: list[str] = []
    for target in codex_trust_paths(workspace):
        entry = projects.get(target)
        if entry is None:
            additions.append(target)
            continue
        if not isinstance(entry, dict):
            raise CodexPreflightError(
                f"codex config {config_path} has a non-table project entry for {target}"
            )
        level = str(entry.get("trust_level") or "")
        if level == "trusted":
            continue
        raise CodexPreflightError(
            f"codex config {config_path} keeps {target} at trust_level {level or '(none)'!r}"
        )
    if not additions:
        return
    body = text if text.endswith("\n") or not text else f"{text}\n"
    for target in additions:
        body += f'\n[projects.{json.dumps(target)}]\ntrust_level = "trusted"\n'
    _save_codex_config(config_path, body)


def codex_version_file(profile: Mapping[str, Any]) -> Path:
    """Where the head with this profile keeps its update check."""
    return Path(codex_home(profile)) / CODEX_VERSION_FILE


def ensure_codex_update_modal_dismissed(
    profile: Mapping[str, Any],
    version_file: Path | None = None,
) -> str:
    """Answer codex's update modal before a head starts, as "Skip until next version" would.

    Sets `dismissed_version` to the found `latest_version`; nothing is upgraded or pinned. Best
    effort: returns what it did instead of raising, and the delivery boundary answers the modal on
    screen. A version file that is not a regular file is refused (never follow a symlink).
    """
    path = version_file or codex_version_file(profile)
    reject_symlinked_config(path, "codex version file")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Codex has not run its check under this home yet; there is no pending update to dismiss.
        return UPDATE_MODAL_NOT_PENDING
    except (OSError, UnicodeError):
        return UPDATE_MODAL_UNPREVENTABLE
    try:
        info = json.loads(raw)
    except ValueError:
        return UPDATE_MODAL_UNPREVENTABLE
    if not isinstance(info, dict):
        return UPDATE_MODAL_UNPREVENTABLE
    latest = info.get("latest_version")
    if not isinstance(latest, str) or not latest:
        return UPDATE_MODAL_NOT_PENDING
    if info.get("dismissed_version") == latest:
        return UPDATE_MODAL_ALREADY_DISMISSED
    updated = dict(info)
    updated["dismissed_version"] = latest
    try:
        _save_codex_json(path, updated)
    except CodexPreflightError:
        return UPDATE_MODAL_UNPREVENTABLE
    return UPDATE_MODAL_PREVENTED


def preflight_codex_launch(
    profile: Mapping[str, Any],
    workspace: str,
    run: HeadRun,
    *,
    schema_attestation: Mapping[str, Any] | None = None,
    binary_path: str | None = None,
    config: Path | None = None,
) -> HeadRun:
    """Prepare one exact Codex ``HeadRun`` and attach advisory fan-out telemetry.

    Workspace trust is the only hard pre-pane requirement. The provider source baseline is written
    whenever it can be enumerated, because it fences later provider progress to this HeadRun.
    """
    attested = attest_codex_fanout(
        profile,
        run,
        schema_attestation=schema_attestation,
        binary_path=binary_path,
    )
    # Persist the pre-pane baseline; shared-workspace journals are not run identity.
    try:
        attested = _with_unbound_provider_source(profile, attested)
    except OSError as exc:
        # Telemetry availability never controls launch or delivery.
        attested = _unknown_run(attested, f"cannot establish Codex provider event source baseline: {exc}")
    try:
        ensure_codex_workspace_trusted(profile, workspace, config)
    except CodexPreflightError as exc:
        refused = _unknown_run(attested, f"workspace trust preflight failed: {exc}")
        raise CodexFanoutPolicyError(str(exc), run=refused) from None
    # Not a launch requirement: the delivery boundary answers the modal on screen if it appears.
    try:
        ensure_codex_update_modal_dismissed(profile)
    except CodexPreflightError:
        pass
    return attested


def attest_codex_fanout(
    profile: Mapping[str, Any],
    run: HeadRun,
    *,
    schema_attestation: Mapping[str, Any] | None = None,
    binary_path: str | None = None,
) -> HeadRun:
    """Build a conservative, run-bound provider-schema attestation without opening a pane.

    ``schema_attestation`` must be a provider-schema capture (canonical ``tools`` and digest, binary
    digest, CLI version, model, role); any other mapping is recorded as schema-unknown.
    """
    # Launch configuration cannot promote itself to provider-schema evidence.
    raw = schema_attestation
    if raw is None:
        return _policy_run(
            run,
            state=FANOUT_SCHEMA_ABSENT,
            terminal_state=FANOUT_TERMINAL_UNKNOWN,
            reason="no provider-schema attestation is attached to this Codex launch",
        )
    if not isinstance(raw, Mapping):
        return _unknown_run(run, "provider-schema attestation is not an object")
    schema = dict(raw)
    if schema.get("version") != FANOUT_ATTESTATION_VERSION:
        return _unknown_run(run, "provider-schema attestation has an unsupported version")
    if not str(run.role).strip():
        return _unknown_run(run, "HeadRun has no role to bind provider evidence to")
    if str(schema.get("role") or "") != run.role:
        return _unknown_run(run, "provider-schema attestation role does not match HeadRun")
    model = run.spec.model or ""
    if str(schema.get("model") or "") != model:
        return _unknown_run(run, "provider-schema attestation model does not match HeadRun")
    tools = schema.get("tools")
    if not isinstance(tools, list) or not all(isinstance(tool, Mapping) for tool in tools):
        return _unknown_run(run, "provider-schema attestation has no canonical tool schema")
    try:
        tool_digest = _json_digest(tools)
    except ValueError as exc:
        return _unknown_run(run, str(exc))
    if not _same_digest(schema.get("tool_schema_digest"), tool_digest):
        return _unknown_run(run, "provider-schema tool digest does not match its schema")
    try:
        observed_path, observed_digest, observed_version = _codex_cli_identity(binary_path)
    except OSError as exc:
        return _unknown_run(run, f"cannot attest Codex binary identity: {exc}")
    if not _same_digest(schema.get("binary_digest"), observed_digest):
        return _unknown_run(run, "provider-schema binary digest does not match launched Codex")
    if str(schema.get("cli_version") or "") != observed_version:
        return _unknown_run(run, "provider-schema CLI version does not match launched Codex")
    tool_names = {
        str(tool.get("name") or "").strip().lower() for tool in tools if str(tool.get("name") or "").strip()
    }
    verdict = str(schema.get("provider_schema_verdict") or "")
    if verdict != FANOUT_SCHEMA_ALLOWED:
        return _policy_run(
            run,
            state=FANOUT_SCHEMA_UNKNOWN,
            terminal_state=FANOUT_TERMINAL_UNKNOWN,
            reason="provider schema does not explicitly prove no callable child-spawn surface",
            binary_path=observed_path,
            binary_digest=observed_digest,
            cli_version=observed_version,
            tool_schema_digest=tool_digest,
            provider_schema_verdict=verdict,
        )
    if _has_child_spawn_surface(tool_names):
        return _policy_run(
            run,
            state=FANOUT_SCHEMA_UNKNOWN,
            terminal_state=FANOUT_TERMINAL_UNKNOWN,
            reason="provider schema exposes a callable child-spawn surface",
            binary_path=observed_path,
            binary_digest=observed_digest,
            cli_version=observed_version,
            tool_schema_digest=tool_digest,
            provider_schema_verdict=verdict,
        )
    return _policy_run(
        run,
        state="allowed",
        terminal_state=FANOUT_TERMINAL_CLEAN,
        reason="provider schema proves no callable child-spawn surface",
        binary_path=observed_path,
        binary_digest=observed_digest,
        cli_version=observed_version,
        tool_schema_digest=tool_digest,
        provider_schema_verdict=verdict,
    )


class CodexProviderEventRecorder:
    """Durably append advisory provider-edge evidence to one exact HeadRun.

    Owns no pane and has no screen or transcript fallback. Classifications are diagnostics only and
    never control a head's lifecycle, delivery, replacement or continuation liveness.
    """

    def __init__(
        self,
        run: HeadRun,
        persist: Callable[[HeadRun], None],
        *,
        expected_parent_thread_id: str = "",
    ) -> None:
        self.run = run
        self.persist = persist
        self.expected_parent_thread_id = str(expected_parent_thread_id or "")

    def record(
        self,
        raw_event: Any,
        *,
        source_sequence: int | str | None,
        source_location: str,
        captured_at: str | None = None,
    ) -> ProviderEventOutcome:
        event = _typed_provider_event(
            raw_event,
            expected_parent_thread_id=self.expected_parent_thread_id,
            source_sequence=source_sequence,
            source_location=source_location,
            captured_at=captured_at,
        )
        policy = dict(self.run.fanout_policy)
        events = list(policy.get("events") or [])
        events.append(event)
        policy["events"] = events
        policy["terminal_state"] = (
            FANOUT_TERMINAL_VIOLATION
            if event["policy_outcome"] == FANOUT_TERMINAL_VIOLATION
            else FANOUT_TERMINAL_UNKNOWN
        )
        policy["reason"] = f"provider event {event['type']}: {event['reason']}"
        updated = self.run.with_fanout_policy(policy)
        try:
            self.persist(updated)
        except Exception as exc:
            failed_policy = dict(updated.fanout_policy)
            failed_policy["terminal_state"] = FANOUT_TERMINAL_UNKNOWN
            failed_policy["reason"] = (
                f"provider event could not be durably recorded: {type(exc).__name__}: {exc}"
            )
            failed = updated.with_fanout_policy(failed_policy)
            self.run = failed
            raise CodexFanoutRecordingError(str(failed_policy["reason"]), run=failed, event=event) from None
        self.run = updated
        return ProviderEventOutcome(run=updated, event=event)


def enforce_provider_event(
    recorder: CodexProviderEventRecorder,
    raw_event: Any,
    *,
    source_sequence: int | str | None,
    source_location: str,
    stop: Callable[[HeadRun, str], None],
    block: Callable[[dict[str, Any]], None],
    captured_at: str | None = None,
) -> ProviderEventOutcome:
    """Record provider-edge telemetry; ``stop`` and ``block`` are kept for the callback shape only."""
    del stop, block
    prior_run = recorder.run
    try:
        outcome = recorder.record(
            raw_event,
            source_sequence=source_sequence,
            source_location=source_location,
            captured_at=captured_at,
        )
    except CodexFanoutRecordingError:
        # Keep prior durable state authoritative when telemetry cannot be written.
        return ProviderEventOutcome(run=prior_run, event={})
    return outcome


def reject_symlinked_config(config: Path, kind: str) -> None:
    """Refuse anything but a regular file as a head runtime's config (no symlinks or devices).

    Public because the Claude side of the bring-up writes its config under the same rule.
    """
    try:
        mode = config.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CodexPreflightError(f"cannot inspect {kind} config {config}: {exc}") from None
    if stat.S_ISLNK(mode):
        raise CodexPreflightError(f"refusing symlinked {kind} config {config}")
    if not stat.S_ISREG(mode):
        raise CodexPreflightError(f"{kind} config {config} is not a regular file")


def _read_codex_config(config: Path) -> str:
    reject_symlinked_config(config, "codex")
    try:
        return config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise CodexPreflightError(f"cannot read codex config {config}: {exc}") from None


def _codex_config_projects(text: str, config: Path) -> dict[str, Any]:
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexPreflightError(f"cannot read codex config {config}: {exc}") from None
    projects = loaded.get("projects", {})
    if not isinstance(projects, dict):
        raise CodexPreflightError(f"codex config {config} has a non-table projects value")
    return projects


def _save_codex_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace one of codex's own JSON state files atomically, under the same symlink rule."""
    _atomic_write(path, json.dumps(payload) + "\n", kind="codex version file")


def _save_codex_config(config: Path, text: str) -> None:
    """Replace the codex config with `text` once it parses as TOML.

    Trust tables are appended to the installation's file rather than re-rendered, which can yield
    invalid TOML, so the result is parsed back before it replaces anything.
    """
    _codex_config_projects(text, config)
    _atomic_write(config, text, kind="codex config")


def _atomic_write(path: Path, text: str, *, kind: str) -> None:
    """Replace one file of a head runtime's own state, never following a symlink into it."""
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlinked_config(path, kind)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        reject_symlinked_config(path, kind)
        os.replace(temp_path, path)
    except OSError as exc:
        raise CodexPreflightError(f"cannot update {kind} {path}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _resolve_git_path(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _workspace_git_dir(workspace_path: Path) -> Path | None:
    dotgit = workspace_path / ".git"
    try:
        if dotgit.is_dir():
            return dotgit.resolve(strict=False)
        if dotgit.is_file():
            first = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
        else:
            return None
    except (OSError, IndexError, UnicodeError):
        return None
    if not first.startswith("gitdir:"):
        return None
    return _resolve_git_path(first.split(":", 1)[1].strip(), workspace_path)


def _git_common_dir(git_dir: Path) -> Path:
    common = git_dir / "commondir"
    try:
        if common.is_file():
            value = common.read_text(encoding="utf-8").splitlines()[0].strip()
            if value:
                return _resolve_git_path(value, git_dir)
    except (OSError, IndexError, UnicodeError):
        pass
    return git_dir.resolve(strict=False)


def _codex_repository_trust_root(workspace_path: Path) -> Path | None:
    """Codex' TUI trust check keys linked worktrees by the common git dir's repo root."""
    git_dir = _workspace_git_dir(workspace_path)
    if git_dir is None:
        return None
    common_dir = _git_common_dir(git_dir)
    if common_dir.name != ".git":
        return None
    try:
        if git_dir != common_dir and not git_dir.is_relative_to(common_dir / "worktrees"):
            return None
    except ValueError:
        return None
    return common_dir.parent.resolve(strict=False)


def _policy_run(
    run: HeadRun,
    *,
    state: str,
    terminal_state: str,
    reason: str,
    binary_path: str = "",
    binary_digest: str = "",
    cli_version: str = "",
    tool_schema_digest: str = "",
    provider_schema_verdict: str = "",
) -> HeadRun:
    return run.with_fanout_policy(
        {
            "version": FANOUT_ATTESTATION_VERSION,
            "state": state,
            "terminal_state": terminal_state,
            "reason": reason,
            "run_id": run.run_id,
            "role": run.role,
            "model": run.spec.model or "",
            "binary_path": binary_path,
            "binary_digest": binary_digest,
            "cli_version": cli_version,
            "tool_schema_digest": tool_schema_digest,
            "provider_schema_verdict": provider_schema_verdict,
            "events": [],
        }
    )


def _with_unbound_provider_source(profile: Mapping[str, Any], run: HeadRun) -> HeadRun:
    """Attach the pre-pane session-journal baseline used to bind the new Codex event journal.

    A session JSONL carries no Ummanu run id, so the baseline lets a later lifecycle select exactly one
    new journal and never relabel an older same-workspace session as this run.
    """
    root = Path(codex_home(profile)) / "sessions"
    if root.exists() and not root.is_dir():
        raise OSError(f"Codex session root {root} is not a directory")
    baseline: list[str] = []
    if root.is_dir():
        try:
            baseline = sorted(
                str(path.resolve(strict=False)) for path in root.rglob("*.jsonl") if path.is_file()
            )
        except OSError as exc:
            raise OSError(f"cannot enumerate Codex session root {root}: {exc}") from None
    policy = dict(run.fanout_policy)
    policy["provider_source_required"] = True
    policy["provider_source"] = {
        "version": 1,
        "kind": "codex_session_event_jsonl",
        "state": "unbound",
        # Bind facts before the pane; a journal has no Ummanu run identity.
        **codex_provider_source_descriptor(run),
        "root": str(root.resolve(strict=False)),
        "baseline": baseline,
    }
    return run.with_fanout_policy(policy)


def codex_provider_source_descriptor(run: HeadRun) -> dict[str, Any]:
    """The immutable launch facts every Codex provider journal binding keeps for its lifetime.

    Source binding may append verified journal facts but must carry these values unchanged into every
    bound source, so readers can reject a foreign same-workspace journal.
    """
    return {
        "run_id": run.run_id,
        "head_run_fingerprint": head_run_binding(run.to_json())[1],
        "workspace": str(Path(run.workspace).resolve(strict=False)),
        "role": run.role,
        "task_ref": run.task_ref.to_json(),
    }


def _unknown_run(run: HeadRun, reason: str) -> HeadRun:
    return _policy_run(
        run,
        state=FANOUT_SCHEMA_UNKNOWN,
        terminal_state=FANOUT_TERMINAL_UNKNOWN,
        reason=reason,
    )


def _codex_cli_identity(binary_path: str | None = None) -> tuple[str, str, str]:
    """Hash and query the binary an ordinary ``codex`` launch resolves to.

    The renderer invokes ``codex`` by name, so the attestation binds to that binary.
    """
    candidate = binary_path or shutil.which("codex")
    if not candidate:
        raise OSError("codex executable is not on PATH")
    path = Path(candidate).resolve(strict=True)
    if not path.is_file():
        raise OSError(f"codex executable {path} is not a regular file")
    digest = _file_digest(path)
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"cannot read Codex CLI version: {exc}") from None
    if result.returncode != 0:
        raise OSError(f"Codex CLI version command failed with {result.returncode}")
    version = (result.stdout or result.stderr or "").strip()
    if not version:
        raise OSError("Codex CLI version command returned no version")
    # Preserve the exact attested version; prefixes could misclassify future forms.
    return str(path), digest, version


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"provider schema is not canonical JSON: {exc}") from None
    return hashlib.sha256(encoded).hexdigest()


def _same_digest(supplied: Any, observed: str) -> bool:
    value = str(supplied or "").lower()
    return (
        value == observed.lower() and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
    )


def _has_child_spawn_surface(tool_names: set[str]) -> bool:
    for name in tool_names:
        compact = name.replace("-", "_")
        if name in KNOWN_COLLABORATION_TOOLS:
            return True
        if any(
            fragment in compact
            for fragment in ("spawn", "child_thread", "childagent", "subagent", "delegate")
        ):
            return True
    return False


def _typed_provider_event(
    raw_event: Any,
    *,
    expected_parent_thread_id: str,
    source_sequence: int | str | None,
    source_location: str,
    captured_at: str | None,
) -> dict[str, Any]:
    """Reduce untrusted provider input to the four durable event kinds.

    Raw bytes are kept only as a canonical digest. A malformed object is still an event, never an
    empty result.
    """
    captured = captured_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    supplied_digest = (
        str(raw_event.get("_ummanu_raw_event_digest") or "") if isinstance(raw_event, Mapping) else ""
    )
    raw_digest = (
        supplied_digest if re.fullmatch(r"[0-9a-f]{64}", supplied_digest) else _raw_event_digest(raw_event)
    )
    base = {
        "raw_event_digest": raw_digest,
        "source_sequence": source_sequence,
        "source_location": str(source_location or ""),
        "captured_at": captured,
        "parent_thread_id": "",
        "child_thread_id": "",
        "tool_name": "",
    }
    if source_sequence is None or not str(source_location or ""):
        return dict(
            base,
            type=EVENT_UNPARSEABLE_PROVIDER_EVENT,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event has no source sequence or location",
        )
    if not isinstance(raw_event, Mapping):
        return dict(
            base,
            type=EVENT_UNPARSEABLE_PROVIDER_EVENT,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event is not an object",
        )
    raw = dict(raw_event)
    parent = str(raw.get("parent_thread_id") or raw.get("parentThreadId") or "")
    child = str(raw.get("child_thread_id") or raw.get("childThreadId") or "")
    children = raw.get("child_thread_ids") or raw.get("childThreadIds")
    if isinstance(children, list):
        nonempty = [str(value) for value in children if str(value)]
        if len(nonempty) == 1:
            child = nonempty[0]
        elif nonempty:
            child = ",".join(nonempty)
    tool = str(raw.get("tool_name") or raw.get("tool") or raw.get("name") or "")
    event_type = str(raw.get("type") or raw.get("event_type") or "")
    base.update(parent_thread_id=parent, child_thread_id=child, tool_name=tool)
    if event_type in PROVIDER_EVENT_TYPES:
        declared = event_type
    elif tool or child:
        declared = EVENT_COLLABORATION_CALL if tool else EVENT_CHILD_THREAD_EDGE
    else:
        return dict(
            base,
            type=EVENT_UNPARSEABLE_PROVIDER_EVENT,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event has no recognised collaboration shape",
        )
    if declared == EVENT_UNPARSEABLE_PROVIDER_EVENT:
        return dict(
            base,
            type=declared,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider emitted an unparseable collaboration event",
        )
    if not expected_parent_thread_id or not parent or parent != expected_parent_thread_id:
        return dict(
            base,
            type=EVENT_UNKNOWN_THREAD_EDGE,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event parent identity is absent or does not match this HeadRun",
        )
    if declared == EVENT_UNKNOWN_THREAD_EDGE:
        return dict(
            base,
            type=declared,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider reported an unknown parent or child thread relation",
        )
    if declared == EVENT_COLLABORATION_CALL:
        if not tool or tool.lower() not in KNOWN_COLLABORATION_TOOLS:
            return dict(
                base,
                type=EVENT_COLLABORATION_CALL,
                policy_outcome=FANOUT_TERMINAL_UNKNOWN,
                reason="provider called an unknown collaboration tool",
            )
        return dict(
            base,
            type=EVENT_COLLABORATION_CALL,
            policy_outcome=FANOUT_TERMINAL_VIOLATION,
            reason="provider collaboration call observed",
        )
    # A declared relation is a violation even with a redacted child; empty is unknown.
    if not child:
        return dict(
            base,
            type=EVENT_UNKNOWN_THREAD_EDGE,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider child-thread edge has no child identity",
        )
    return dict(
        base,
        type=EVENT_CHILD_THREAD_EDGE,
        policy_outcome=FANOUT_TERMINAL_VIOLATION,
        reason="provider child-thread edge observed",
    )


def _raw_event_digest(raw_event: Any) -> str:
    try:
        return _json_digest(raw_event)
    except ValueError:
        # Never retain an untrusted repr; the fixed literal keeps the digest stable.
        return _json_digest({"unserialisable_type": type(raw_event).__name__})
