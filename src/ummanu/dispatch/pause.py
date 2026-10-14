"""The production dispatcher's pause flag, and the legacy flag it mirrors itself into.

`ProductionPause` is the working pause, next to the production dispatcher's state in the live
data plane (`<data_dir>/dispatcher/pause.json`). The legacy flag under the pipeline worktree is
still read by the background roles, so a pause mirrors itself there and a resume removes that
mirror again — but only when the pause wrote it, never a file that was already on disk.

Semantics:

  drain  — no new claims and no background-role dispatch; cards already in flight keep riding
           their cycle to the end.
  freeze — drain, plus the live worker and reviewer heads are stopped and the tick advances
           nothing at all. Workspaces and worktrees are never removed.

A freeze set by automation carries a TTL and is resumed by the next tick once it outlives
`UMMANU_HARD_PAUSE_AUTO_RESUME_TTL_S`, because `ummanu backup create` resumes in its `finally` and
a backup killed before that block would otherwise leave the dispatcher frozen forever. A freeze
held by a human is a maintenance window and never expires.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from ummanu._fsutil import write_json
from ummanu.runtime.paths import env_value


class PauseMode(StrEnum):
    """The two durable production-pause modes."""

    DRAIN = "drain"
    FREEZE = "freeze"


class LegacyPauseMirror(TypedDict, total=False):
    """Receipt for the best-effort legacy pause mirror."""

    path: str
    written: bool
    cleared: bool
    reason: str


class PauseState(TypedDict, total=False):
    """The durable ``dispatcher/pause.json`` document.

    The type describes the stable in-process contract while the persisted representation stays the
    same JSON object. ``corrupt`` is synthetic and is returned only when the file cannot be read.
    """

    version: int
    mode: PauseMode
    since: str
    actor: str
    reason: str
    stopped_worker: list[str]
    stopped_reviewer: list[str]
    stopped_observer: list[str]
    excluded_worker: list[str]
    legacy_mirror: LegacyPauseMirror
    corrupt: bool


class AutoResumeStatus(TypedDict, total=False):
    """Decision document describing whether an automation freeze has expired."""

    eligible: bool
    ttl_seconds: int
    reason: str
    actor: str
    age_seconds: int


PAUSE_MODES: tuple[PauseMode, ...] = tuple(PauseMode)
AUTO_RESUME_TTL_DEFAULT = 2700
AUTO_RESUME_ACTORS_DEFAULT = "pipeline,ummanu-backup,ummanu,steward,curator,retro"
_PAUSE_MODE_ALIASES: dict[str, PauseMode] = {
    "drain": PauseMode.DRAIN,
    "soft": PauseMode.DRAIN,
    "freeze": PauseMode.FREEZE,
    "hard": PauseMode.FREEZE,
}
_LEGACY_MODES: dict[PauseMode, str] = {PauseMode.DRAIN: "soft", PauseMode.FREEZE: "hard"}


def normalize_pause_mode(mode: str | None) -> PauseMode | Literal[""]:
    """Public mode for a requested one, ``""`` when it is not a pause mode.

    The legacy ``soft``/``hard`` spellings keep parsing: operators and runbooks still carry them.
    """
    return _PAUSE_MODE_ALIASES.get(str(mode or "").strip().lower(), "")


def auto_resume_ttl_seconds() -> int:
    """TTL for an automation-owned freeze, 0 when auto-resume is turned off."""
    try:
        ttl = int(env_value("UMMANU_HARD_PAUSE_AUTO_RESUME_TTL_S", str(AUTO_RESUME_TTL_DEFAULT)))
    except ValueError:
        ttl = AUTO_RESUME_TTL_DEFAULT
    return max(0, ttl)


def auto_resume_actors() -> tuple[str, ...]:
    """Actors whose freeze expires. Anything else is a human maintenance window and is held."""
    raw = env_value("UMMANU_HARD_PAUSE_AUTO_RESUME_ACTORS", AUTO_RESUME_ACTORS_DEFAULT)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def auto_resume_status(state: PauseState, *, now: float | None = None) -> AutoResumeStatus:
    """Whether this pause state is an automation-owned freeze that has outlived its TTL.

    A freeze without a readable `since` counts as expired rather than eternal: the failure mode being
    fixed here is a freeze nobody lifts.
    """
    ttl = auto_resume_ttl_seconds()
    out: AutoResumeStatus = {"eligible": False, "ttl_seconds": ttl, "reason": "not-freeze"}
    if normalize_pause_mode(state.get("mode")) != PauseMode.FREEZE:
        return out
    if ttl <= 0:
        out["reason"] = "disabled"
        return out
    actor = str(state.get("actor") or "").strip()
    out["actor"] = actor
    if actor not in auto_resume_actors():
        out["reason"] = "manual-or-unknown-actor"
        return out
    since = _parse_since(state.get("since"))
    if since is None:
        out["eligible"] = True
        out["reason"] = "missing-or-invalid-since"
        return out
    age = max(0, int((now if now is not None else time.time()) - since))
    out["age_seconds"] = age
    if age < ttl:
        out["reason"] = "fresh"
        return out
    out["eligible"] = True
    out["reason"] = "stale-automation-freeze"
    return out


def _parse_since(value: str | None) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class ProductionPause:
    """The pause flag the production tick reads. Absent file = running."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "dispatcher"
        self.path = self.root / "pause.json"

    def load(self) -> PauseState:
        """State, or ``{}`` when the pause is not set.

        A corrupt file is read as a freeze: continuing a dispatch while an operator's stop state cannot
        be read is worse than deferring it. `summary()` and `status()` carry an explicit warning.
        """
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError):
            return {"corrupt": True, "mode": PauseMode.FREEZE}
        if not isinstance(payload, dict):
            return {"corrupt": True, "mode": PauseMode.FREEZE}
        # JSON is the adapter boundary. Consumers see the typed contract, while semantic validation
        # remains where it already lives so this refactor does not change corrupt-file behaviour.
        return cast(PauseState, payload)

    def mode(self) -> PauseMode | Literal[""]:
        return normalize_pause_mode(self.load().get("mode"))

    def save(self, payload: PauseState) -> None:
        write_json(self.path, payload)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def summary(self) -> dict[str, Any]:
        """Compact pause state for a tick result: enough for the log to say why nothing moved."""
        state = self.load()
        mode = normalize_pause_mode(state.get("mode"))
        out: dict[str, Any] = {
            "paused": bool(mode),
            "mode": mode,
            "pause_file": str(self.path),
        }
        if state.get("corrupt"):
            out["warnings"] = [f"pause file is unreadable and read as frozen: {self.path}"]
        if not mode:
            return out
        out.update(
            {
                "auto_resume": auto_resume_status(state),
                "since": str(state.get("since") or ""),
                "actor": str(state.get("actor") or ""),
                "reason": str(state.get("reason") or ""),
                "stopped_worker": list(state.get("stopped_worker") or []),
                "stopped_reviewer": list(state.get("stopped_reviewer") or []),
                "stopped_observer": list(state.get("stopped_observer") or []),
                "excluded_worker": list(state.get("excluded_worker") or []),
            }
        )
        return out


