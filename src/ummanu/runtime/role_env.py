"""Role-scoped runtime env for ummanu automation launch boundaries.

The source of host secrets is an instance-owned runtime env file. Launchers must not
inherit it wholesale: each role gets only the names declared here, and sensitive names outside the
role allowlist are stripped even if the parent process already had them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from ummanu.board.local_run import parse_local_run_policy
from ummanu.runtime import docker_guard
from ummanu.runtime.paths import PRODUCT_ENV, default_instance_path

# The one module every launcher runs as `python3 -P -m <this> exec --role ...`.
ENTRY_POINT = "ummanu.runtime.role_env"

RUNTIME_ENV_FILE_ENV = "TA_RUNTIME_ENV_FILE"
UMMANU_RUNTIME_ENV_FILE_ENV = "UMMANU_RUNTIME_ENV_FILE"
# Packaged automation units use the TA_ spelling, dispatcher-launched heads the UMMANU_ one. The
# UMMANU_ pin wins, so recovery cannot materialize secrets into an ambient unit's runtime.env.
RUNTIME_ENV_FILE_ENVS = (UMMANU_RUNTIME_ENV_FILE_ENV, RUNTIME_ENV_FILE_ENV)
RUNTIME_ENV_DEFAULT = str(default_instance_path() / "runtime.env")


def runtime_env_path() -> Path:
    """The runtime env file, resolved per call so a process moved onto another installation follows."""
    return Path(
        next(
            (os.environ[name] for name in RUNTIME_ENV_FILE_ENVS if os.environ.get(name)), RUNTIME_ENV_DEFAULT
        )
    )


# For ummanu.session's launch error: the file selected at import.
RUNTIME_ENV = runtime_env_path()


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PYTHONPATH_ENV = "TA_RUNTIME_PYTHONPATH"


def runtime_product_root() -> Path:
    """The checkout a launched role imports the product from, resolved per call.

    ``TA_RUNTIME_PYTHONPATH``, then the configured ``UMMANU_REPO`` (an alternate checkout binds it in
    the units and launch command it renders), else this module's own checkout, which is importable.
    Never the dispatcher's running checkout by default, nor an assumed ``~/ummanu``.
    """
    configured = os.environ.get(RUNTIME_PYTHONPATH_ENV) or os.environ.get(PRODUCT_ENV)
    return Path(configured).expanduser() if configured else REPO_ROOT


def runtime_pythonpath() -> str:
    """The source tree of `runtime_product_root()`."""
    return str(runtime_product_root() / "src")


# The product's own interpreter (`<product root>/.venv/bin/python3`), provisioned by `ummanu upgrade`
# and verified by `scripts/ummanu-agent-gate.sh`.
MANAGED_VENV_DIR = ".venv"


def managed_venv_bin(product_root: Path | str | None = None) -> Path:
    """`<product root>/.venv/bin`, the one place a role's product interpreter is resolved.

    Defaults to `runtime_product_root()` so interpreter and source come from one checkout. Pure;
    `require_managed_interpreter` refuses a missing one.
    """
    root = Path(product_root).expanduser() if product_root is not None else runtime_product_root()
    return root / MANAGED_VENV_DIR / "bin"


def require_managed_interpreter(product_root: Path | str | None = None) -> Path:
    """`managed_venv_bin()`, refused when its `python3` is missing or not executable.

    Without it a head would run the system `python3` and every `python3 -P -m ummanu ...` would fail.
    The message matches `scripts/ummanu-agent-gate.sh`.
    """
    root = Path(product_root).expanduser() if product_root is not None else runtime_product_root()
    venv_bin = managed_venv_bin(root)
    python = venv_bin / "python3"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RoleEnvError(
            f"selected product checkout {str(root)!r} has no executable managed interpreter at "
            f"{str(python)!r}; repair it through the supported install/upgrade path: "
            f"ummanu upgrade --no-pull --product-root {shlex.quote(str(root))}"
        )
    return venv_bin


# Installation identity, not secrets. UMMANU_DATA_DIR must survive the allowlist: the dispatcher unit
# imports runtime.env wholesale, so a role stripped of it would read a state file nobody writes.
NONSECRET_ENV = (
    "UMMANU_INSTANCE",
    "UMMANU_DATA_DIR",
    "UMMANU_REPO",
)
# Bound by the launcher (the rendered unit); runtime.env cannot override them.
OBSERVER_SPRINT_ENV = "UMMANU_OBSERVER_SPRINT"
OBSERVER_GENERATION_ENV = "UMMANU_OBSERVER_GENERATION"
MEMORY_ACCESS_TOKEN_ENV = "UMMANU_MEMORY_ACCESS_TOKEN"
# The `--actor` board commands default to: a worker/reviewer head's profile, else the role name.
BOARD_ACTOR_ENV = "BOARD_ACTOR"
UNIT_BOUND_ENV = (
    "UMMANU_INSTANCE",
    "UMMANU_REPO",
    OBSERVER_SPRINT_ENV,
    OBSERVER_GENERATION_ENV,
    MEMORY_ACCESS_TOKEN_ENV,
    BOARD_ACTOR_ENV,
)
# Launcher-supplied identity only: runtime.env must never let a head claim another sprint or name.
LAUNCHER_ONLY_ENV = (OBSERVER_SPRINT_ENV, OBSERVER_GENERATION_ENV, MEMORY_ACCESS_TOKEN_ENV, BOARD_ACTOR_ENV)
# What a launched process has to be told about the installation it belongs to.
LAUNCH_BOUND_ENV = (*RUNTIME_ENV_FILE_ENVS, "UMMANU_INSTANCE", "UMMANU_REPO")

ROLE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "pipeline": NONSECRET_ENV,
    "worker": (*NONSECRET_ENV, MEMORY_ACCESS_TOKEN_ENV, BOARD_ACTOR_ENV),
    "reviewer": (*NONSECRET_ENV, MEMORY_ACCESS_TOKEN_ENV, BOARD_ACTOR_ENV),
    "observer": (*NONSECRET_ENV, OBSERVER_SPRINT_ENV, OBSERVER_GENERATION_ENV, MEMORY_ACCESS_TOKEN_ENV),
    "steward": (*NONSECRET_ENV, MEMORY_ACCESS_TOKEN_ENV),
    "retro": (*NONSECRET_ENV, MEMORY_ACCESS_TOKEN_ENV),
    "curator": (*NONSECRET_ENV, MEMORY_ACCESS_TOKEN_ENV),
}
RUFF_ROLES = frozenset(("worker", "reviewer"))
# Roles whose heads run the product's CLI on its managed venv. `pipeline` launches no head.
PRODUCT_VENV_ROLES = frozenset(("observer", "steward", "retro", "curator"))
# Reserved to the dispatcher, separate from a project's adapter-owned ``.venv``.
WORKSPACE_ENV_DIR = ".ummanu-task-env/venv"
# The dispatcher-owned namespace holding that environment; cleanup removes it whole, so everything
# the pipeline generates in a workspace belongs inside it.
WORKSPACE_NAMESPACE = Path(WORKSPACE_ENV_DIR).parts[0]
# Keeps worker/reviewer test and lint caches inside the namespace, out of the candidate tree.
WORKSPACE_TOOL_CACHES = {
    "PYTHONPYCACHEPREFIX": "pycache",
    "RUFF_CACHE_DIR": "ruff-cache",
    "MYPY_CACHE_DIR": "mypy-cache",
}
# A venv startup file setting the same bytecode prefix for every interpreter of that venv, including
# a child started with a scratch environment. An explicit prefix wins; the name sorts first so other
# startup files' imports are redirected too.
WORKSPACE_PYCACHE_PTH = "00-ummanu-task-pycache.pth"
# What the pipeline writes into a candidate checkout, excluded via the repo's local ``info/exclude``
# on every bring-up. All but the namespace are root-anchored: deeper same-named paths are the project's.
WORKSPACE_EXCLUDES = (
    f"{Path(WORKSPACE_ENV_DIR).parts[0]}/",
    "/TASK.md",
    "/state/checks/",
    "/.ummanu-report/",
)


def workspace_tool_cache_env(workspace: Path | str) -> dict[str, str]:
    """The cache redirections a head launched into `workspace` runs with."""
    namespace = Path(workspace).expanduser() / WORKSPACE_NAMESPACE
    return {name: str(namespace / directory) for name, directory in WORKSPACE_TOOL_CACHES.items()}


def workspace_pycache_pth(workspace: Path | str) -> str:
    """The body of `WORKSPACE_PYCACHE_PTH` for `workspace`: one import line `site` executes."""
    prefix = workspace_tool_cache_env(workspace)["PYTHONPYCACHEPREFIX"]
    return f"import sys; sys.pycache_prefix = sys.pycache_prefix or {prefix!r}\n"


def dispatcher_workspace_namespace(root: Path | str) -> Path | None:
    """`root`'s reserved namespace when the dispatcher's ownership claim is intact, else None.

    The claim (written by `_claim_workspace_environment`) is an `owner.json` naming the dispatcher and
    this exact workspace, inside a namespace that resolves within it.
    """
    try:
        resolved = Path(root).resolve(strict=True)
        namespace = resolved / WORKSPACE_NAMESPACE
        observed = json.loads((namespace / "owner.json").read_text(encoding="utf-8"))
        inside = namespace.resolve(strict=True).is_relative_to(resolved)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None
    expected = {"owner": "ummanu-dispatcher", "schema_version": 1, "workspace": str(resolved)}
    return namespace if observed == expected and inside and not namespace.is_symlink() else None


# Gates the synthetic BOARD_ROLE. po and dispatcher have no allowlist entry and are rejected earlier.
BOARD_ROLES = {"po", "dispatcher", "worker", "reviewer", "observer", "steward", "retro"}
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_ENV_NAME_RE = re.compile(
    r"(^|_)(TOKEN|PASSWORD|PASSWD|SECRET|PAT|KEY|IDENTITY|CREDENTIAL|AUTH|WEBHOOK)(_|$)",
    re.IGNORECASE,
)


def is_sensitive_env_name(name: str) -> bool:
    """Whether an env variable's name declares credential material.

    The one classification for role env filtering and every exact-value redaction gate; values alone
    cannot tell a secret from long configuration.
    """
    return bool(SENSITIVE_ENV_NAME_RE.search(str(name)))


class RoleEnvError(RuntimeError):
    """The role runtime env cannot be built without leaking or missing required names."""


def docker_guard_dir() -> Path:
    return runtime_product_root() / "src" / "ummanu" / "runtime" / "docker-bin"


def _docker_bindings(env: dict[str, str]) -> dict[str, str]:
    """Resolve native Docker before adding the guard, skipping previous guard PATH entries."""
    for directory in env.get("PATH", "").split(os.pathsep):
        if not directory or not Path(directory).is_absolute():
            continue
        candidate = Path(directory) / "docker"
        if candidate.resolve().parent.name == "docker-bin":
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            backend = str(candidate.resolve())
            break
    else:
        # Still bind the guard when Docker is absent. Executing it refuses, never falls through.
        backend = ""
    return {
        docker_guard.BACKEND_ENV: backend,
        docker_guard.PYTHON_ENV: str(managed_venv_bin() / "python3"),
        docker_guard.SOURCE_ENV: runtime_pythonpath(),
    }


def _require_docker_guard(env: dict[str, str]) -> None:
    guard = docker_guard_dir() / "docker"
    if not guard.is_file() or not os.access(guard, os.X_OK):
        raise RoleEnvError(f"Docker guard is unavailable at {guard}; repair the product installation")
    require_managed_interpreter()
    backend = Path(env[docker_guard.BACKEND_ENV])
    if not backend.is_file() or not os.access(backend, os.X_OK):
        raise RoleEnvError("native Docker backend is unavailable; repair the role launch PATH")


def _parse_assignment(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    if "=" not in line:
        return None
    key, raw_value = line.split("=", 1)
    key = key.strip()
    if not _KEY_RE.match(key):
        return None
    try:
        parts = shlex.split(f"x={raw_value}", comments=True, posix=True)
    except ValueError:
        value = raw_value.strip().strip("'\"")
    else:
        value = parts[0].split("=", 1)[1] if parts else ""
    return key, value


def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Read simple KEY=value lines from the control-panel env file without logging values."""
    env_path = Path(path) if path is not None else runtime_env_path()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        item = _parse_assignment(line)
        if item is not None:
            key, value = item
            out[key] = value
    return out


