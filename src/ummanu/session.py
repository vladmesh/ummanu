"""Interactive ummanu launch — the trusted operator entry point.

`ummanu shell` is the human operator's own tool, not a pipeline role. It boots a chosen head
(claude, codex, hermes or any heads.toml profile) with the *full* installation runtime env, so
board access and every other credential are present regardless of which head runs. Automated
worker/reviewer heads stay narrowly scoped through role_env; the operator deliberately does not.

The env is injected at the launch boundary, not by the head, so switching heads never changes
whether the credentials are there.

The head starts in the installation's interactive workspace, `<data>/interactive`, which carries its
persona (`ummanu.runtime.interactive_workspace`); `--workspace` names another directory instead.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ummanu.memory import access as memory_access
from ummanu.runtime import heads as head_registry
from ummanu.runtime import interactive_workspace
from ummanu.runtime.codex_home import DATA_DIR_ENV, INSTANCE_ENV
from ummanu.runtime.head import (
    HeadCommandError,
    HeadRun,
    HeadSpec,
    TaskRef,
    new_run_id,
    render_head_command,
    with_pid_heartbeat,
)
from ummanu.runtime.paths import default_instance_path
from ummanu.runtime.role_env import load_env_file

# The operator names a head the way a human thinks about it ("claude", "codex", "hermes"): a bare
# adapter name is an adapter choice, never a profile id, and picks that adapter's head from the
# installation's registry. Any real heads.toml profile id is also accepted verbatim, so
# `--head claude-opus-high` or `--head codex-sol-medium` work too. With no `--head`, the shell opens
# on the registry's `role_defaults.new_card`.
SHELL_ADAPTERS = ("claude", "codex", "hermes")
DEFAULT_HEAD_ROLE = "new_card"


class SessionError(RuntimeError):
    pass


def operator_env(
    env_file: str | os.PathLike[str] | None = None,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Full runtime env for the operator: base process env overlaid with the entire runtime.env.

    No allowlist and no sensitive-name scrubbing — that is the point.
    """
    base = dict(os.environ if base_env is None else base_env)
    source = load_env_file(env_file)
    env = {**base, **source}
    env["UMMANU_ROLE"] = "operator"
    return env


def resolve_profile_id(head: str | None, *, registry: head_registry.Registry | None = None) -> str:
    """Resolve a user-supplied head name to a real heads.toml profile id."""
    reg = registry or head_registry.load_registry()
    if not head:
        head = head_registry.required_role_default(reg.role_defaults, DEFAULT_HEAD_ROLE)
    elif head in SHELL_ADAPTERS:
        return adapter_profile(head, reg)
    # An id the registry does not define — one the installation has retired included — fails by
    # name with the known ids rather than being routed to a look-alike.
    return reg.resolve(head)


def adapter_profile(adapter: str, registry: head_registry.Registry) -> str:
    """The profile a bare adapter name opens: the first role default on that adapter, else the
    first profile on it in id order."""
    routed = [str(head) for head in registry.role_defaults.values() if isinstance(head, str)]
    candidates = [*routed, *registry.known()]
    for pid in candidates:
        profile = registry.profiles.get(pid)
        if isinstance(profile, dict) and profile.get("adapter") == adapter:
            return pid
    raise head_registry.HeadRegistryError(
        f"no {adapter} head in the registry (known: {', '.join(registry.known()) or '(none)'})"
    )


def render_interactive(
    profile_id: str,
    *,
    workspace: str | None = None,
    registry: head_registry.Registry | None = None,
) -> str:
    """The interactive (no seeded prompt) launch command for a profile's adapter.

    The same renderer every launched head goes through, asked for the same shape a dispatcher- or
    tick-launched head gets: `prompt=None` is the command that carries no prompt, and an operator
    types into the session once it is up. Two things differ, and both are this caller: the command
    is not wrapped for a role, because it execs in a terminal the operator already owns with the
    environment they already have; and a Codex head is not preflighted through `codex_preflight`
    beforehand, because the trust dialog the flags do not always answer is being put to somebody
    who is sitting in front of it. So this writes nothing to the runtime's own config.
    """
    reg = registry or head_registry.load_registry()
    profile = reg.profile(profile_id)
    try:
        return render_head_command(profile, prompt=None, workspace=workspace or os.getcwd()).command
    except HeadCommandError as exc:
        raise SessionError(str(exc)) from None


@dataclass(frozen=True)
class ShellTarget:
    """The one resolution of the selected installation a shell launch uses throughout."""

    # The data dir every part of the launch reads: the Codex home binding, the memory grant, and
    # the default workspace. None only when `--workspace` was given and no installation resolves.
    data_dir: Path | None
    # The head's cwd and Codex trust directory.
    workspace: str


