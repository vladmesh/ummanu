"""The one place a head's shell command is built from a profile and prompt input.

Owns the adapter shapes (`claude`, `codex`, `hermes`: efforts, prompt on the command line or not)
and the role-env wrapper. Does not own the registry (`ummanu.runtime.heads` imports this package,
never the reverse; a profile arrives as a mapping) and opens no pane (`spawn` runs the string).

A given `prompt` goes on the command line for adapters that can carry one; `prompt=None` renders the
interactive shape and `prompt_after_start` tells the caller to deliver into the live pane. Codex has
only the interactive shape.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from ummanu.runtime import role_env
from ummanu.runtime.launch_prefix import pythonpath_prefix

from ..codex_preflight import CodexHomeLoginMissing, codex_home, codex_trust_paths

# Valid backend names; this renderer validates the profile's choice.
from ..head_runtimes import DEFAULT_HEAD_RUNTIME, HEAD_RUNTIMES

# Efforts each adapter accepts and their command-line spelling.
CODEX_EFFORTS = {
    "default": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "ultra",
}

CLAUDE_EFFORTS = {"default", "low", "medium", "high", "xhigh", "max"}

# Interactive adapters receive prompts through their live pane.
PROMPT_AFTER_START_ADAPTERS = {"codex"}

# Codex heads are TUI sessions; unsupported modes are refused.
CODEX_TUI_MODE = "tui"
CODEX_LAUNCH_MODES = {CODEX_TUI_MODE}

PYTHON_SAFE_PATH_FLAG = "-P"

# Every head runs `role_env.ENTRY_POINT`; the binding only chooses its PYTHONPATH. Head binding: the
# configured checkout (`UMMANU_REPO`, else `$HOME/ummanu`) plus the launcher's PYTHONPATH, with an
# optional identity. Standing binding: `role_env.runtime_pythonpath()`, no identity.
HEAD_BINDING = "head"
STANDING_BINDING = "standing"
ROLE_ENV_BINDINGS = (HEAD_BINDING, STANDING_BINDING)


class HeadCommandError(RuntimeError):
    """A head whose command cannot be rendered: unknown adapter, unspellable effort, missing input."""


@dataclass(frozen=True)
class HeadCommand:
    """One head's launch command, whether its prompt still has to be delivered, and by what."""

    command: str
    prompt_after_start: bool = False
    adapter: str = ""


def validate_launch_shape(profile_id: str, profile: Mapping[str, Any]) -> None:
    """Refuse a profile whose launch shape this module cannot render.

    Shared by `validate_registry` and `HeadSpec.from_profile`, so load time and bring-up apply one
    rule. Covers adapter, effort, codex mode, `runtime` (independent of adapter; absent means
    `DEFAULT_HEAD_RUNTIME`; `orca-legacy` is refused by name with the fix) and memory limit. Resource
    existence and fallback chains stay with the registry.
    """
    adapter = _named(profile.get("adapter"), f"profile {profile_id!r} adapter")
    if adapter not in _ADAPTERS:
        raise HeadCommandError(
            f"profile {profile_id!r} has unknown adapter {adapter!r} (known: {', '.join(sorted(_ADAPTERS))})"
        )
    if adapter == "codex":
        effort = _named(profile.get("effort", "default"), f"profile {profile_id!r} effort")
        if effort not in CODEX_EFFORTS:
            known = ", ".join(sorted(CODEX_EFFORTS))
            raise HeadCommandError(
                f"profile {profile_id!r} has unknown codex effort {effort!r} (known: {known})"
            )
        mode = _named(profile.get("codex_mode", CODEX_TUI_MODE), f"profile {profile_id!r} codex launch mode")
        if mode not in CODEX_LAUNCH_MODES:
            known = ", ".join(sorted(CODEX_LAUNCH_MODES))
            raise HeadCommandError(
                f"profile {profile_id!r} has unknown codex launch mode {mode!r} (known: {known})"
            )
    if adapter == "claude":
        effort = _named(profile.get("effort", "default"), f"profile {profile_id!r} effort")
        if effort not in CLAUDE_EFFORTS:
            known = ", ".join(sorted(CLAUDE_EFFORTS))
            raise HeadCommandError(
                f"profile {profile_id!r} has unknown claude effort {effort!r} (known: {known})"
            )
    # Backend validity is independent of the CLI adapter.
    runtime = _named(profile.get("runtime", DEFAULT_HEAD_RUNTIME), f"profile {profile_id!r} runtime")
    if runtime not in HEAD_RUNTIMES:
        known = ", ".join(HEAD_RUNTIMES)
        raise HeadCommandError(
            f"profile {profile_id!r} has unknown runtime {runtime!r} (known: {known}); "
            f'set `runtime = "{DEFAULT_HEAD_RUNTIME}"` or drop the key'
        )
    from .memory import DEFAULT_MEMORY_LIMIT_MIB, memory_limit_mib

    try:
        memory_limit_mib(profile.get("memory_limit_mib", DEFAULT_MEMORY_LIMIT_MIB), profile_id)
    except ValueError as exc:
        raise HeadCommandError(str(exc)) from None


