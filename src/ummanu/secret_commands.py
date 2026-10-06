"""`ummanu secret` — init, set, list, import, remove and materialize.

Three rules shape this surface. A value never travels through argv, so `set`
reads stdin or a file and `import` reads an env file. No command here prints a
value: `list` prints catalog metadata, `import` and `materialize` print ids and
variable names, and reading a value is an internal API until the broker card
gives it a safe consumer. The recovery phrase is shown once, on stderr, and the
store is not created until the user has typed some of it back.
"""

from __future__ import annotations

import argparse
import os
import secrets as pysecrets
import stat
import sys
from functools import wraps
from pathlib import Path

from ummanu.cli_output import print_json
from ummanu.infra.github_credential import (
    CHECKPOINT_CREDENTIAL_ID,
    CHECKPOINT_CREDENTIAL_PURPOSE,
    CredentialError,
    validate_checkpoint_credential,
)
from ummanu.runtime.paths import add_instance_argument
from ummanu.secret_store import (
    CONFIRM_WORDS,
    MATERIALIZE_FILE,
    MATERIALIZE_RUNTIME_ENV,
    MATERIALIZE_TARGETS,
    SecretStoreError,
    SecretStoreStateError,
    SecretStoreValidationError,
    generate_recovery_phrase,
    import_env_file,
    initialize_store,
    is_initialized,
    list_secrets,
    materialize_secrets,
    normalize_phrase,
    remove_secret,
    set_secret,
)
from ummanu.state_repo import StateRepoError

SECRET_EXIT_VALIDATION = 2
SECRET_EXIT_STATE = 3

DEFAULT_ACTOR = "operator"


def add_secret_subcommands(subparsers) -> None:
    secret = subparsers.add_parser("secret", help="manage the encrypted secret store in the instance repo")
    secret_subcommands = secret.add_subparsers(dest="secret_command")

    init = secret_subcommands.add_parser(
        "init",
        help="generate the recovery phrase and create the store; refuses to overwrite one",
    )
    add_instance_argument(init)
    init.add_argument("--actor", default=DEFAULT_ACTOR)
    init.add_argument(
        "--words",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    init.set_defaults(handler=run_secret_init)

    set_command = secret_subcommands.add_parser(
        "set", help="store one value read from stdin or a file; never from the command line"
    )
    add_instance_argument(set_command)
    set_command.add_argument("--actor", default=DEFAULT_ACTOR)
    set_command.add_argument("--id", required=True, dest="secret_id")
    set_command.add_argument("--scope", required=True, help="'installation' or 'project:<id>'")
    set_command.add_argument("--purpose", required=True)
    set_command.add_argument("--environment", help="environment variable this value materializes into")
    _add_target_arguments(set_command, "where this value materializes", default="none")
    source = set_command.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="read the value from this file")
    source.add_argument("--stdin", action="store_true", help="read the value from standard input")
    set_command.set_defaults(handler=run_secret_set)

    list_command = secret_subcommands.add_parser(
        "list", help="print catalog metadata; values are never printed"
    )
    add_instance_argument(list_command)
    list_command.set_defaults(handler=run_secret_list)

    import_command = secret_subcommands.add_parser(
        "import", help="take an existing env file into the store, one secret per variable"
    )
    add_instance_argument(import_command)
    import_command.add_argument("--actor", default=DEFAULT_ACTOR)
    import_command.add_argument(
        "--file",
        required=True,
        help="the env file to read: KEY=VALUE lines, LF, no comments or blank lines",
    )
    import_command.add_argument("--scope", required=True, help="'installation' or 'project:<id>'")
    import_command.add_argument("--purpose", required=True)
    _add_target_arguments(
        import_command,
        "where these values materialize back to",
        default=MATERIALIZE_RUNTIME_ENV,
    )
    import_command.set_defaults(handler=run_secret_import)

    remove_command = secret_subcommands.add_parser(
        "remove", help="drop one secret from the catalog and delete its envelope"
    )
    add_instance_argument(remove_command)
    remove_command.add_argument("--actor", default=DEFAULT_ACTOR)
    remove_command.add_argument("--id", required=True, dest="secret_id")
    remove_command.set_defaults(handler=run_secret_remove)

    materialize_command = secret_subcommands.add_parser(
        "materialize", help="write the materializing secrets into their env files"
    )
    add_instance_argument(materialize_command)
    materialize_command.add_argument(
        "--target",
        choices=MATERIALIZE_TARGETS,
        help="write only this target; by default every target in the catalog is written",
    )
    materialize_command.set_defaults(handler=run_secret_materialize)

    github = secret_subcommands.add_parser(
        "checkpoint-github", help="manage the one encrypted GitHub credential used by checkpoint pushes"
    )
    github_commands = github.add_subparsers(dest="checkpoint_github_command")
    for verb in ("set", "import"):
        command = github_commands.add_parser(
            verb, help="read a GitHub token from stdin or a mode-0600 file; never from argv"
        )
        add_instance_argument(command)
        command.add_argument("--actor", default=DEFAULT_ACTOR)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--file", help="regular mode-0600 token file")
        source.add_argument("--stdin", action="store_true", help="read the token from standard input")
        command.set_defaults(handler=run_checkpoint_github_set)
    github.set_defaults(handler=lambda args: _usage(github))

    secret.set_defaults(handler=lambda args: _usage(secret))


