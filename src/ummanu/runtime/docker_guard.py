"""Conservative protection against accidental Docker execution in worker/reviewer heads.

Native Docker resolves endpoints and container metadata. All targets are checked before the
single destructive call, which uses full IDs. This is not a malicious-head sandbox. See
docs/HEAD_RUNTIME.md "Worker and reviewer Docker guard".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ummanu.board.local_run import parse_local_run_policy
from ummanu.runtime.container_labels import is_test_container

BACKEND_ENV = "UMMANU_DOCKER_BACKEND"
PYTHON_ENV = "UMMANU_DOCKER_PYTHON"
SOURCE_ENV = "UMMANU_DOCKER_SOURCE"
POLICY_ENV = "UMMANU_DOCKER_LOCAL_RUN_POLICY"
BINDINGS = (BACKEND_ENV, PYTHON_ENV, SOURCE_ENV, POLICY_ENV)
DESTRUCTIVE = {"rm", "stop", "kill"}
HEAVY = {"run", "create", "build"}
CONTAINER_ALIASES = {"remove": "rm"}
BUILD_ALIASES = {"image": {"build"}, "builder": {"build"}, "buildx": {"build", "b"}}
PRUNE_GROUPS = {"container", "system", "volume", "image", "builder", "buildx", "network"}
GLOBAL_VALUES = {
    "--host",
    "-H",
    "--context",
    "-c",
    "--config",
    "--log-level",
    "-l",
    "--tlscacert",
    "--tlscert",
    "--tlskey",
}
GLOBAL_BOOLS = {"--debug", "-D", "--tls", "--tlsverify", "--help", "-h", "--version", "-v"}
COMPOSE_VALUES = {
    "--file",
    "-f",
    "--project-name",
    "-p",
    "--project-directory",
    "--env-file",
    "--profile",
    "--parallel",
    "--ansi",
    "--progress",
}
COMPOSE_BOOLS = {"--compatibility", "--dry-run", "--all-resources", "--help"}
ALTERNATIVE = "use docker container rm|stop|kill with explicit test container IDs instead"
USE_CI = "use CI; heavy local Docker requires an exact sprint local_run_exceptions argv"


class GuardError(RuntimeError):
    pass


def _option(args: list[str], index: int, values: set[str], booleans: set[str]) -> tuple[list[str], int]:
    """Consume one known flag; never infer how many arguments an unknown flag takes."""
    token = args[index]
    name, separator, value = token.partition("=")
    # Docker's short value flags permit attached values, e.g. -Hunix:///tmp/docker.sock.
    if not separator and token[:2] in values and not token.startswith("--") and len(token) > 2:
        name, separator, value = token[:2], "=", token[2:]
    if name in booleans:
        if separator and value not in {"true", "false", "1", "0"}:
            raise GuardError(f"invalid boolean option {name}")
        return [token], index + 1
    if name in values:
        if separator:
            if not value:
                raise GuardError(f"missing value for {name}")
            return [name, value], index + 1
        if index + 1 >= len(args) or not args[index + 1] or args[index + 1].startswith("--"):
            raise GuardError(f"missing value for {name}")
        return args[index : index + 2], index + 2
    raise GuardError(f"unknown option {name}; command scope is unresolved; {USE_CI}")


def _leading(args: list[str], values: set[str], booleans: set[str]) -> tuple[list[str], list[str]]:
    prefix: list[str] = []
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "--":
            return prefix, args[index + 1 :]
        option, index = _option(args, index, values, booleans)
        prefix.extend(option)
    return prefix, args[index:]


def _targets(args: list[str], operation: str, globals_: list[str]) -> tuple[list[str], list[str]]:
    values = {"--signal", "-s"} if operation in {"stop", "kill"} else set()
    if operation == "stop":
        values |= {"--timeout", "--time", "-t"}
    booleans = {"--force", "-f", "--volumes", "-v"} if operation == "rm" else set()
    options: list[str] = []
    targets: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            targets.extend(args[index + 1 :])
            break
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            # rm's -l means link removal, including attached/equals spellings. Never reinterpret
            # an unsupported local flag as the global log level and remove the whole container.
            if operation == "rm" and (
                name == "--link" or (token.startswith("-l") and not token.startswith("--"))
            ):
                raise GuardError(f"link removal is unsupported; {ALTERNATIVE}")
            # Local flags win over globals (rm -v means volumes, not version).
            if name in values | booleans or (token[:2] in values and not token.startswith("--")):
                option, index = _option(args, index, values, booleans)
                options.extend(option)
            else:
                option, index = _option(args, index, GLOBAL_VALUES, GLOBAL_BOOLS - {"-v"})
                globals_.extend(option)
        else:
            targets.append(token)
            index += 1
    if not targets or any(not target or target.startswith("-") for target in targets):
        raise GuardError("explicit container targets are required")
    return options, targets


def _native_json(backend: str, args: list[str], env: dict[str, str]) -> object:
    result = subprocess.run(
        [backend, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise GuardError("native inspection failed; no containers changed")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise GuardError("native inspection returned invalid JSON") from exc


def _endpoint(backend: str, globals_: list[str], env: dict[str, str]) -> list[str]:
    """Let Docker resolve flags/env/current context, then pin its actual host for both calls.

    A named context with stored TLS material needs a second TLS translation engine. Refuse that
    scope in this minimal guard; explicit host/TLS CLI settings use Docker's default context.
    """
    records = _native_json(backend, [*globals_, "context", "inspect"], env)
    try:
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError
        context = records[0]
        name = context["Name"]
        endpoint = context["Endpoints"]["docker"]
        host = endpoint["Host"]
        if not isinstance(name, str) or not name or not isinstance(host, str) or not host:
            raise ValueError
        if not re.match(r"^(unix|tcp|ssh|npipe)://[^\s]+$", host):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise GuardError("Docker endpoint is unresolved") from exc
    if name != "default" and (context.get("TLSMaterial") or endpoint.get("SkipTLSVerify")):
        raise GuardError("TLS context scope unsupported; use explicit --host and TLS options")
    pinned: list[str] = []
    index = 0
    while index < len(globals_):
        token = globals_[index]
        flag = token.split("=", 1)[0]
        if flag in {"--host", "-H", "--context", "-c"}:
            index += 2
            continue
        if name != "default" and flag.startswith("--tls"):
            index += 2 if flag in GLOBAL_VALUES else 1
            continue
        pinned.append(token)
        index += 1
    env.pop("DOCKER_CONTEXT", None)
    env.pop("DOCKER_HOST", None)
    if name != "default":
        env.pop("DOCKER_TLS_VERIFY", None)
        env.pop("DOCKER_CERT_PATH", None)
    return [*pinned, "--host", host]


def _checked_ids(backend: str, prefix: list[str], targets: list[str], env: dict[str, str]) -> list[str]:
    ids: list[str] = []
    for target in targets:
        records = _native_json(backend, [*prefix, "container", "inspect", "--", target], env)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise GuardError(f"container {target!r} did not resolve uniquely")
        record = records[0]
        config = record.get("Config")
        identifier = record.get("Id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise GuardError(f"container {target!r} has no full immutable ID")
        if not isinstance(config, dict) or not is_test_container(config.get("Labels")):
            raise GuardError(
                f"container {target!r} is protected: valid test ownership required, no production marker"
            )
        ids.append(identifier)
    return ids


def _heavy_allowed(args: list[str], env: dict[str, str]) -> bool:
    """Match ['docker', *original_arguments], before parsing or endpoint/alias translation.

    No basename, path, flag, shell, wildcard or prefix equivalence is applied. The role launcher
    strips inherited policy and supplies only its explicit dispatcher snapshot. This is an
    accidental-command guard, not protection against a head forging its own environment.
    """
    try:
        entries = parse_local_run_policy(json.loads(env.get(POLICY_ENV, "")))
    except (ValueError, TypeError):
        return False
    return any(entry.argv == ("docker", *args) for entry in entries)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    heavy_allowed = _heavy_allowed(args, env)
    backend = env.get(BACKEND_ENV, "")
    try:
        path = Path(backend)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise GuardError("native Docker backend unavailable; repair the role launch")
        if path.resolve().parent.name == "docker-bin":
            raise GuardError("native Docker backend resolves to the guard")
        globals_, command = _leading(args, GLOBAL_VALUES, GLOBAL_BOOLS)
        if command:
            verb = command[0]
            rest = command[1:]
            if verb == "compose":
                _, compose = _leading(rest, COMPOSE_VALUES | GLOBAL_VALUES, COMPOSE_BOOLS | GLOBAL_BOOLS)
                if compose and compose[0] in DESTRUCTIVE | {"down", "prune"}:
                    raise GuardError(f"Compose destructive scope is unsupported; {ALTERNATIVE}")
                if compose and compose[0] in {"up", "run", "build"} and not heavy_allowed:
                    raise GuardError(f"Compose {compose[0]} refused; {USE_CI}")
            elif verb in PRUNE_GROUPS:
                values = GLOBAL_VALUES | ({"--builder"} if verb == "buildx" else set())
                nested_globals, nested = _leading(rest, values, GLOBAL_BOOLS)
                globals_.extend(nested_globals)
                if nested and nested[0] == "prune":
                    raise GuardError(f"prune scope is unsupported; {ALTERNATIVE}")
                if verb == "container" and nested:
                    operation = CONTAINER_ALIASES.get(nested[0], nested[0])
                    if operation in DESTRUCTIVE | {"run", "create"}:
                        verb, rest = operation, nested[1:]
                elif nested and nested[0] in BUILD_ALIASES.get(verb, ()):
                    verb = "build"
            if verb == "prune":
                raise GuardError(f"prune scope is unsupported; {ALTERNATIVE}")
            if verb in HEAVY and not heavy_allowed:
                raise GuardError(f"Docker {verb} refused; {USE_CI}")
            if verb in DESTRUCTIVE:
                options, targets = _targets(rest, verb, globals_)
                prefix = _endpoint(backend, globals_, env)
                identifiers = _checked_ids(backend, prefix, targets, env)
                args = [*prefix, "container", verb, *options, "--", *identifiers]
        # exec preserves native output, exit status and signal handling, and never searches PATH.
        os.execve(backend, [backend, *args], env)
    except subprocess.TimeoutExpired:
        print("docker-guard: native inspection timed out; no containers changed", file=sys.stderr)
        return 125
    except (GuardError, OSError) as exc:
        print(f"docker-guard: {exc}", file=sys.stderr)
        return 125
    return 125  # execve never returns