def allowlist(role: str) -> tuple[str, ...]:
    try:
        return ROLE_ALLOWLIST[role]
    except KeyError as e:
        known = ", ".join(sorted(ROLE_ALLOWLIST))
        raise RoleEnvError(f"unknown runtime role {role!r} (known: {known})") from e


def _is_sensitive_name(name: str) -> bool:
    return is_sensitive_env_name(name)


def runtime_env(
    role: str,
    *,
    base_env: dict[str, str] | None = None,
    env_file: Path | str | None = None,
    workspace: Path | str | None = None,
    local_run_policy: str | None = None,
) -> dict[str, str]:
    """Return a sanitized env for `role`, with role-allowed values overlaid from the source file."""
    allowed = set(allowlist(role))
    source = load_env_file(env_file)
    base = dict(os.environ if base_env is None else base_env)

    env: dict[str, str] = {}
    for key, value in base.items():
        if key in source and key not in allowed:
            continue
        if _is_sensitive_name(key) and key not in allowed:
            continue
        env[key] = value

    for key in allowed:
        if key in base and key in UNIT_BOUND_ENV:
            env[key] = base[key]
        elif key in LAUNCHER_ONLY_ENV:
            env.pop(key, None)
        elif key in source:
            env[key] = source[key]
        elif key in base:
            env[key] = base[key]

    # Derived by the trusted launcher, never inherited or taken from runtime.env.
    for key in docker_guard.BINDINGS:
        env.pop(key, None)
    if role in RUFF_ROLES:
        env.update(_docker_bindings(env))
        # Only from the explicit trusted command argument, never the ambient env.
        if local_run_policy is not None:
            try:
                policy = json.loads(local_run_policy)
                parse_local_run_policy(policy)
            except (ValueError, TypeError):
                pass  # malformed authority grants nothing, including an otherwise valid prefix
            else:
                env[docker_guard.POLICY_ENV] = json.dumps(policy, ensure_ascii=True)
        if workspace is not None:
            environment = Path(workspace).expanduser() / WORKSPACE_ENV_DIR
            venv_bin = environment / "bin"
            python = venv_bin / "python3"
            if not python.is_file() or not os.access(python, os.X_OK):
                raise RoleEnvError(f"workspace Python environment is unavailable at {environment}")
            env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
            env["VIRTUAL_ENV"] = str(environment)
            env.update(workspace_tool_cache_env(workspace))
        # PYTHONPATH served only the wrapper's own import; it is not authority for the head's commands.
        env.pop("PYTHONPATH", None)
        env["PATH"] = str(docker_guard_dir()) + os.pathsep + env.get("PATH", "")
    elif role in PRODUCT_VENV_ROLES:
        venv_bin = managed_venv_bin()
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(venv_bin.parent)

    if role in BOARD_ROLES:
        env["BOARD_ROLE"] = role
        env[BOARD_ACTOR_ENV] = board_actor(role, env)
    else:
        env.pop("BOARD_ROLE", None)
        env.pop(BOARD_ACTOR_ENV, None)
    return env