def resolve_shell_target(
    explicit_workspace: str | None, env_file: str | os.PathLike[str] | None, env: dict[str, str]
) -> ShellTarget:
    """Resolve the selected installation's data dir once, and the workspace from it.

    The data dir is, in order: `UMMANU_DATA_DIR`, the data dir of `UMMANU_INSTANCE`, of the
    `--env-file`'s instance, else of the default instance (`env` is the operator env, the process
    env overlaid with the runtime env file). The workspace is `--workspace` when given, else
    `<data>/interactive`.

    The interactive workspace is never materialized here. Upgrade and recover own it (they read the
    product checkout and the live root, and hand the tree to the runtime user); a missing one is
    refused with the command that creates it.
    """
    from ummanu.config import DataDirError, instance_data_dir

    data_dir: Path | None
    try:
        if env.get(DATA_DIR_ENV):
            data_dir = Path(env[DATA_DIR_ENV]).expanduser()
        elif env.get(INSTANCE_ENV):
            data_dir = instance_data_dir(Path(env[INSTANCE_ENV]))
        elif env_file:
            data_dir = instance_data_dir(Path(env_file).expanduser().parent)
        else:
            data_dir = instance_data_dir(default_instance_path())
    except (DataDirError, OSError) as exc:
        if explicit_workspace:
            return ShellTarget(None, explicit_workspace)
        raise SessionError(
            f"cannot resolve the interactive workspace: {exc}; run `ummanu upgrade` or pass --workspace"
        ) from None
    if explicit_workspace:
        return ShellTarget(data_dir, explicit_workspace)
    workspace = interactive_workspace.workspace_dir(data_dir)
    if not (workspace / interactive_workspace.AGENTS_FILE).is_file():
        raise SessionError(
            f"interactive workspace {workspace} is missing; run `ummanu upgrade --instance LIVE_ROOT` to "
            f"materialize it, set {INSTANCE_ENV} to select another installation, or pass --workspace"
        )
    return ShellTarget(data_dir, str(workspace))


@contextmanager
def _bound(data_dir: Path | None, env: dict[str, str]) -> Iterator[None]:
    """Name `data_dir` as `UMMANU_DATA_DIR` in this process and the launch env for the block.

    Codex home rendering reads the process env, the launched head reads `env`; both get the one
    resolved value. The previous process value is restored on exit.
    """
    if data_dir is None:
        yield
        return
    env[DATA_DIR_ENV] = str(data_dir)
    previous = os.environ.get(DATA_DIR_ENV)
    os.environ[DATA_DIR_ENV] = str(data_dir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = previous


def run_shell(args: argparse.Namespace) -> int:
    try:
        env = operator_env(args.env_file)
        target = resolve_shell_target(args.workspace, args.env_file, env)
    except SessionError as exc:
        print(f"ummanu shell: {exc}", file=sys.stderr)
        return 2
    # A Codex shell renders its CODEX_HOME against the same data dir the workspace came from.
    with _bound(target.data_dir, env):
        return _run_shell(args, env, target)


def _run_shell(args: argparse.Namespace, env: dict[str, str], target: ShellTarget) -> int:
    workspace = target.workspace
    try:
        profile_id = resolve_profile_id(args.head)
        command = render_interactive(profile_id, workspace=workspace)
    except (SessionError, head_registry.HeadRegistryError) as exc:
        print(f"ummanu shell: {exc}", file=sys.stderr)
        return 2
    if args.print_command:
        print(f"cd {shlex.quote(workspace)} && {command}")
        return 0
    try:
        registry = head_registry.load_registry()
        run_id = new_run_id()
        pid_dir = memory_access.bindings_dir(target.data_dir) / "heads"
        pid_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run = HeadRun(
            run_id=run_id,
            spec=HeadSpec.from_profile(profile_id, registry.profile(profile_id)),
            workspace=workspace,
            task_ref=TaskRef.standing("interactive"),
            role="po",
            pid_file=str(pid_dir / f"{run_id}.pid"),
        )
        grant = memory_access.issue_grant(
            run, memory_access.interactive_po_subject(), data_dir=target.data_dir
        )
        env[memory_access.MEMORY_ACCESS_TOKEN_ENV] = grant.token
        command = with_pid_heartbeat(
            command,
            run.pid_file,
            identity={"run_id": run.run_id, "role": run.role, "task": "standing:interactive"},
        )
    except (MemoryError, OSError, ValueError, memory_access.MemoryAccessError) as exc:
        print(f"ummanu shell: memory access binding could not be issued: {exc}", file=sys.stderr)
        return 2
    argv = ["/bin/sh", "-c", command]
    try:
        os.chdir(workspace)
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        print(f"ummanu shell: exec {command!r} in {workspace} failed: {exc}", file=sys.stderr)
        return 126
    return 0  # unreachable after a successful execvpe