def _named(value: object, what: str) -> str:
    """A profile field that must be a plain string; checked first since an unhashable value would
    make `value not in table` raise TypeError."""
    if not isinstance(value, str):
        raise HeadCommandError(f"{what} must be a name, got {type(value).__name__}")
    return value


def render_head_command(
    profile: Mapping[str, Any],
    *,
    prompt: str | None = None,
    workspace: str = "",
    role: str = "",
    identity: Mapping[str, str] | None = None,
    local_run_policy: str | None = None,
    binding: str = HEAD_BINDING,
) -> HeadCommand:
    """The shell command that brings one head up, and how its prompt reaches it.

    An empty `role` renders the bare adapter command (`ummanu shell`). `workspace` is required for
    Codex trust overrides. `identity` is rendered only by the head binding.
    """
    adapter = str(profile.get("adapter") or "")
    render = _ADAPTERS.get(adapter)
    if render is None:
        known = ", ".join(sorted(_ADAPTERS))
        raise HeadCommandError(f"head has unknown adapter {adapter!r} (known: {known})")
    command = render(profile, prompt=prompt, workspace=workspace)
    if role:
        command = wrap_role_command(
            role,
            command,
            identity=identity,
            local_run_policy=local_run_policy,
            binding=binding,
            workspace=workspace,
        )
    elif identity or local_run_policy is not None:
        raise HeadCommandError("an unwrapped head command carries no identity")
    return HeadCommand(
        command,
        prompt_after_start=prompt is None or adapter in PROMPT_AFTER_START_ADAPTERS,
        adapter=adapter,
    )


def wrap_role_command(
    role: str,
    command: str,
    *,
    identity: Mapping[str, str] | None = None,
    local_run_policy: str | None = None,
    binding: str = HEAD_BINDING,
    workspace: str = "",
) -> str:
    """Render one head's command under the role environment its launcher binds.

    The installation binding is written into the command because the head's terminal is not the
    launcher's child; without it a non-default instance's heads read the home default `runtime.env`.
    `identity` names must be in the role's allowlist, else refused. `local_run_policy` is passed as an
    explicit role-env argument so no inherited or runtime.env value can grant the exception.
    """
    if binding not in ROLE_ENV_BINDINGS:
        known = ", ".join(ROLE_ENV_BINDINGS)
        raise HeadCommandError(f"unknown role env binding {binding!r} (known: {known})")
    if binding == STANDING_BINDING:
        if identity or local_run_policy is not None:
            raise HeadCommandError(f"the {STANDING_BINDING} binding renders no identity for role {role!r}")
        return role_env.wrap_shell_command(role, command, workspace=workspace or None)
    if local_run_policy is not None and role not in role_env.RUFF_ROLES:
        raise HeadCommandError(f"role {role!r} carries no local-run policy")
    unknown = sorted(set(identity or {}) - set(role_env.ROLE_ALLOWLIST.get(role, ())))
    if unknown:
        raise HeadCommandError(f"role {role!r} carries no binding named {', '.join(unknown)}")
    rendered = [f"{name}={shlex.quote(value)}" for name, value in sorted((identity or {}).items())]
    prefix = " ".join([*role_env.launch_binding(), *rendered])
    command = role_env.role_shell_command(role, command, workspace=workspace or None)
    workspace_arg = f" --workspace {shlex.quote(workspace)}" if workspace else ""
    policy_arg = (
        f" --local-run-policy {shlex.quote(local_run_policy)}" if local_run_policy is not None else ""
    )
    return (
        f"{prefix} {pythonpath_prefix(cast(dict[str, str], os.environ))} python3 {PYTHON_SAFE_PATH_FLAG} "
        f"-m {role_env.ENTRY_POINT} exec --role {shlex.quote(role)}{workspace_arg}{policy_arg} -- /bin/sh -lc "
        f"{shlex.quote(command)}"
    )


