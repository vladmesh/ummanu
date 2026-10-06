"""`ummanu web-front`: set the password, render the front's configuration, audit it.

Three verbs, and the split between them is the point. `set-password` is the only one that touches a
plaintext password, and it never puts one on a command line or on stdout. It also rotates the
independent browser-session secret, revoking every persistent browser session with the password.
`render` reads the hash and session secret from the secret store and writes the configuration under
the data directory. For an installation created before persistent sessions existed, its first
render creates that random session secret once. `check` reads a rendered configuration back and
reports every published route it would answer without owner authentication.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.cli_output import print_json
from ummanu.config import instance_data_dir
from ummanu.runtime.paths import add_instance_argument
from ummanu.secret_store import (
    MATERIALIZE_FILE,
    SecretStoreError,
    SecretStoreStateError,
    SecretStoreValidationError,
    list_secrets,
    read_secret,
    set_secret,
)
from ummanu.state_repo import StateRepoError
from ummanu.web.app import ROUTES
from ummanu.webfront.caddyfile import (
    HASH_SECRET_ID,
    PASSWORD_SECRET_ID,
    SESSION_SECRET_BYTES,
    SESSION_SECRET_ID,
    USERNAME,
    FrontConfig,
    FrontConfigError,
    render,
)
from ummanu.webfront.guard import CaddyfileSyntaxError, unguarded_routes, upstreams

EXIT_VALIDATION = 2
EXIT_STATE = 3

DEFAULT_ACTOR = "operator"
#: Where a rendered configuration and Caddy's own storage live, under the data directory.
FRONT_DIRNAME = "webfront"
CONFIG_NAME = "Caddyfile"
STORAGE_NAME = "caddy"
#: The env file the owner reads their own password back from, mode 0600, outside the repository.
PASSWORD_FILE_NAME = "owner-password.env"
PASSWORD_VARIABLE = "UMMANU_WEB_FRONT_PASSWORD"
#: Bytes of entropy in a generated password: 32 URL-safe characters out of `secrets`.
GENERATED_BYTES = 24
#: Where an installation keeps the addresses its front answers on, so a recovered host renders the
#: same file: instance config travels with the live root, the rendered file does not (ummanu-53).
SITES_SETTING = "host.web_front.sites"


def add_web_front_subcommands(subparsers) -> None:
    group = subparsers.add_parser(
        "web-front",
        help="the password-guarded TLS front that publishes the loopback web transport",
    )
    commands = group.add_subparsers(dest="web_front_command")

    password = commands.add_parser(
        "set-password",
        help="store the owner's password and its bcrypt hash; a value never travels through argv",
    )
    add_instance_argument(password)
    password.add_argument("--actor", default=DEFAULT_ACTOR)
    source = password.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true", help="read the password from standard input")
    source.add_argument(
        "--generate",
        action="store_true",
        help="generate one from `secrets`, store it, and print nothing but where to read it",
    )
    password.add_argument("--caddy", default="caddy", help="the caddy executable that hashes it")
    password.set_defaults(handler=run_set_password)

    config = commands.add_parser(
        "render", help="write the front's configuration, taking auth material from the secret store"
    )
    add_instance_argument(config)
    config.add_argument("--data-dir", default=os.environ.get("UMMANU_DATA_DIR"))
    config.add_argument(
        "--site",
        action="append",
        required=True,
        metavar="ADDRESS",
        help="an https site address to answer on; repeat for a name and its addresses",
    )
    config.add_argument(
        "--bind",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="listen on this interface only; the rehearsal and rollback handle (see OPERATIONS.md)",
    )
    config.add_argument("--output", help="where to write it; the data directory's own path by default")
    config.add_argument("--upstream-port", type=int, default=None)
    config.set_defaults(handler=run_render)

    audit = commands.add_parser(
        "check", help="report every published route a rendered configuration answers unguarded"
    )
    add_instance_argument(audit)
    audit.add_argument("--data-dir", default=os.environ.get("UMMANU_DATA_DIR"))
    audit.add_argument("--config", help="the file to read; the data directory's own path by default")
    audit.set_defaults(handler=run_check)

    group.set_defaults(handler=lambda args: _usage(group))


# -- verbs ---------------------------------------------------------------------------------------


def run_set_password(args: argparse.Namespace) -> int:
    instance_dir = _instance_dir(args.instance)
    if args.generate:
        password = pysecrets.token_urlsafe(GENERATED_BYTES)
    else:
        password = sys.stdin.read().strip("\r\n")
    if not password:
        return _fail("set-password", "validation", "the password is empty")
    try:
        digest = hash_password(password, executable=args.caddy)
    except FrontConfigError as exc:
        return _fail("set-password", "validation", str(exc))
    session_secret = pysecrets.token_urlsafe(SESSION_SECRET_BYTES)
    try:
        # Rotate the bearer first. If a later write fails, a subsequent render fails safer: old
        # browser sessions are revoked while the last complete password/hash pair still guards it.
        set_secret(
            instance_dir,
            secret_id=SESSION_SECRET_ID,
            value=session_secret.encode("utf-8"),
            scope="installation",
            purpose="web front browser session signing secret, rotated with owner password",
            actor=args.actor,
        )
        set_secret(
            instance_dir,
            secret_id=PASSWORD_SECRET_ID,
            value=password.encode("utf-8"),
            scope="installation",
            purpose="web front owner password",
            actor=args.actor,
            environment=PASSWORD_VARIABLE,
            materialize={
                "target": MATERIALIZE_FILE,
                "path": str(_front_dir(instance_dir, None) / PASSWORD_FILE_NAME),
            },
        )
        set_secret(
            instance_dir,
            secret_id=HASH_SECRET_ID,
            value=digest.encode("utf-8"),
            scope="installation",
            purpose="web front owner password bcrypt hash, read by `web-front render`",
            actor=args.actor,
        )
    except SecretStoreValidationError as exc:
        return _fail("set-password", "validation", str(exc))
    except SecretStoreStateError as exc:
        return _fail("set-password", "state", str(exc))
    except (SecretStoreError, StateRepoError) as exc:
        return _fail("set-password", "runtime", str(exc))
    print_json(
        {
            "ok": True,
            "op": "set-password",
            "account": USERNAME,
            "password_secret": PASSWORD_SECRET_ID,
            "hash_secret": HASH_SECRET_ID,
            "session_secret": SESSION_SECRET_ID,
            "generated": bool(args.generate),
            "read_it_back": (
                f"ummanu secret materialize --instance {args.instance} --target file, then read "
                f"{PASSWORD_VARIABLE} from "
                f"{_front_dir(instance_dir, None) / PASSWORD_FILE_NAME}"
            ),
            "next": "ummanu web-front render, then restart the front unit",
        }
    )
    return 0


def run_render(args: argparse.Namespace) -> int:
    instance_dir = _instance_dir(args.instance)
    try:
        rendered = render_front(
            instance_dir,
            sites=args.site,
            data_dir=args.data_dir,
            bind=args.bind,
            output=Path(args.output).expanduser() if args.output else None,
            upstream_port=args.upstream_port,
        )
    except FrontRenderError as exc:
        return _fail("render", exc.error, str(exc))
    print_json(
        {
            "ok": True,
            "op": "render",
            "config": str(rendered.path),
            "sites": list(rendered.config.sites),
            "bind": list(rendered.config.bind),
            "upstream": list(upstreams(rendered.text)),
            "routes_guarded": len(ROUTES),
            "storage": str(rendered.storage),
            "changed": rendered.changed,
        }
    )
    return 0


def run_check(args: argparse.Namespace) -> int:
    instance_dir = _instance_dir(args.instance)
    front = _front_dir(instance_dir, args.data_dir)
    path = Path(args.config).expanduser() if args.config else front / CONFIG_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _fail("check", "state", f"could not read {path}: {exc}")
    try:
        findings = unguarded_routes(text, ROUTES)
        proxied = upstreams(text)
    except CaddyfileSyntaxError as exc:
        return _fail("check", "validation", f"{path} is not a configuration this reader can audit: {exc}")
    print_json(
        {
            "ok": not findings,
            "op": "check",
            "config": str(path),
            "routes": len(ROUTES),
            "unguarded": list(findings),
            "upstream": list(proxied),
        }
    )
    return 0 if not findings else EXIT_STATE


# -- the one render ------------------------------------------------------------------------------


class FrontRenderError(RuntimeError):
    """No configuration was written. `error` is the verb's error class: validation, state or runtime."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error