def _add_target_arguments(parser: argparse.ArgumentParser, help_text: str, *, default: str) -> None:
    parser.add_argument(
        "--materialize",
        choices=(*MATERIALIZE_TARGETS, "none"),
        default=default,
        help=help_text,
    )
    parser.add_argument(
        "--materialize-path",
        help=f"target file for --materialize {MATERIALIZE_FILE}",
    )


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 2


def _secret_command(operation: str):
    def decorate(command):
        @wraps(command)
        def run(args: argparse.Namespace) -> int:
            try:
                payload = command(args)
            except SecretStoreValidationError as exc:
                return _fail(operation, "validation", str(exc))
            except SecretStoreStateError as exc:
                return _fail(operation, "state", str(exc))
            except (SecretStoreError, StateRepoError) as exc:
                return _fail(operation, "runtime", str(exc))
            if isinstance(payload, int):
                return payload
            _print_json(payload)
            return 0

        return run

    return decorate


@_secret_command("init")
def run_secret_init(args: argparse.Namespace) -> int:
    if not _stdin_and_stderr_are_interactive():
        return _fail(
            "init",
            "validation",
            "secret init is interactive by design: stdin and stderr must both be a "
            "terminal, so the phrase and its confirmation never land in a pipe, file, "
            "or log",
        )
    instance_dir = _instance_dir(args.instance)
    if is_initialized(instance_dir):
        return _fail(
            "init",
            "state",
            "secret store is already initialized; init will not overwrite it",
        )
    words = args.words or 16
    phrase = generate_recovery_phrase(words)

    _show_phrase(phrase)
    if not _acknowledge_written_down():
        return _fail(
            "init",
            "validation",
            "recovery phrase not confirmed; the store was not initialized",
        )
    if not _clear_screen_and_scrollback():
        return _fail(
            "init",
            "validation",
            "could not clear the terminal and its scrollback; refusing to ask the "
            "confirmation questions while the phrase might still be visible",
        )
    if not _confirm_phrase(phrase):
        return _fail(
            "init",
            "validation",
            "recovery phrase not confirmed; the store was not initialized",
        )
    result = initialize_store(instance_dir, phrase=phrase, actor=args.actor)
    return {
        "ok": True,
        "op": "init",
        "key": str(result.key_path),
        "catalog": str(result.catalog_path),
        "commit": result.commit,
    }