def with_pid_heartbeat(
    command: str, pid_file: str, *, identity: Mapping[str, str] | None = None,
    in_process: bool = False,
) -> str:
    """Prefix a head command with an atomic versioned launch-identity heartbeat.

    `$$` is the shell's pid and the final `exec` replaces it with the head, so the pid holds for the
    head's life. `in_process` writes the record in that same process: a separate writer would join the
    cgroup and break sole-victim OOM attribution. `exec env <command>` is required because `exec
    NAME=value prog` treats the assignment as the program.
    """
    # Keeps the terminal process group for TTY semantics and safe group signalling.
    writer_args = "path, pid, identity, command = sys.argv[1:]" if in_process else "path, pid, identity = sys.argv[1:]"
    writer = """import json
import os
import sys
import tempfile
__WRITER_ARGS__
stat = open(f'/proc/{pid}/stat', encoding='utf-8').read()
close = stat.rfind(')')
fields = stat[close + 2:].split()
if close < 0 or len(fields) <= 19:
    raise RuntimeError('process stat has no start time')
record = json.loads(identity)
record.update({'version': 1, 'pid': int(pid),
               'boot_id': open('/proc/sys/kernel/random/boot_id', encoding='utf-8').read().strip(),
               'proc_starttime_ticks': fields[19]})
def bind_leaf(record):
    try:
        handoff = json.load(open(path + '.leaf', encoding='utf-8'))
        expected = handoff.get('expected')
        leaf = handoff.get('leaf')
        if (isinstance(expected, dict) and isinstance(leaf, str)
                and all(str(record.get(name) or '') == str(expected.get(name) or '')
                        and str(expected.get(name) or '')
                        for name in ('run_id', 'role', 'task'))):
            record['leaf'] = leaf
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
directory = os.path.dirname(path) or '.'
os.makedirs(directory, mode=0o700, exist_ok=True)
def publish(payload):
    fd, temporary = tempfile.mkstemp(prefix='.ummanu-heartbeat-', dir=directory)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, sort_keys=True, separators=(',', ':'))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
bind_leaf(record)
publish(record)
# The terminal reply can race between the first handoff read and this base replace.  Check once
# more after publishing so the durable record eventually carries the returned leaf in either order.
before = record.get('leaf')
bind_leaf(record)
if record.get('leaf') != before:
    publish(record)""".replace("__WRITER_ARGS__", writer_args)
    encoded_identity = json.dumps(dict(identity or {}), sort_keys=True, separators=(",", ":"))
    if in_process:
        # Written in the head process itself, so the recorded pid survives exec.
        writer += "\nos.execvpe('/bin/sh', ['/bin/sh', '-c', 'exec env ' + command], os.environ)"
        return (
            f'exec python3 -P -c {shlex.quote(writer)} {shlex.quote(pid_file)} "$$" '
            f"{shlex.quote(encoded_identity)} {shlex.quote(command)}"
        )
    return (
        f'python3 -P -c {shlex.quote(writer)} {shlex.quote(pid_file)} "$$" '
        f"{shlex.quote(encoded_identity)}; exec env {command}"
    )


