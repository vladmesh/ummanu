"""Bootstrap credentials, secret unlock and scoped runtime environment."""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ummanu.config import validate_instance
from ummanu.infra.github_credential import (
    CredentialError,
    bootstrap_file_owner_is_allowed,
    validate_checkpoint_credential,
)
from ummanu.installation.models import InstallError, InstallResult
from ummanu.installation.support import _named_path, _valid_existing_layout
from ummanu.runtime_env import instance_runtime_env_path
from ummanu.secret_recover import SecretRecovery, recover_secrets
from ummanu.secret_store import (
    MATERIALIZE_FILE,
    SecretStoreError,
    is_initialized,
    key_path,
    list_secrets,
    materialize_path,
    normalize_phrase,
)
from ummanu.state_repo import StateRepoError


def _bootstrap_credential(args: argparse.Namespace, target: Path) -> tuple[Path | None, Path | None]:
    """Return external bootstrap material and a disposable file to remove afterwards."""
    source = getattr(args, "bootstrap_credential_file", None)
    from_stdin = bool(getattr(args, "bootstrap_credential_stdin", False))
    if not source and not from_stdin:
        return None, None
    if source:
        path = Path(source).expanduser()
        try:
            info = path.lstat()
        except OSError as exc:
            raise InstallError("could not inspect bootstrap credential file") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise InstallError("bootstrap credential file must be a regular mode-0600 file")
        if not bootstrap_file_owner_is_allowed(info):
            raise InstallError("bootstrap credential file belongs to another user")
        try:
            validate_checkpoint_credential(path.read_bytes())
        except OSError as exc:
            raise InstallError("bootstrap credential file is unreadable") from exc
        except CredentialError as exc:
            raise InstallError(f"bootstrap credential content is rejected: {exc}") from None
        return path, None
    try:
        value = sys.stdin.buffer.read()
        validate_checkpoint_credential(value)
    except CredentialError as exc:
        raise InstallError(f"bootstrap credential content is rejected: {exc}") from None
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ummanu-bootstrap-credential-", dir=target.parent)
    path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
    except OSError as exc:
        raise InstallError("could not prepare bootstrap credential") from exc
    return path, path



def _runtime_env_file(instance_dir: Path, override: str | None) -> Path:
    """The env file this installation runs on, override included."""
    return instance_runtime_env_path(instance_dir, override)



def _recovery_phrase(args: argparse.Namespace, instance_dir: Path) -> str | None:
    """Read the phrase the same way a secret value is read: never from argv.

    A phrase on the command line lands in the process table and the shell history, and one in the
    environment is inherited by everything this command starts, so the only inputs are a file,
    standard input and a non-echoing terminal prompt. No flag and no terminal means no phrase.
    """
    path = getattr(args, "recovery_phrase_file", None)
    if path:
        source = Path(path).expanduser()
        try:
            raw = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InstallError(f"could not read the recovery phrase file: {exc}") from None
        return _clean_phrase(raw, f"{source} holds no recovery phrase")
    if getattr(args, "recovery_phrase_stdin", False):
        return _clean_phrase(sys.stdin.read(), "no recovery phrase on standard input")
    if is_initialized(instance_dir) and not key_path(instance_dir).exists() and sys.stdin.isatty():
        answer = getpass.getpass("Recovery phrase (empty to continue without it): ")
        if answer.strip():
            return _clean_phrase(answer, "no recovery phrase entered")
    return None



def _clean_phrase(raw: str, empty: str) -> str:
    try:
        return normalize_phrase(raw)
    except SecretStoreError:
        raise InstallError(empty) from None