def board_actor(role: str, env: dict[str, str]) -> str:
    """The actor a role head writes the board as.

    Worker and reviewer (allowlist carries `BOARD_ACTOR`) use the value in the exec process's env,
    bound to the head profile by the launch command; runtime.env cannot supply it. Every other role,
    the observer included, is its role name.
    """
    if BOARD_ACTOR_ENV in ROLE_ALLOWLIST.get(role, ()):
        named = str(env.get(BOARD_ACTOR_ENV) or "").strip()
        if named:
            return named
    return role


def role_shell_command(
    role: str,
    command: str,
    *,
    environ: dict[str, str] | None = None,
    workspace: Path | str | None = None,
) -> str:
    """Make product-provisioned tools available inside a role's login shell.

    Login profiles may reset ``PATH`` after ``runtime_env()``, so the venv prefix goes in the command:
    the workspace venv for worker/reviewer, the product's managed venv for product-CLI roles.
    """
    guard_prefix = ""
    if role in RUFF_ROLES:
        guard = docker_guard_dir() / "docker"
        guard_prefix = (
            f"test -x {shlex.quote(str(guard))} || "
            "{ printf '%s\\n' 'docker-guard: executable unavailable; repair the role launch' >&2; exit 125; }; "
        )
    if role in PRODUCT_VENV_ROLES:
        venv_bin = managed_venv_bin()
    elif role in RUFF_ROLES:
        prefix = str(docker_guard_dir())
        if workspace is not None:
            prefix += os.pathsep + str(Path(workspace).expanduser() / WORKSPACE_ENV_DIR / "bin")
        return f"{guard_prefix}PATH={shlex.quote(prefix)}${{PATH:+:$PATH}}; export PATH; {command}"
    else:
        return command
    return f"PATH={shlex.quote(str(venv_bin))}${{PATH:+:$PATH}}; export PATH; {command}"