@_secret_command("set")
def run_secret_set(args: argparse.Namespace) -> int:
    value = _read_value(args)
    materialize = _materialize_spec(args)
    result = set_secret(
        _instance_dir(args.instance),
        secret_id=args.secret_id,
        value=value,
        scope=args.scope,
        purpose=args.purpose,
        environment=args.environment,
        materialize=materialize,
        actor=args.actor,
    )
    return {
        "ok": True,
        "op": "set",
        "id": result.secret_id,
        "scope": result.scope,
        "bytes": len(value),
        "created": result.created,
        "commit": result.commit,
    }


@_secret_command("checkpoint-github")
def run_checkpoint_github_set(args: argparse.Namespace) -> int:
    value = _read_checkpoint_credential(args)
    try:
        # Persist the canonical token, not the ordinary line terminator an
        # operator used to deliver it.
        value = validate_checkpoint_credential(value).encode("utf-8")
    except CredentialError as exc:
        raise SecretStoreValidationError(str(exc)) from None
    result = set_secret(
        _instance_dir(args.instance),
        secret_id=CHECKPOINT_CREDENTIAL_ID,
        value=value,
        scope="installation",
        purpose=CHECKPOINT_CREDENTIAL_PURPOSE,
        actor=args.actor,
    )
    return {
        "ok": True,
        "op": "checkpoint-github",
        "id": result.secret_id,
        "created": result.created,
        "bytes": len(value),
        "commit": result.commit,
    }


@_secret_command("list")
def run_secret_list(args: argparse.Namespace) -> int:
    entries = list_secrets(_instance_dir(args.instance))
    return {"ok": True, "op": "list", "secrets": [dict(entry) for entry in entries]}


@_secret_command("import")
def run_secret_import(args: argparse.Namespace) -> int:
    materialize = _materialize_spec(args)
    result = import_env_file(
        _instance_dir(args.instance),
        source=Path(args.file).expanduser(),
        scope=args.scope,
        purpose=args.purpose,
        materialize=materialize,
        actor=args.actor,
    )
    return {
        "ok": True,
        "op": "import",
        "created": list(result.created),
        "updated": list(result.updated),
        "unchanged": list(result.unchanged),
        "commit": result.commit,
    }


@_secret_command("remove")
def run_secret_remove(args: argparse.Namespace) -> int:
    result = remove_secret(_instance_dir(args.instance), secret_id=args.secret_id, actor=args.actor)
    return {"ok": True, "op": "remove", "id": result.secret_id, "commit": result.commit}


@_secret_command("materialize")
def run_secret_materialize(args: argparse.Namespace) -> int:
    results = materialize_secrets(_instance_dir(args.instance), target=args.target)
    return {
        "ok": True,
        "op": "materialize",
        "targets": [
            {
                "target": result.target,
                "path": str(result.path),
                "variables": list(result.variables),
                "changed": result.changed,
            }
            for result in results
        ],
    }


def _materialize_spec(args: argparse.Namespace) -> dict[str, str] | None:
    """Turn the two target flags into the instruction the catalog stores."""
    choice = getattr(args, "materialize", "none")
    path = getattr(args, "materialize_path", None)
    if choice == "none":
        if path:
            raise SecretStoreValidationError("--materialize-path needs --materialize " + MATERIALIZE_FILE)
        return None
    if choice == MATERIALIZE_FILE:
        if not path:
            raise SecretStoreValidationError(f"--materialize {MATERIALIZE_FILE} needs --materialize-path")
        return {"target": choice, "path": path}
    if path:
        raise SecretStoreValidationError(
            f"--materialize {MATERIALIZE_RUNTIME_ENV} takes no --materialize-path; "
            "the path comes from the installation's runtime env resolution"
        )
    return {"target": choice}


def _read_value(args: argparse.Namespace) -> bytes:
    """Read the value as bytes, so multiline and binary survive untouched."""
    if args.file:
        path = Path(args.file).expanduser()
        try:
            value = path.read_bytes()
        except FileNotFoundError:
            raise SecretStoreValidationError(f"value file not found: {path}") from None
        except OSError as exc:
            raise SecretStoreValidationError(f"could not read {path}: {exc}") from None
    else:
        value = sys.stdin.buffer.read()
    if not value:
        raise SecretStoreValidationError("secret value is empty")
    return value