def _render_claude(profile: Mapping[str, Any], *, prompt: str | None, workspace: str) -> str:
    del workspace
    memory = json.dumps(
        {
            "mcpServers": {
                "memory": {
                    "type": "http",
                    "url": "http://127.0.0.1:8077/mcp",
                    "headers": {"Authorization": "Bearer ${UMMANU_MEMORY_ACCESS_TOKEN}"},
                }
            }
        },
        separators=(",", ":"),
    )
    args = [
        "claude",
        "--dangerously-skip-permissions",
        "--strict-mcp-config",
        "--mcp-config",
        memory,
    ]
    model = profile.get("model")
    if model:
        args += ["--model", str(model)]
    effort = str(profile.get("effort") or "default")
    if effort not in CLAUDE_EFFORTS:
        known = ", ".join(sorted(CLAUDE_EFFORTS))
        raise HeadCommandError(f"claude profile has unknown effort {effort!r} (known: {known})")
    if effort != "default":
        args += ["--effort", effort]
    command = shlex.join(args)
    return command if prompt is None else f"{command} {prompt!r}"


def _render_hermes(profile: Mapping[str, Any], *, prompt: str | None, workspace: str) -> str:
    """Hermes' equivalent of `claude --dangerously-skip-permissions <prompt>`.

    `-z` seeds an autonomous session (not `-q` single-turn), `--yolo` skips permissions, `--cli` forces
    the plain REPL. Without a prompt the REPL comes up empty, as `ummanu shell` wants.
    """
    del workspace
    parts = ["hermes"]
    if prompt is not None:
        parts += ["-z", repr(prompt)]
    if profile.get("model"):
        parts += ["-m", str(profile["model"])]
    if profile.get("provider"):
        parts += ["--provider", str(profile["provider"])]
    parts += ["--yolo", "--cli"]
    return " ".join(parts)


def _render_codex_tui(profile: Mapping[str, Any], *, prompt: str | None, workspace: str) -> str:
    """The command that brings one Codex head up; the only shape is the interactive TUI.

    `prompt` is ignored: the caller delivers it into the live pane once the TUI is idle. No
    `--skip-git-repo-check` (exec-only; the TUI rejects it). The trust overrides state intent only;
    Codex 0.145 still shows the dialog, so the `codex_preflight` write into this CODEX_HOME is what
    passes it, and the paths come from that preflight.
    """
    del prompt
    if not workspace:
        raise HeadCommandError("codex TUI launch requires workspace for directory trust override")
    # Best-effort preference, never provider capability evidence.
    args = [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--enable",
        "multi_agent_v2",
        "-c",
        "features.multi_agent_v2.wait_agent_enabled=false",
        "-c",
        "mcp_servers.po_memory.enabled=false",
        "-c",
        'mcp_servers.memory.url="http://127.0.0.1:8077/mcp"',
        "-c",
        'mcp_servers.memory.bearer_token_env_var="UMMANU_MEMORY_ACCESS_TOKEN"',
    ]
    model = profile.get("model")
    if model:
        args += ["-m", str(model)]
    effort_name = str(profile.get("effort") or "default")
    if effort_name not in CODEX_EFFORTS:
        known = ", ".join(sorted(CODEX_EFFORTS))
        raise HeadCommandError(f"codex profile has unknown effort {effort_name!r} (known: {known})")
    effort = CODEX_EFFORTS[effort_name]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    for path in codex_trust_paths(workspace):
        # TUI trust comes from preflight's config.toml, not these command overrides.
        args += ["-c", f'projects.{json.dumps(path)}.trust_level="trusted"']
    try:
        home = codex_home(profile)
    except CodexHomeLoginMissing as exc:
        # Refused in the renderer's own failure type, with the fix.
        raise HeadCommandError(str(exc)) from None
    return f"CODEX_HOME={shlex.quote(home)} {shlex.join(args)}"


_ADAPTERS = {
    "claude": _render_claude,
    "hermes": _render_hermes,
    "codex": _render_codex_tui,
}