def observer_binding(sprint: str, generation: str) -> dict[str, str]:
    """The identity a launcher renders into one observer head's command line."""
    sprint = str(sprint or "").strip()
    generation = str(generation or "").strip()
    if not sprint or not generation:
        return {}
    return {OBSERVER_SPRINT_ENV: sprint, OBSERVER_GENERATION_ENV: generation}


def declared_observer_sprint(env: dict[str, str] | None = None) -> str:
    """The sprint this process was launched to observe, or an empty string."""
    source = os.environ if env is None else env
    sprint = str(source.get(OBSERVER_SPRINT_ENV, "") or "").strip()
    generation = str(source.get(OBSERVER_GENERATION_ENV, "") or "").strip()
    return sprint if generation else ""


def launch_binding() -> list[str]:
    """Leading assignments that tie a launched process to this installation.

    The head's terminal is not the launcher's child, so the runtime env file and instance are named in
    the command; otherwise a non-default installation's role reads ``paths.default_instance_path``.
    Only names the launcher was given are rendered.
    """
    return [f"{name}={shlex.quote(value)}" for name in LAUNCH_BOUND_ENV if (value := os.environ.get(name))]


def wrap_shell_command(
    role: str,
    command: str,
    *,
    pythonpath: str | None = None,
    env_file: Path | str | None = None,
    workspace: Path | str | None = None,
) -> str:
    """Shell command that execs `command` under the role env without putting secret values in argv."""
    py_path = pythonpath or runtime_pythonpath()
    parts = [
        *launch_binding(),
        f"PYTHONPATH={shlex.quote(py_path)}",
        "python3",
        "-P",
        "-m",
        ENTRY_POINT,
        "exec",
        "--role",
        shlex.quote(role),
    ]
    if env_file is not None:
        parts += ["--env-file", shlex.quote(str(env_file))]
    if workspace is not None:
        parts += ["--workspace", shlex.quote(str(workspace))]
    parts += [
        "--",
        "/bin/sh",
        "-lc",
        shlex.quote(role_shell_command(role, command, workspace=workspace)),
    ]
    return " ".join(parts)


def _main_exec(argv: list[str], *, prog: str) -> int:
    parser = argparse.ArgumentParser(prog=f"{prog} exec")
    parser.add_argument("--role", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--workspace")
    parser.add_argument("--local-run-policy")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    command = list(ns.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")
    try:
        if ns.role in RUFF_ROLES and not ns.workspace:
            raise RoleEnvError(f"role {ns.role!r} requires a workspace-owned Python environment")
        if ns.role in PRODUCT_VENV_ROLES:
            require_managed_interpreter()
        env = runtime_env(
            ns.role, env_file=ns.env_file, workspace=ns.workspace, local_run_policy=ns.local_run_policy
        )
        if ns.role in RUFF_ROLES:
            _require_docker_guard(env)
    except RoleEnvError as e:
        print(f"role-env: {e}", file=sys.stderr)
        return 125
    try:
        os.execvpe(command[0], command, env)
    except OSError as e:
        print(f"role-env: exec {command[0]!r} failed: {e}", file=sys.stderr)
        return 126


def main(
    argv: Sequence[str] | None = None,
    *,
    prog: str = f"python3 -m {ENTRY_POINT}",
    description: str | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(description or __doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "exec":
        return _main_exec(rest, prog=prog)
    print(f"role-env: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