@dataclass(frozen=True)
class RenderedFront:
    config: FrontConfig
    text: str
    path: Path
    storage: Path
    #: Whether the file on disk was different (or absent) before this render.
    changed: bool


def configured_sites(host: Any) -> tuple[str, ...]:
    """The site addresses `host.web_front.sites` names; empty when the setting is absent."""
    front = host.get("web_front") if isinstance(host, dict) else None
    sites = front.get("sites") if isinstance(front, dict) else None
    if not isinstance(sites, list):
        return ()
    return tuple(site for site in sites if isinstance(site, str) and site)


def missing_sites_message(instance_dir: Path) -> str:
    """What an operator does about an enabled front with no sites in instance config."""
    return (
        f"the web-front component is enabled and {SITES_SETTING} is not set in "
        f"{instance_dir / 'instance.yaml'}; list the https addresses the front answers on there "
        "(docs/OPERATIONS.md, \"Web front sites\"), or render one by hand with `ummanu web-front render "
        "--instance ... --site https://...`, or disable the component"
    )


def render_front(
    instance_dir: Path,
    *,
    sites: Iterable[str],
    data_dir: str | Path | None = None,
    bind: Iterable[str] = (),
    output: Path | None = None,
    upstream_port: int | None = None,
) -> RenderedFront:
    """Render the front from the secret store's hash and session secret, audit it, then write it.

    `ummanu web-front render` is this function, and the materializer's `web-front-config` step runs
    that verb as the installation key's owner. The file is rewritten only when its text or mode
    differs, and never when the guard finds an unauthenticated route.
    """
    front = _front_dir(instance_dir, str(data_dir) if data_dir else None)
    path = output if output is not None else front / CONFIG_NAME
    try:
        digest = read_secret(instance_dir, HASH_SECRET_ID).decode("utf-8").strip()
    except SecretStoreStateError as exc:
        raise FrontRenderError(
            "state",
            f"{exc}; run `ummanu web-front set-password` before rendering a front that "
            "would otherwise have no password to check",
        ) from None
    except (SecretStoreError, StateRepoError) as exc:
        raise FrontRenderError("runtime", str(exc)) from None
    try:
        session_secret = _read_or_create_session_secret(instance_dir)
    except SecretStoreValidationError as exc:
        raise FrontRenderError("validation", str(exc)) from None
    except SecretStoreStateError as exc:
        raise FrontRenderError("state", str(exc)) from None
    except (SecretStoreError, StateRepoError) as exc:
        raise FrontRenderError("runtime", str(exc)) from None
    config = FrontConfig(
        sites=tuple(sites),
        password_hash=digest,
        session_secret=session_secret,
        storage=front / STORAGE_NAME,
        bind=tuple(bind),
        **({"upstream_port": upstream_port} if upstream_port else {}),
    )
    try:
        text = render(config)
    except FrontConfigError as exc:
        raise FrontRenderError("validation", str(exc)) from None
    findings = unguarded_routes(text, ROUTES)
    if findings:
        # Belt and braces: the renderer cannot produce this, and the file is not written if it does.
        raise FrontRenderError("state", "; ".join(findings))
    changed = not _holds_private(path, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    (front / STORAGE_NAME).mkdir(parents=True, exist_ok=True)
    if changed:
        _write_private(path, text)
    return RenderedFront(config=config, text=text, path=path, storage=front / STORAGE_NAME, changed=changed)


# -- helpers -------------------------------------------------------------------------------------


def _holds_private(path: Path, text: str) -> bool:
    """Whether `path` is already this text, at mode 0600."""
    try:
        return (path.stat().st_mode & 0o777) == 0o600 and path.read_text(encoding="utf-8") == text
    except (OSError, UnicodeError):
        return False


def _read_or_create_session_secret(instance_dir: Path) -> str:
    """Return the persistent signing secret, migrating an older installation on first render."""
    entries = list_secrets(instance_dir)
    if any(entry.get("id") == SESSION_SECRET_ID for entry in entries):
        return read_secret(instance_dir, SESSION_SECRET_ID).decode("utf-8").strip()
    value = pysecrets.token_urlsafe(SESSION_SECRET_BYTES)
    set_secret(
        instance_dir,
        secret_id=SESSION_SECRET_ID,
        value=value.encode("utf-8"),
        scope="installation",
        purpose="web front browser session signing secret, rotated with owner password",
        actor=DEFAULT_ACTOR,
    )
    return value


def hash_password(password: str, *, executable: str = "caddy") -> str:
    """The bcrypt hash `basicauth` checks, from the same binary that will check it.

    The plaintext goes in on stdin; it is never an argument, so it is never in this host's process
    table. `caddy hash-password` reads one line, which is why the newline is written explicitly.
    """
    try:
        finished = subprocess.run(
            [executable, "hash-password"],
            input=password + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise FrontConfigError(
            f"could not run {executable!r} to hash the password ({exc}); the front's own binary "
            "produces the hash it checks, so it must be installed first"
        ) from None
    if finished.returncode != 0:
        raise FrontConfigError(f"{executable} hash-password failed: {finished.stderr.strip()}")
    digest = finished.stdout.strip()
    if not digest.startswith("$2"):
        raise FrontConfigError(f"{executable} hash-password did not produce a bcrypt hash")
    return digest


def _front_dir(instance_dir: Path, data_dir: str | None) -> Path:
    root = Path(data_dir).expanduser() if data_dir else instance_data_dir(instance_dir)
    return root / FRONT_DIRNAME


def _write_private(path: Path, text: str) -> None:
    """Write the configuration where only its owner can read its hash and bearer."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _instance_dir(value: str) -> Path:
    path = Path(value).expanduser()
    return path.parent if path.name == "instance.yaml" else path


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 2


def _fail(operation: str, error: str, message: str) -> int:
    print(
        json.dumps({"ok": False, "op": operation, "error": error, "message": message}, sort_keys=True),
        file=sys.stderr,
    )
    if error == "validation":
        return EXIT_VALIDATION
    if error == "state":
        return EXIT_STATE
    return 1
