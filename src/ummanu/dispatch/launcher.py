"""What a dispatcher-launched head needs around its command: workspace preflight, and the model
and environment its bring-up is journalled with.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ummanu.runtime.codex_preflight import (
    CodexPreflightError,
    ensure_codex_workspace_trusted as _preflight_codex_workspace,
    reject_symlinked_config as _reject_symlinked_config,
)
from ummanu.runtime.paths import env_value
from ummanu.runtime.role_env import (
    RoleEnvError,
    runtime_env,
)

CLAUDE_JSON_DEFAULT = str(Path.home() / ".claude.json")
CLAUDE_THEME_DEFAULT = "dark"
# Where the `claude` CLI itself takes a model from when the head profile pins none.
CLAUDE_MANAGED_SETTINGS_DEFAULT = "/etc/claude-code/managed-settings.json"
CLAUDE_MANAGED_SETTINGS_ENV = "CLAUDE_MANAGED_SETTINGS"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CLAUDE_MODEL_ENV = "ANTHROPIC_MODEL"


class HeadLaunchError(RuntimeError):
    """A head that cannot be brought up in this workspace: its CLI's first-run state is unwritable."""


def ensure_claude_workspace_ready(
    workspace: str,
    config: Path | None = None,
    *,
    default_theme: str = CLAUDE_THEME_DEFAULT,
) -> None:
    """Pre-answer Claude Code first-run prompts for one headless workspace."""
    config_path = config or Path(env_value("UMMANU_CLAUDE_JSON", CLAUDE_JSON_DEFAULT))
    data = _load_claude_config(config_path)
    changed = _mark_claude_workspace_trusted(data, str(workspace), config_path)
    changed = _ensure_claude_theme(data, default_theme) or changed
    if changed:
        _save_claude_config(config_path, data)


def ensure_codex_workspace_trusted(
    profile: dict[str, Any],
    workspace: str,
    config: Path | None = None,
) -> None:
    """The dispatcher's way in to the shared Codex interactive preflight."""
    try:
        _preflight_codex_workspace(profile, workspace, config)
    except CodexPreflightError as exc:
        raise HeadLaunchError(str(exc)) from None


def claude_launch_model(
    profile: dict[str, Any],
    *,
    workspace: str = "",
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """The model a `claude` bring-up will run under, and where that value came from.

    A profile without `model` renders a command without `--model`, so the CLI picks the model itself.
    The routing journal has to name that model as of the bring-up, so this reads the same sources the
    CLI reads, in the CLI's own precedence. When nothing pins a model anywhere the value stays empty
    under a `cli_default` source.

    `env` is the environment the head itself will run with, which is not the dispatcher's own: heads
    run through the role env wrapper, and `role_env.runtime_env` drops every `runtime.env` variable
    that is not role-allowlisted, so reading `os.environ` here would journal a variable the CLI never
    receives.
    """
    environ = os.environ if env is None else env
    managed = _settings_model(
        Path(environ.get(CLAUDE_MANAGED_SETTINGS_ENV) or CLAUDE_MANAGED_SETTINGS_DEFAULT)
    )
    if managed:
        return managed, "managed_settings"
    pinned = str(profile.get("model") or "")
    if pinned:
        return pinned, "profile"
    from_env = str(environ.get(CLAUDE_MODEL_ENV) or "").strip()
    if from_env:
        return from_env, f"env:{CLAUDE_MODEL_ENV}"
    if workspace:
        for name, source in (
            ("settings.local.json", "project_settings_local"),
            ("settings.json", "project_settings"),
        ):
            model = _settings_model(Path(workspace) / ".claude" / name)
            if model:
                return model, source
    user = _settings_model(_claude_config_dir(environ) / "settings.json")
    if user:
        return user, "user_settings"
    return "", "cli_default"


def _claude_config_dir(env: Mapping[str, str]) -> Path:
    override = env.get(CLAUDE_CONFIG_DIR_ENV)
    if override:
        return Path(override)
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


def _settings_model(path: Path) -> str:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    return str(loaded.get("model") or "").strip()


def _load_claude_config(config: Path) -> dict[str, Any]:
    _reject_symlinked_claude_config(config)
    try:
        if not config.exists():
            return {}
        loaded = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HeadLaunchError(f"cannot read Claude config {config}: {exc}") from None
    if not isinstance(loaded, dict):
        raise HeadLaunchError(f"Claude config {config} has an unsupported shape")
    return loaded


def _mark_claude_workspace_trusted(data: dict[str, Any], workspace: str, config: Path) -> bool:
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise HeadLaunchError(f"Claude config {config} has non-object projects")
    entry = projects.setdefault(workspace, {})
    if not isinstance(entry, dict):
        raise HeadLaunchError(f"Claude config {config} has non-object project entry for {workspace}")
    if entry.get("hasTrustDialogAccepted") is True:
        return False
    entry["hasTrustDialogAccepted"] = True
    return True


def _ensure_claude_theme(data: dict[str, Any], default_theme: str) -> bool:
    if data.get("theme"):
        return False
    data["theme"] = default_theme
    return True


def _save_claude_config(config: Path, data: dict[str, Any]) -> None:
    temp_path: Path | None = None
    try:
        _reject_symlinked_claude_config(config)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config.name}.",
            suffix=".tmp",
            dir=config.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _reject_symlinked_claude_config(config)
        os.replace(temp_path, config)
    except OSError as exc:
        raise HeadLaunchError(f"cannot update Claude config {config}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _reject_symlinked_claude_config(config: Path) -> None:
    """The same guard the codex preflight writes its config behind, answered in this module's own
    failure type so a Claude bring-up still raises what its callers catch."""
    try:
        _reject_symlinked_config(config, "Claude")
    except CodexPreflightError as exc:
        raise HeadLaunchError(str(exc)) from None


def role_launch_env(role: str) -> dict[str, str]:
    """The environment the role env wrapper will actually hand a head of `role`.

    Same call the wrapper makes, so a snapshot taken here sees what the head sees. A role the
    allowlist does not know is not launchable through the wrapper at all.
    """
    try:
        return runtime_env(role)
    except RoleEnvError:
        return dict(os.environ)