def _read_checkpoint_credential(args: argparse.Namespace) -> bytes:
    """The dedicated credential file has the same ownership boundary as the key."""
    if not args.file:
        return _read_value(args)
    path = Path(args.file).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise SecretStoreValidationError(f"could not inspect credential file: {exc}") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise SecretStoreValidationError("credential file must be a regular mode-0600 file")
    if info.st_uid != os.geteuid():
        raise SecretStoreValidationError("credential file belongs to another user")
    return _read_value(args)


def _stdin_and_stderr_are_interactive() -> bool:
    """Whether both ends of the confirmation dialog are a real terminal.

    `secret init` prints the phrase and reads the confirmation, so a
    non-interactive stdin or stderr means the phrase would land in a pipe,
    file, or log before anything asks a single question. That check has to
    run before the phrase is generated, not just before it is printed.
    """
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _show_phrase(phrase: str) -> None:
    """Show the phrase once, on stderr so a redirected stdout cannot capture it."""
    words = phrase.split()
    lines = [
        "",
        "Recovery phrase. Written down now or lost forever; it is not stored on this host.",
        "",
    ]
    lines += [f"  {index + 1:2d}. {word}" for index, word in enumerate(words)]
    lines += ["", f"Confirm {CONFIRM_WORDS} words to prove you wrote it down.", ""]
    print("\n".join(lines), file=sys.stderr)


def _acknowledge_written_down() -> bool:
    """The explicit "I wrote it down" step between the phrase and the clear.

    Without a step here, the screen clear could race a distracted operator
    who has not actually copied the phrase yet. Requiring the literal word
    "yes" means an empty Enter or a stray keystroke does not silently pass.
    """
    try:
        answer = _read_line(
            "Type 'yes' once you have written the phrase down, to clear the screen and continue: "
        )
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def _clear_screen_and_scrollback() -> bool:
    """Clear the visible screen and the scrollback, not just the viewport.

    `\\033[3J` is the part a plain `clear screen` sequence omits: without it
    the phrase is one scroll-up away for the rest of the session. A dumb
    terminal (or no terminal at all) cannot be trusted to have honored it, so
    this reports failure rather than guessing.
    """
    term = os.environ.get("TERM", "")
    if not term or term == "dumb" or not sys.stderr.isatty():
        return False
    try:
        sys.stderr.write("\033[H\033[2J\033[3J")
        sys.stderr.flush()
    except OSError:
        return False
    return True


def _confirm_phrase(phrase: str) -> bool:
    words = phrase.split()
    positions = sorted(_confirmation_positions(len(words)))
    for position in positions:
        try:
            answer = _read_line(f"word {position + 1}: ")
        except EOFError:
            return False
        if normalize_phrase_or_empty(answer) != words[position]:
            return False
    return True


def _read_line(prompt: str) -> str:
    """The one seam all interactive reads go through."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":
        raise EOFError
    return line.rstrip("\n")


def normalize_phrase_or_empty(answer: str) -> str:
    try:
        return normalize_phrase(answer)
    except SecretStoreValidationError:
        return ""


def _confirmation_positions(count: int) -> set[int]:
    positions: set[int] = set()
    while len(positions) < min(CONFIRM_WORDS, count):
        positions.add(pysecrets.randbelow(count))
    return positions


def _instance_dir(value: str) -> Path:
    path = Path(value).expanduser()
    return path.parent if path.name == "instance.yaml" else path


def _fail(operation: str, error: str, message: str) -> int:
    _print_json({"ok": False, "op": operation, "error": error, "message": message})
    if error == "validation":
        return SECRET_EXIT_VALIDATION
    if error == "state":
        return SECRET_EXIT_STATE
    return 1


def _print_json(payload: dict[str, object]) -> None:
    print_json(payload, indent=2)


__all__ = [
    "add_secret_subcommands",
    "run_secret_import",
    "run_secret_init",
    "run_secret_list",
    "run_secret_materialize",
    "run_secret_remove",
    "run_secret_set",
]