def _open_secret_store(
    instance_dir: Path, runtime_env: Path, *, phrase: str | None, dry_run: bool
) -> SecretRecovery:
    """Open the store before anything asks for credentials.

    Which file `runtime-env` materializes into belongs to the installation being recovered, not to
    the host default, so the resolution override is pinned to this target for the duration of the
    write. That is also what keeps a recovery drill off a live installation's env file.
    """
    try:
        with _runtime_environment({"UMMANU_RUNTIME_ENV_FILE": str(runtime_env)}):
            return recover_secrets(instance_dir, phrase=phrase, dry_run=dry_run)
    except (SecretStoreError, StateRepoError) as exc:
        raise InstallError(f"secret store: {exc}") from None



def _checked_file_targets(instance_dir: Path) -> frozenset[Path]:
    """Refuse a catalog file target already in a data directory ummanu has not laid out, before the
    store writes anything; return the targets in the data directory this run may create.

    The store replaces a regular file at a target path, so once it has run nothing tells an operator's
    file there from the one it wrote. A target that is already there while the data directory holds
    no ummanu layout is therefore foreign and is refused by path, with no secret written. Only the
    targets returned here, absent before the store ran, count as this run's own afterwards
    (`_checked_data_target`). A laid-out data directory has its targets refreshed as before.
    """
    if not is_initialized(instance_dir):
        return frozenset()
    report = validate_instance(instance_dir)
    if not report.ok or report.data_dir is None:
        # The instance is refused right after the store step; there is no data directory to guard.
        return frozenset()
    data_dir = Path(report.data_dir).expanduser().resolve()
    try:
        targets = {
            _named_path(materialize_path(instance_dir, entry))
            for entry in list_secrets(instance_dir)
            if (entry.get("materialize") or {}).get("target") == MATERIALIZE_FILE
        }
    except (SecretStoreError, StateRepoError) as exc:
        raise InstallError(f"secret store: {exc}") from None
    inside = {path for path in targets if path.is_relative_to(data_dir)}
    present = sorted(path for path in inside if path.exists() or path.is_symlink())
    if present and not _valid_existing_layout(data_dir):
        raise InstallError(
            f"data target {data_dir} is not an installation created by ummanu, and it already holds "
            f"{', '.join(map(str, present))}, where the secret store would write; nothing was written: "
            "choose adopt or a clean recovery target"
        )
    return frozenset(inside - set(present))



def _secret_store_step(recovery: SecretRecovery) -> tuple[str, str]:
    if not recovery.store_present:
        return "skipped", "no secret store in the instance repo"
    if not recovery.unlocked:
        return "unchanged", recovery.summary()
    return "changed" if recovery.changed else "unchanged", recovery.summary()



def _add_secret_steps(result: InstallResult, recovery: SecretRecovery) -> None:
    """One line per secret that did not come back, ids and targets only."""
    for status, entries in (("locked", recovery.locked), ("missing", recovery.missing)):
        for entry in entries:
            where = entry.get("path") or entry.get("target") or "not materialized"
            result.add(f"secret:{entry['id']}", status, f"{entry.get('environment', '-')} -> {where}")
    for path in recovery.withheld:
        result.add(f"secret-file:{path}", "withheld", "a secret this file needs is not readable")



def _blocked_by_secrets(cause: InstallError, recovery: SecretRecovery, runtime_env: Path) -> InstallError:
    """Say what is still closed instead of asking for a hand-written file."""
    reason = (
        str(cause) if runtime_env.exists() else f"{runtime_env} is not there, and the store is what writes it"
    )
    lines = [f"recovery is incomplete: {recovery.summary()}", f"  cause: {reason}"]
    lines.extend(recovery.render())
    return InstallError("\n".join(lines))



@contextmanager
def _runtime_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value



def add_bootstrap_credential_arguments(parser) -> None:
    """The one declaration of the external clone credential, shared by `install`, `recover` and `bootstrap`."""
    bootstrap = parser.add_mutually_exclusive_group()
    bootstrap.add_argument(
        "--bootstrap-credential-file",
        help="mode-0600 external GitHub credential used only to clone a private instance remote",
    )
    bootstrap.add_argument(
        "--bootstrap-credential-stdin",
        action="store_true",
        help="read a one-line external bootstrap credential from standard input before clone",
    )