def pause_payload(
    *,
    mode: PauseMode,
    actor: str,
    reason: str,
    since: str,
    stopped_worker: list[str],
    stopped_reviewer: list[str],
    excluded_worker: list[str],
    legacy_mirror: LegacyPauseMirror,
    stopped_observer: list[str] | None = None,
) -> PauseState:
    return {
        "version": 1,
        "mode": mode,
        "since": since,
        "actor": actor,
        "reason": reason,
        "stopped_worker": sorted(stopped_worker),
        "stopped_reviewer": sorted(stopped_reviewer),
        "stopped_observer": sorted(stopped_observer or []),
        "excluded_worker": sorted(excluded_worker),
        "legacy_mirror": legacy_mirror,
    }


def on_resume_text(mode: str, stopped_worker: list[str], stopped_reviewer: list[str]) -> str:
    if mode == PauseMode.FREEZE:
        return (
            f"resume clears the freeze, relaunches {len(stopped_worker)} stopped worker head(s) and "
            f"{len(stopped_reviewer)} stopped reviewer head(s) in their existing workspaces, gives "
            "every wait watchdog a fresh window, and lets the next tick bring the stopped sprint "
            "observers back"
        )
    if mode == PauseMode.DRAIN:
        return (
            "resume clears the drain and lets the tick claim Ready cards again; cards already in "
            "flight kept running through the pause"
        )
    return "not paused, resume is a no-op"


def legacy_mirror_path() -> Path:
    """Where a mirrored legacy flag is written.

    Resolved the way the background roles resolve their own state dir, not the wider candidate list
    the probe reads: a mirror written anywhere else is a file nobody checks.
    """
    explicit = os.environ.get("UMMANU_LEGACY_PAUSE_FILE")
    if explicit:
        return Path(explicit)
    for name in ("UMMANU_LEGACY_PIPELINE_STATE_DIR", "UMMANU_PIPELINE_STATE_DIR"):
        value = (env_value(name, nonempty=True) if name == "UMMANU_PIPELINE_STATE_DIR" else os.environ.get(name))
        if value:
            return Path(value) / "pause.json"
    workspaces_root = Path(env_value("UMMANU_WORKSPACES_ROOT", nonempty=True) or Path.home() / "orca" / "workspaces")
    return workspaces_root / "ummanu" / "pipeline" / "state" / "pipeline" / "pause.json"


def write_legacy_mirror(*, mode: PauseMode, actor: str, reason: str, since: str) -> LegacyPauseMirror:
    """Mirror the pause into the legacy flag so steward/curator/retro keep shedding.

    Best effort by design: a legacy path that cannot be written must not fail the pause. An existing
    legacy flag is never overwritten — someone else owns it, and clearing it on resume would lift a
    pause this command did not set.
    """
    path = legacy_mirror_path()
    out: LegacyPauseMirror = {"path": str(path), "written": False}
    if path.exists():
        out["reason"] = "a legacy pause file already exists and is left untouched"
        return out
    payload = {
        "mode": _LEGACY_MODES.get(mode, "soft"),
        "since": since,
        "reason": reason,
        "actor": actor,
        "stopped_worker": [],
        "stopped_reviewer": [],
        "excluded_worker": [],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, payload)
    except OSError as exc:
        out["reason"] = f"legacy mirror could not be written: {exc}"
        return out
    out["written"] = True
    return out


def clear_legacy_mirror(state: PauseState) -> LegacyPauseMirror:
    """Remove a mirror this pause wrote. A mirror it did not write is left where it is."""
    mirror = state.get("legacy_mirror") or {}
    path = str(mirror.get("path") or "")
    if not mirror.get("written") or not path:
        return {"path": path, "cleared": False, "reason": "no legacy mirror was written by this pause"}
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return {"path": path, "cleared": False, "reason": "legacy mirror is already gone"}
    except OSError as exc:
        return {"path": path, "cleared": False, "reason": f"legacy mirror could not be removed: {exc}"}
    return {"path": path, "cleared": True}
