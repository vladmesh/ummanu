"""Recoverable secret store: envelope format, installation key, open catalog.

Contract: docs/RECOVERY.md, "Secrets" and "Writers". The store lives in the live root, so it rides
the same recovery chain as the board, the runs and the knowledge plane:

    <live root>/
      secrets/
        catalog.yaml          open metadata, exported, redact-scanned
        installation-key.json open KDF parameters plus a verifier, exported
        installation.key      raw key, mode 0600, never exported
        values/<id>.enc.json  one versioned envelope per secret, exported

Two keys, two jobs. The installation key opens the values after a reboot without a human. The
recovery phrase exists only to rebuild that key on a clean host: it is generated here, shown once
and never stored. `installation-key.json` holds the KDF id and its parameters in the open, plus a
verifier so a mistyped phrase fails loudly instead of producing a plausible-looking wrong key.

Every envelope carries its own format version, KDF id, KDF parameters and AEAD id in the clear
next to the ciphertext; nothing about how a value was sealed lives only in this module's
constants. The primitives are `cryptography`'s (Scrypt, HKDF, ChaCha20-Poly1305).

The store starts no Git child. "Exported" means matched by the snapshot export allowlist
(`infra.export_allowlist.is_exported`); `installation.key` is not, and every write refuses to run if
it ever were. Store writes take `state_repo.state_repo_lock`, the live-root writer lock the tick
holds while it commits or cuts, and land all or nothing: each one keeps the prior bytes of every
path it replaces or removes in the undo area `secrets/.undo` (the memory canon's transaction), so a
failure restores `secrets/` byte for byte and a crash is restored by the next writer before it reads
anything. The catalog and the values it names therefore never diverge in a checkpoint. Where a commit
id used to be, results carry the store's content revision (:func:`store_revision`).

A secret read as an environment variable also carries a `materialize` record — variable name,
file, line — which is what lets a recovered installation put its env files back without a human
listing paths. `materialize_secrets` writes each file whole and by rename, because `runtime.env`
is read by systemd on every unit start and may never be seen missing or half-written.

The env-file format the store round-trips is `KEY=VALUE` lines, LF, one variable per line,
nothing else: no comments, no blank lines, no padding, and a final newline. `import` refuses
anything outside it rather than take in a file it would hand back as different bytes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets as pysecrets
import stat
import tempfile
from collections.abc import Container, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ummanu import state_repo
from ummanu._fsutil import files_revision
from ummanu.config import _safe_yaml_error, validate
from ummanu.infra.export_allowlist import is_exported
from ummanu.memory.canon import CanonTransaction, canon_transaction, recover_canon_undo
from ummanu.runtime import role_env
from ummanu.runtime.redact import looks_like_credential, redact
from ummanu.runtime_env import RuntimeEnvError, parse_env_value
from ummanu.secret_words import RECOVERY_WORDS

CATALOG_NAME = "catalog.yaml"
KEY_PARAMS_NAME = "installation-key.json"
KEY_NAME = "installation.key"
VALUES_DIRNAME = "values"
SECRETS_DIR_NAME = "secrets"
VALUE_SUFFIX = ".enc.json"

# The key's live-root path. It must never match the export allowlist; that is its whole exclusion.
KEY_RELATIVE = "secrets/installation.key"
CATALOG_VERSION = 1

KEY_PARAMS_FORMAT = "ummanu.installation-key"
KEY_PARAMS_VERSION = 1
PHRASE_KDF_ID = "scrypt"
PHRASE_KDF_N = 2**16
PHRASE_KDF_R = 8
PHRASE_KDF_P = 1
# Recorded v1 parameters may differ from today's defaults, but recovery never accepts
# unbounded work from an exported file. These ceilings allow four default derivations.
_SCRYPT_MAX_MEMORY = 256 * 1024 * 1024
_SCRYPT_MAX_WORK = 2**21
_SCRYPT_MAX_R = 32
_SCRYPT_MAX_P = 16
KEY_LENGTH = 32
VERIFIER_PLAINTEXT = b"ummanu installation key v1"
VERIFIER_AAD = b"ummanu/installation-key/v1"

ENVELOPE_FORMAT = "ummanu.secret-envelope"
ENVELOPE_VERSION = 1
VALUE_KDF_ID = "hkdf-sha256"
VALUE_KDF_INFO = "ummanu/secret/v1"
AEAD_ID = "chacha20poly1305"
NONCE_LENGTH = 12
SALT_LENGTH = 16

PHRASE_WORDS = 16
CONFIRM_WORDS = 3

INSTALLATION_SCOPE = "installation"
PROJECT_SCOPE_PREFIX = "project:"

# Where a value goes when it is materialized. `runtime-env` is the installation's
# own env file, whose path only `role_env.runtime_env_path()` may answer; `file`
# names any other env file, and carries the path in the catalog.
MATERIALIZE_RUNTIME_ENV = "runtime-env"
MATERIALIZE_FILE = "file"
MATERIALIZE_TARGETS = (MATERIALIZE_RUNTIME_ENV, MATERIALIZE_FILE)

_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretStoreError(RuntimeError):
    """A store operation did not happen."""


class SecretStoreValidationError(SecretStoreError):
    """The request is not something the store accepts."""


class SecretStoreStateError(SecretStoreError):
    """The store on disk is not in the state this operation needs."""


class RecoveryPhraseError(SecretStoreError):
    """The recovery phrase does not open this installation key."""


# Every result's `commit` is the store's content revision after the operation (`store_revision`), not
# a Git commit: the store makes none. An operation that changed nothing answers the current revision.


@dataclass(frozen=True)
class InitResult:
    key_path: Path
    catalog_path: Path
    commit: str


@dataclass(frozen=True)
class SetResult:
    secret_id: str
    scope: str
    path: Path
    commit: str
    created: bool


@dataclass(frozen=True)
class RemoveResult:
    secret_id: str
    path: Path
    commit: str


@dataclass(frozen=True)
class ImportResult:
    """What one `import` did, per secret id. Values never appear here."""

    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    commit: str


@dataclass(frozen=True)
class MaterializeResult:
    target: str
    path: Path
    variables: tuple[str, ...]
    changed: bool


def secrets_dir(instance_dir: Path) -> Path:
    return state_repo.secrets_dir(instance_dir)


def catalog_path(instance_dir: Path) -> Path:
    return secrets_dir(instance_dir) / CATALOG_NAME


def key_params_path(instance_dir: Path) -> Path:
    return secrets_dir(instance_dir) / KEY_PARAMS_NAME


def key_path(instance_dir: Path) -> Path:
    return secrets_dir(instance_dir) / KEY_NAME


def value_path(instance_dir: Path, secret_id: str) -> Path:
    return secrets_dir(instance_dir) / VALUES_DIRNAME / f"{secret_id}{VALUE_SUFFIX}"


def is_initialized(instance_dir: Path) -> bool:
    return key_params_path(instance_dir).exists() and catalog_path(instance_dir).exists()


def _store_exists(instance_dir: Path) -> bool:
    """Whether `secrets/` holds any trace of a store, complete or not.

    `is_initialized` requires the catalog and the key params together; health and findings need to
    tell a partial store apart from a directory that was never touched.
    """
    if (
        catalog_path(instance_dir).exists()
        or key_params_path(instance_dir).exists()
        or key_path(instance_dir).exists()
    ):
        return True
    values_dir = secrets_dir(instance_dir) / VALUES_DIRNAME
    return values_dir.is_dir() and any(values_dir.iterdir())


def generate_recovery_phrase(words: int = PHRASE_WORDS) -> str:
    """A fresh phrase with `words` * 8 bits of entropy, chosen by the product."""
    if words < 8:
        raise SecretStoreValidationError("a recovery phrase needs at least 8 words")
    return " ".join(pysecrets.choice(RECOVERY_WORDS) for _ in range(words))


def normalize_phrase(phrase: str) -> str:
    """Collapse the shape a human types back to the shape that was generated."""
    normalized = " ".join(str(phrase).lower().split())
    if not normalized:
        raise SecretStoreValidationError("recovery phrase is empty")
    return normalized


def _new_key_params() -> dict[str, Any]:
    return {
        "format": KEY_PARAMS_FORMAT,
        "version": KEY_PARAMS_VERSION,
        "kdf": {
            "id": PHRASE_KDF_ID,
            "salt": _b64(pysecrets.token_bytes(SALT_LENGTH)),
            "length": KEY_LENGTH,
            "n": PHRASE_KDF_N,
            "r": PHRASE_KDF_R,
            "p": PHRASE_KDF_P,
        },
    }


def _derive_key(phrase: str, params: dict[str, Any]) -> bytes:
    kdf = params.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("id") != PHRASE_KDF_ID:
        raise SecretStoreStateError("unsupported installation key kdf")
    try:
        length, n, r, p = (kdf[name] for name in ("length", "n", "r", "p"))
        if (
            any(type(value) is not int for value in (length, n, r, p))
            or length != KEY_LENGTH
            or n < 2
            or n & (n - 1)
            or r < 1
            or r > _SCRYPT_MAX_R
            or p < 1
            or p > _SCRYPT_MAX_P
        ):
            raise SecretStoreStateError("installation key parameters are unusable")
        if 128 * n * r > _SCRYPT_MAX_MEMORY or n * r * p > _SCRYPT_MAX_WORK:
            raise SecretStoreStateError("installation key kdf exceeds supported resource limits")
        derivation = Scrypt(
            salt=_unb64(kdf["salt"], "installation key salt", length=SALT_LENGTH),
            length=length,
            n=n,
            r=r,
            p=p,
        )
        return derivation.derive(normalize_phrase(phrase).encode("utf-8"))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SecretStoreStateError("installation key parameters are unusable") from None


def _seal_verifier(key: bytes) -> dict[str, str]:
    nonce = pysecrets.token_bytes(NONCE_LENGTH)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, VERIFIER_PLAINTEXT, VERIFIER_AAD)
    return {"id": AEAD_ID, "nonce": _b64(nonce), "ciphertext": _b64(sealed)}


def _check_verifier(key: bytes, params: dict[str, Any]) -> None:
    verifier = params.get("verifier")
    if not isinstance(verifier, dict) or verifier.get("id") != AEAD_ID:
        raise SecretStoreStateError("installation key file carries no usable verifier")
    try:
        opened = ChaCha20Poly1305(key).decrypt(
            _unb64(verifier["nonce"], "verifier nonce", length=NONCE_LENGTH),
            _unb64(verifier["ciphertext"], "verifier ciphertext"),
            VERIFIER_AAD,
        )
    except InvalidTag:
        raise RecoveryPhraseError(
            "recovery phrase does not match this installation; nothing was written"
        ) from None
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SecretStoreStateError("installation key verifier is damaged") from None
    if opened != VERIFIER_PLAINTEXT:
        raise RecoveryPhraseError("recovery phrase does not match this installation")


def load_installation_key(instance_dir: Path) -> bytes:
    """Read the key from disk, refusing a file the wrong user or mode owns."""
    path = key_path(instance_dir)
    try:
        info = path.lstat()
    except OSError:
        raise SecretStoreStateError(
            f"installation key is missing: {path}; restore it from the recovery phrase"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise SecretStoreStateError("installation key must be a regular file, not a symlink")
    if info.st_mode & 0o077:
        raise SecretStoreStateError("installation key permissions are too broad; run chmod 0600")
    if info.st_uid != os.geteuid():
        raise SecretStoreStateError("installation key belongs to another user")
    try:
        material = _unb64(path.read_text(encoding="utf-8").strip(), "installation key")
    except (OSError, UnicodeError):
        raise SecretStoreStateError("could not read the installation key") from None
    if len(material) != KEY_LENGTH:
        raise SecretStoreStateError("installation key has the wrong length")
    _check_verifier(material, _read_key_params(instance_dir))
    return material


def verify_recovery_phrase(instance_dir: Path, phrase: str) -> None:
    """Answer whether the phrase opens this store, touching no file."""
    params = _read_key_params(instance_dir)
    _check_verifier(_derive_key(phrase, params), params)


def restore_installation_key(instance_dir: Path, phrase: str) -> Path:
    """Rebuild the key file from the phrase. Wrong phrase writes nothing."""
    instance_dir = _live_root(instance_dir)
    params = _read_key_params(instance_dir)
    key = _derive_key(phrase, params)
    _check_verifier(key, params)
    with _locked_store(instance_dir):
        _assert_key_not_exported()
        _write_key_file(key_path(instance_dir), key)
    return key_path(instance_dir)


def _write_key_file(path: Path, key: bytes) -> None:
    """Write the raw key 0600 without ever leaving it world-readable."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_b64(key) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise SecretStoreError(
            f"could not write the installation key: {exc.strerror or 'I/O error'}"
        ) from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _read_key_params(instance_dir: Path) -> dict[str, Any]:
    path = key_params_path(instance_dir)
    try:
        params = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SecretStoreStateError(
            "secret store is not initialized; run `ummanu secret init` first"
        ) from None
    except (OSError, ValueError):
        raise SecretStoreStateError(f"could not read {KEY_PARAMS_NAME}") from None
    if not isinstance(params, dict) or params.get("format") != KEY_PARAMS_FORMAT:
        raise SecretStoreStateError(f"{KEY_PARAMS_NAME} is not an installation key file")
    if params.get("version") != KEY_PARAMS_VERSION:
        raise SecretStoreStateError(
            f"{KEY_PARAMS_NAME} has an unsupported format version; "
            f"this product reads version {KEY_PARAMS_VERSION}"
        )
    return params


def seal_value(key: bytes, secret_id: str, value: bytes) -> dict[str, Any]:
    """Wrap one value. Everything needed to open it later is in the result."""
    salt = pysecrets.token_bytes(SALT_LENGTH)
    nonce = pysecrets.token_bytes(NONCE_LENGTH)
    header = {
        "format": ENVELOPE_FORMAT,
        "version": ENVELOPE_VERSION,
        "id": secret_id,
        "kdf": {
            "id": VALUE_KDF_ID,
            "salt": _b64(salt),
            "length": KEY_LENGTH,
            "info": VALUE_KDF_INFO,
        },
        "aead": {"id": AEAD_ID, "nonce": _b64(nonce)},
    }
    subkey = _derive_value_key(key, header)
    ciphertext = ChaCha20Poly1305(subkey).encrypt(nonce, value, _header_bytes(header))
    return {**header, "ciphertext": _b64(ciphertext)}


def open_value(key: bytes, envelope: dict[str, Any]) -> bytes:
    """Unwrap one envelope, reading its own declared parameters, not ours."""
    if not isinstance(envelope, dict) or envelope.get("format") != ENVELOPE_FORMAT:
        raise SecretStoreStateError("value file is not a secret envelope")
    version = envelope.get("version")
    if version != ENVELOPE_VERSION:
        raise SecretStoreStateError(
            "envelope has an unsupported format version; "
            f"this product reads version {ENVELOPE_VERSION}; upgrade ummanu"
        )
    aead = envelope.get("aead")
    if not isinstance(aead, dict) or aead.get("id") != AEAD_ID:
        raise SecretStoreStateError("unsupported envelope aead")
    header = {name: field for name, field in envelope.items() if name != "ciphertext"}
    subkey = _derive_value_key(key, header)
    try:
        return ChaCha20Poly1305(subkey).decrypt(
            _unb64(aead["nonce"], "envelope nonce", length=NONCE_LENGTH),
            _unb64(envelope["ciphertext"], "envelope ciphertext"),
            _header_bytes(header),
        )
    except (InvalidTag, KeyError, TypeError, ValueError, OverflowError):
        raise SecretStoreStateError(
            "could not open the secret value: wrong installation key or a damaged envelope"
        ) from None


def _derive_value_key(key: bytes, header: dict[str, Any]) -> bytes:
    kdf = header.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("id") != VALUE_KDF_ID:
        raise SecretStoreStateError("unsupported envelope kdf")
    try:
        if type(kdf["length"]) is not int or kdf["length"] != KEY_LENGTH:
            raise SecretStoreStateError("envelope key length is unusable")
        if not isinstance(kdf["info"], str) or not isinstance(header.get("id"), str):
            raise SecretStoreStateError("envelope parameters are unusable")
        derivation = HKDF(
            algorithm=SHA256(),
            length=kdf["length"],
            salt=_unb64(kdf["salt"], "envelope salt", length=SALT_LENGTH),
            info=f"{kdf['info']}:{header.get('id')}".encode(),
        )
        return derivation.derive(key)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SecretStoreStateError("envelope parameters are unusable") from None


def _header_bytes(header: dict[str, Any]) -> bytes:
    """The open part of the envelope, bound into the AEAD tag.

    Serialized with sorted keys and no spaces so the bytes are the same whether the header was just
    built or parsed back from the file.
    """
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_catalog(instance_dir: Path) -> dict[str, Any]:
    path = catalog_path(instance_dir)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SecretStoreStateError(
            "secret store is not initialized; run `ummanu secret init` first"
        ) from None
    except yaml.YAMLError as exc:
        raise SecretStoreStateError(f"{CATALOG_NAME} is invalid: {_safe_yaml_error(exc)}") from None
    except OSError as exc:
        raise SecretStoreStateError(
            f"could not read {CATALOG_NAME}: {exc.strerror or 'unreadable'}"
        ) from None
    errors = validate(data, "secret-catalog", f"secrets/{CATALOG_NAME}")
    if errors:
        raise SecretStoreStateError(f"{CATALOG_NAME} is invalid: {errors[0]}")
    return data


def list_secrets(instance_dir: Path) -> tuple[dict[str, Any], ...]:
    """Catalog metadata only. There is no path from here to a value."""
    return tuple(load_catalog(instance_dir)["secrets"])


def store_divergence(instance_dir: Path) -> tuple[str, ...]:
    """Names the catalog and the values directory disagree about."""
    catalogued = {entry["id"] for entry in list_secrets(instance_dir)}
    root = secrets_dir(instance_dir) / VALUES_DIRNAME
    stored = set()
    if root.is_dir():
        stored = {
            path.name[: -len(VALUE_SUFFIX)]
            for path in root.iterdir()
            if path.is_file() and path.name.endswith(VALUE_SUFFIX)
        }
    return tuple(
        sorted(
            [f"{name}: catalogued with no value" for name in catalogued - stored]
            + [f"{name}: value with no catalog entry" for name in stored - catalogued]
        )
    )


def _catalog_text(catalog: dict[str, Any]) -> str:
    return yaml.safe_dump(catalog, sort_keys=True, allow_unicode=True, default_flow_style=False)


# `status` and `doctor` use these metadata-only functions; they never read values or key material.


def store_health(instance_dir: Path) -> dict[str, Any]:
    """Non-secret snapshot of the store for `ummanu status --json`.

    A `secrets/` directory holding none of catalog, key params or key file reports as absent rather
    than raising: an installation with no secrets yet is a valid state. Any other partial shape
    reports as present.
    """
    instance_dir = Path(instance_dir)
    if not _store_exists(instance_dir):
        return {
            "initialized": False,
            "secret_count": 0,
            "last_modified_at": None,
            "installation_key": {"present": False, "usable": None},
            "materialize": [],
        }
    try:
        secrets = list_secrets(instance_dir)
    except SecretStoreError:
        secrets = ()
    present, usable = _key_presence(instance_dir)
    return {
        "initialized": is_initialized(instance_dir),
        "secret_count": len(secrets),
        "last_modified_at": _mtime(catalog_path(instance_dir)),
        "installation_key": {"present": present, "usable": usable},
        "materialize": _materialize_summary(tuple(secrets)),
    }


def store_findings(instance_dir: Path) -> tuple[str, ...]:
    """Everything wrong with the store on disk, for `ummanu doctor`.

    An empty `secrets/` directory gives no findings: absence is a valid state. Any other shape is
    checked against a catalog/values divergence, an installation key with permissions wider than
    0600, and a key that is missing or does not open a non-empty store.
    """
    instance_dir = Path(instance_dir)
    if not _store_exists(instance_dir):
        return ()
    findings: list[str] = []
    try:
        secrets = list_secrets(instance_dir)
    except SecretStoreError as exc:
        findings.append(f"secret store: {exc}")
        secrets = ()
    else:
        findings.extend(f"secret store: {item}" for item in store_divergence(instance_dir))

    path = key_path(instance_dir)
    wide_permissions = False
    try:
        info = path.lstat()
    except OSError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode) and (info.st_mode & 0o077):
        wide_permissions = True
        findings.append(f"secret store: installation key permissions are too broad; run chmod 0600 {path}")

    if secrets and not wide_permissions:
        try:
            load_installation_key(instance_dir)
        except SecretStoreError as exc:
            findings.append(f"secret store: installation key is missing or unusable: {exc}")
    return tuple(findings)


def _key_presence(instance_dir: Path) -> tuple[bool, bool | None]:
    """Whether a key file is there, and whether it opens this store.

    `usable` is `None` when there is no key file to judge, so a status reader cannot mistake
    "absent" for "present but broken".
    """
    try:
        path = key_path(instance_dir)
        if not stat.S_ISREG(path.lstat().st_mode):
            return True, False
    except OSError:
        return False, None
    try:
        load_installation_key(instance_dir)
    except SecretStoreError:
        return True, False
    return True, True


def _materialize_summary(secrets: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """One row per materialization target, counted, never with a secret's name."""
    counts: dict[tuple[str, str], int] = {}
    for entry in secrets:
        instruction = entry.get("materialize")
        if not instruction:
            continue
        slot = _materialize_slot(instruction)
        counts[slot] = counts.get(slot, 0) + 1
    return [
        {"target": target, "path": path or None, "count": count}
        for (target, path), count in sorted(counts.items())
    ]


def initialize_store(instance_dir: Path, *, phrase: str, actor: str) -> InitResult:
    """Create the key, the key parameters and an empty catalog, all or nothing. Never overwrites."""
    actor = _clean_actor(actor)
    instance_dir = _live_root(instance_dir)
    params = _new_key_params()
    key = _derive_key(phrase, params)
    params["verifier"] = _seal_verifier(key)
    catalog = {"version": CATALOG_VERSION, "secrets": []}

    with _locked_store(instance_dir):
        if key_params_path(instance_dir).exists() or catalog_path(instance_dir).exists():
            raise SecretStoreStateError(
                "secret store is already initialized; init will not overwrite it. "
                "Rotating the recovery phrase is a separate operation."
            )
        # Checked before the key exists, so no window holds a key the export would copy.
        _assert_key_not_exported()
        catalog_text = _catalog_text(catalog)
        params_text = json.dumps(params, indent=2, sort_keys=True) + "\n"
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        _scan_open_file(f"secrets/{KEY_PARAMS_NAME}", params_text)
        with _store_write(instance_dir) as transaction:
            transaction.guard(key_path(instance_dir))
            _write_key_file(key_path(instance_dir), key)
            _write(transaction, key_params_path(instance_dir), params_text)
            _write(transaction, catalog_path(instance_dir), catalog_text)
        revision = store_revision(instance_dir)
    return InitResult(
        key_path=key_path(instance_dir),
        catalog_path=catalog_path(instance_dir),
        commit=revision,
    )


def set_secret(
    instance_dir: Path,
    *,
    secret_id: str,
    value: bytes,
    scope: str,
    purpose: str,
    actor: str,
    environment: str | None = None,
    materialize: dict[str, Any] | None = None,
) -> SetResult:
    """Seal one value and record its metadata, as one all-or-nothing store write."""
    actor = _clean_actor(actor)
    secret_id = _clean_secret_id(secret_id)
    scope = _clean_scope(scope)
    purpose = _clean_purpose(purpose)
    environment = _clean_environment(environment)
    materialize = _clean_materialize(materialize)
    _check_value(value)

    instance_dir = _live_root(instance_dir)
    with _locked_store(instance_dir):
        key = load_installation_key(instance_dir)
        # Re-checked on every write, not only at init: this is the moment a key that came to be
        # exported would leave the host with the next checkpoint.
        _assert_key_not_exported()
        catalog = load_catalog(instance_dir)
        entries = {entry["id"]: dict(entry) for entry in catalog["secrets"]}
        existing = entries.get(secret_id)
        entries[secret_id] = _entry(
            secret_id,
            scope=scope,
            purpose=purpose,
            environment=environment,
            materialize=_assign_order(
                entries, secret_id=secret_id, materialize=materialize, existing=existing
            ),
            existing=existing,
        )
        catalog = _catalog(entries)

        # A retried credential entry must not generate a new random envelope: an envelope whose
        # plaintext is already this value is never rewritten or re-encrypted, and a request whose
        # metadata is unchanged as well writes nothing at all. This makes the generic `secret set`
        # safe to repeat when it already describes the canonical value.
        stored = None if existing is None else _stored_value(instance_dir, secret_id, key)
        value_unchanged = stored == bytes(value)
        if value_unchanged and entries[secret_id] == existing:
            return SetResult(
                secret_id=secret_id,
                scope=scope,
                path=value_path(instance_dir, secret_id),
                commit=store_revision(instance_dir),
                created=False,
            )

        catalog_text = _catalog_text(catalog)
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        with _store_write(instance_dir) as transaction:
            if not value_unchanged:
                # Never redact-scan ciphertext; a base64 coincidence must not erase a secret.
                envelope_text = (
                    json.dumps(seal_value(key, secret_id, bytes(value)), indent=2, sort_keys=True) + "\n"
                )
                _write(transaction, value_path(instance_dir, secret_id), envelope_text)
            _write(transaction, catalog_path(instance_dir), catalog_text)
        revision = store_revision(instance_dir)
    return SetResult(
        secret_id=secret_id,
        scope=scope,
        path=value_path(instance_dir, secret_id),
        commit=revision,
        created=existing is None,
    )


def read_secret(instance_dir: Path, secret_id: str) -> bytes:
    """Internal API. No command in this card puts the result on stdout.

    A read needs no repository: the snapshot exporter reads redaction values from a live root that
    is a plain directory.
    """
    secret_id = _clean_secret_id(secret_id)
    instance_dir = Path(instance_dir).expanduser().resolve()
    if not any(entry["id"] == secret_id for entry in list_secrets(instance_dir)):
        raise SecretStoreStateError(f"no secret named {secret_id!r} in the catalog")
    return _read_value(instance_dir, secret_id, load_installation_key(instance_dir))


def redaction_values(instance_dir: Path) -> tuple[str, ...]:
    """Return plaintext values that an instance's writers must redact.

    An entry is included only when the canonical runtime name marks it sensitive or its plaintext
    independently looks like a known credential (URL userinfo included), so custom credential names
    are protected without turning UMMANU_DATA_DIR into an over-broad exact-value rule.

    A locked or partial store contributes no plaintext values. It stays a separate doctor finding;
    treating it as a checkpoint gate would turn a recovery that deliberately left stale encrypted
    credentials locked into a permanent durability outage.
    """
    instance_dir = Path(instance_dir).expanduser()
    values: list[str] = []
    if _store_exists(instance_dir) and is_initialized(instance_dir) and key_path(instance_dir).is_file():
        try:
            for entry in list_secrets(instance_dir):
                secret_id = str(entry["id"])
                environment = str(entry.get("environment") or "")
                try:
                    plaintext = read_secret(instance_dir, secret_id)
                    value = plaintext.decode("utf-8", errors="strict")
                except (SecretStoreError, UnicodeDecodeError):
                    # A valid binary secret cannot appear in text verbatim.  A
                    # missing/bad envelope remains visible through store_findings;
                    # one entry must not make us forget other readable credentials.
                    continue
                forms = [value]
                if environment:
                    with suppress(RuntimeEnvError):
                        decoded = parse_env_value(value)
                        if decoded != value:
                            forms.append(decoded)
                if role_env.is_sensitive_env_name(environment) or any(
                    looks_like_credential(form) for form in forms
                ):
                    values.extend(forms)
        except SecretStoreError:
            pass
    return tuple(values)


def remove_secret(instance_dir: Path, *, secret_id: str, actor: str) -> RemoveResult:
    """Drop the catalog entry and its envelope together, all or nothing.

    A missing id is an error, not a quiet success: hiding it turns a typo into a secret nobody knows
    is still stored under its real name.
    """
    actor = _clean_actor(actor)
    secret_id = _clean_secret_id(secret_id)
    instance_dir = _live_root(instance_dir)
    with _locked_store(instance_dir):
        catalog = load_catalog(instance_dir)
        entries = {entry["id"]: dict(entry) for entry in catalog["secrets"]}
        if secret_id not in entries:
            raise SecretStoreStateError(f"no secret named {secret_id!r} in the catalog")
        del entries[secret_id]
        catalog_text = _catalog_text(_catalog(entries))
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        path = value_path(instance_dir, secret_id)
        with _store_write(instance_dir) as transaction:
            _write(transaction, catalog_path(instance_dir), catalog_text)
            try:
                transaction.remove(path)
            except SecretStoreError:
                raise
            except RuntimeError as exc:
                raise SecretStoreError(f"could not remove {secret_id!r}: {exc}") from None
        revision = store_revision(instance_dir)
    return RemoveResult(secret_id=secret_id, path=path, commit=revision)


def import_env_file(
    instance_dir: Path,
    *,
    source: Path,
    scope: str,
    purpose: str,
    actor: str,
    materialize: dict[str, Any] | None = None,
) -> ImportResult:
    """Take an existing env file into the store, one secret per variable.

    The file's own line order is what the catalog records, so `materialize` puts the same bytes back.
    Idempotent by content: a variable whose sealed value and metadata already match is left alone,
    envelope bytes included, so re-importing the same file adds no duplicates and writes nothing.
    """
    actor = _clean_actor(actor)
    scope = _clean_scope(scope)
    purpose = _clean_purpose(purpose)
    materialize = _clean_materialize(materialize)
    source = Path(source).expanduser()
    try:
        # Bytes, then decode: read_text would translate CRLF into LF and hide a
        # file whose bytes this store cannot give back.
        text = source.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise SecretStoreValidationError(f"env file not found: {source}") from None
    except (OSError, UnicodeError) as exc:
        raise SecretStoreValidationError(f"could not read {source}: {exc}") from None
    variables = parse_env_file(text, source=str(source))
    if not variables:
        raise SecretStoreValidationError(f"{source} defines no variables")
    _assert_distinct_secret_ids(variables, source=str(source))

    instance_dir = _live_root(instance_dir)
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    with _locked_store(instance_dir):
        key = load_installation_key(instance_dir)
        _assert_key_not_exported()
        before = {entry["id"]: dict(entry) for entry in load_catalog(instance_dir)["secrets"]}
        entries = {name: dict(entry) for name, entry in before.items()}
        imported = {secret_id_for_variable(name) for name in variables}
        if materialize:
            _shift_foreign_lines(entries, materialize, len(variables), keep=imported)
        writes: list[tuple[Path, str]] = []
        for line, (name, raw) in enumerate(variables.items()):
            secret_id = secret_id_for_variable(name)
            value = raw.encode("utf-8")
            if not value:
                raise SecretStoreValidationError(
                    f"{source}: {name} has an empty value; the store holds no empty secrets"
                )
            existing = entries.get(secret_id)
            if existing is not None and existing.get("environment", name) != name:
                raise SecretStoreValidationError(
                    f"{source}: {name} would take over secret {secret_id!r}, which already holds "
                    f"{existing['environment']}; remove one of them before importing"
                )
            entry = _entry(
                secret_id,
                scope=scope,
                purpose=purpose,
                environment=name,
                materialize={**materialize, "order": line} if materialize else None,
                existing=existing,
            )
            entries[secret_id] = entry
            stored = None if existing is None else _stored_value(instance_dir, secret_id, key)
            if existing is None:
                created.append(secret_id)
            elif existing == entry and stored == value:
                unchanged.append(secret_id)
                continue
            else:
                updated.append(secret_id)
            if stored == value:
                # Metadata moved, the value did not. Resealing would rewrite the
                # envelope with a fresh nonce for nothing.
                continue
            writes.append(
                (
                    value_path(instance_dir, secret_id),
                    json.dumps(seal_value(key, secret_id, value), indent=2, sort_keys=True) + "\n",
                )
            )

        if not writes and entries == before:
            return ImportResult(
                created=(),
                updated=(),
                unchanged=tuple(unchanged),
                commit=store_revision(instance_dir),
            )
        catalog_text = _catalog_text(_catalog(entries))
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        with _store_write(instance_dir) as transaction:
            for path, text in writes:
                _write(transaction, path, text)
            _write(transaction, catalog_path(instance_dir), catalog_text)
        revision = store_revision(instance_dir)
    return ImportResult(
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        commit=revision,
    )


def materialize_secrets(
    instance_dir: Path, *, target: str | None = None, paths: Container[Path] | None = None
) -> tuple[MaterializeResult, ...]:
    """Write every materializing secret into its env file.

    One file per target, written whole, so a variable dropped from the catalog is gone from the file.
    `target` narrows to one file; `paths` lets recovery leave a file alone when one of its secrets is
    unreadable rather than publish an env file with a line missing. Line order is the catalog's
    `materialize.order`, so an imported file comes back byte for byte.
    """
    if target is not None and target not in MATERIALIZE_TARGETS:
        raise SecretStoreValidationError(
            f"unknown materialization target {target!r}; expected one of " + ", ".join(MATERIALIZE_TARGETS)
        )
    instance_dir = _live_root(instance_dir)
    with _locked_store(instance_dir):
        key = load_installation_key(instance_dir)
        groups: dict[Path, list[dict[str, Any]]] = {}
        for entry in list_secrets(instance_dir):
            instruction = entry.get("materialize")
            if not instruction:
                continue
            if target is not None and instruction.get("target") != target:
                continue
            path = materialize_path(instance_dir, entry)
            if paths is not None and path not in paths:
                continue
            groups.setdefault(path, []).append(entry)

        results = []
        for path in sorted(groups):
            entries = sorted(
                groups[path],
                key=lambda item: (item["materialize"].get("order", 0), item["environment"]),
            )
            _assert_one_secret_per_variable(path, entries)
            _assert_one_secret_per_line(path, entries)
            _assert_writable_target(instance_dir, path)
            lines = []
            for entry in entries:
                value = _read_value(instance_dir, entry["id"], key)
                lines.append(f"{entry['environment']}={_env_value(entry, value)}\n")
            changed = _publish_env_file(path, "".join(lines))
            results.append(
                MaterializeResult(
                    target=entries[0]["materialize"]["target"],
                    path=path,
                    variables=tuple(entry["environment"] for entry in entries),
                    changed=changed,
                )
            )
    return tuple(results)


def materialize_path(instance_dir: Path, entry: dict[str, Any]) -> Path:
    """Resolve one catalog entry's materialization target to a path.

    `runtime-env` never carries a path of its own: the installation's env file is whatever `role_env`
    says it is, override included, so the store and a launched head always mean the same file.
    """
    instruction = entry.get("materialize") or {}
    target = instruction.get("target")
    if target == MATERIALIZE_RUNTIME_ENV:
        return role_env.runtime_env_path()
    if target == MATERIALIZE_FILE:
        path = Path(str(instruction.get("path", ""))).expanduser()
        if not str(path):
            raise SecretStoreStateError(f"secret {entry.get('id')!r} materializes to a file with no path")
        return path if path.is_absolute() else instance_dir / path
    raise SecretStoreStateError(
        f"secret {entry.get('id')!r} has an unknown materialization target {target!r}"
    )


def parse_env_file(text: str, *, source: str = "env file") -> dict[str, str]:
    """Read the env-file format the store can hand back byte for byte.

    The store keeps variable names, values and line order and nothing else, so a comment, a blank
    line, a stray space or a CR would be dropped on the way in and could not be put back: a file
    carrying one is refused rather than round-tripped into a different file. Values are taken
    literally, with no unquoting. Returns the variables in file order.
    """
    if not text:
        return {}
    if "\r" in text:
        raise SecretStoreValidationError(
            f"{source} has CR line endings; the store keeps env files in LF only"
        )
    if not text.endswith("\n"):
        raise SecretStoreValidationError(f"{source} does not end with a newline")
    values: dict[str, str] = {}
    for number, line in enumerate(text[:-1].split("\n"), 1):
        if not line.strip():
            raise SecretStoreValidationError(
                f"{source} line {number} is blank; the store keeps no blank lines"
            )
        if line.startswith("#"):
            raise SecretStoreValidationError(
                f"{source} line {number} is a comment; the store keeps no comments"
            )
        if line != line.strip():
            raise SecretStoreValidationError(
                f"{source} line {number} is padded with whitespace; write it as KEY=VALUE"
            )
        if line.startswith("export ") or "=" not in line:
            raise SecretStoreValidationError(f"{source} line {number} must use KEY=VALUE syntax")
        name, value = line.split("=", 1)
        if not _ENV_NAME_RE.match(name):
            raise SecretStoreValidationError(f"{source} line {number} has an invalid variable name")
        if name in values:
            raise SecretStoreValidationError(f"{source} defines {name} twice")
        try:
            parse_env_value(value)
        except RuntimeEnvError as exc:
            raise SecretStoreValidationError(f"{source} line {number}: {exc}") from None
        values[name] = value
    return values


def secret_id_for_variable(name: str) -> str:
    """Map an environment-variable name to its validated store identifier."""
    return _clean_secret_id(str(name).strip().lower())


def _entry(
    secret_id: str,
    *,
    scope: str,
    purpose: str,
    environment: str | None,
    materialize: dict[str, Any] | None,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """One catalog entry. `created_at` belongs to the first write, not this one."""
    if materialize and not environment:
        raise SecretStoreValidationError(
            "a secret that materializes needs the environment variable it materializes into"
        )
    entry: dict[str, Any] = {
        "id": secret_id,
        "scope": scope,
        "purpose": purpose,
        "created_at": existing["created_at"] if existing else _now(),
    }
    if environment:
        entry["environment"] = environment
    if materialize:
        entry["materialize"] = materialize
    return entry


def _catalog(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    catalog = {
        "version": CATALOG_VERSION,
        "secrets": [entries[name] for name in sorted(entries)],
    }
    errors = validate(catalog, "secret-catalog", f"secrets/{CATALOG_NAME}")
    if errors:
        raise SecretStoreValidationError(f"catalog entry is invalid: {errors[0]}")
    return catalog


def _read_value(instance_dir: Path, secret_id: str, key: bytes) -> bytes:
    path = value_path(instance_dir, secret_id)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SecretStoreStateError(
            f"secret {secret_id!r} is catalogued but its value file is missing"
        ) from None
    except (OSError, ValueError):
        raise SecretStoreStateError(f"could not read the value for {secret_id!r}") from None
    if not isinstance(envelope, dict) or envelope.get("id") != secret_id:
        raise SecretStoreStateError(f"value file does not belong to secret {secret_id!r}")
    return open_value(key, envelope)


def _stored_value(instance_dir: Path, secret_id: str, key: bytes) -> bytes | None:
    """The value already in the store, or None if it is not readable as one."""
    try:
        return _read_value(instance_dir, secret_id, key)
    except SecretStoreStateError:
        return None


def _env_value(entry: dict[str, Any], value: bytes) -> str:
    """The right-hand side of one env line, or a refusal.

    Values retain their serialized EnvironmentFile syntax so imported files round-trip
    byte for byte. Validate it before publishing; runtime consumers decode this syntax.
    """
    try:
        text = value.decode("utf-8")
    except UnicodeError:
        raise SecretStoreValidationError(
            f"secret {entry['id']!r} is not text and cannot go into an env file"
        ) from None
    if any(char in text for char in "\n\r\x00"):
        raise SecretStoreValidationError(
            f"secret {entry['id']!r} contains a newline and cannot go into an env file"
        )
    try:
        parse_env_value(text)
    except RuntimeEnvError as exc:
        raise SecretStoreValidationError(
            f"secret {entry['id']!r} cannot go into an env file: {exc}"
        ) from None
    return text


def _assert_distinct_secret_ids(variables: dict[str, str], *, source: str) -> None:
    """Refuse a file whose variables would share one secret id.

    Ids are lower case, so `FOO` and `foo` are two variables but one id: importing both would leave
    the second one's value under the first one's name. Refused before writing anything.
    """
    seen: dict[str, str] = {}
    for name in variables:
        secret_id = secret_id_for_variable(name)
        first = seen.get(secret_id)
        if first is not None:
            raise SecretStoreValidationError(
                f"{source} defines {first} and {name}, which differ only in case and would share "
                f"the secret id {secret_id!r}; rename one of them before importing"
            )
        seen[secret_id] = name


def _assert_one_secret_per_variable(path: Path, entries: list[dict[str, Any]]) -> None:
    """Two secrets claiming one variable is a store fault, not a write order."""
    seen: dict[str, str] = {}
    for entry in entries:
        name = entry["environment"]
        if name in seen:
            raise SecretStoreStateError(f"{seen[name]} and {entry['id']} both materialize {name} into {path}")
        seen[name] = entry["id"]


def _assert_one_secret_per_line(path: Path, entries: list[dict[str, Any]]) -> None:
    """Two secrets claiming one line means the recorded file layout is not a file."""
    seen: dict[int, str] = {}
    for entry in entries:
        order = entry["materialize"].get("order")
        if order is None:
            raise SecretStoreStateError(
                f"secret {entry['id']} materializes into {path} without a line number"
            )
        if order in seen:
            raise SecretStoreStateError(f"{seen[order]} and {entry['id']} both claim line {order} of {path}")
        seen[order] = entry["id"]


def _assert_writable_target(instance_dir: Path, path: Path) -> None:
    """Refuse a target the snapshot export would copy, or that is not a plain file."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        raise SecretStoreStateError(
            f"materialization target {path} is not a regular file; refusing to replace it"
        )
    try:
        relative = path.resolve().relative_to(instance_dir.resolve())
    except ValueError:
        return
    if is_exported(relative.as_posix()):
        raise SecretStoreError(
            f"materialization target {relative} is a live-root path the snapshot export copies; "
            "refusing to write plaintext where a checkpoint can pick it up"
        )


def _publish_env_file(path: Path, text: str) -> bool:
    """Replace the env file in one step, or leave it exactly as it was.

    systemd reads this file on every unit start, so there is no moment it may be missing, empty or
    half-written: the content is written to a neighbour, given its mode there, and only then renamed
    over the target. A crash before the rename leaves the old file untouched.
    """
    desired = text.encode("utf-8")
    try:
        if path.exists() and path.read_bytes() == desired:
            return False
    except OSError as exc:
        raise SecretStoreError(f"could not read the materialization target {path}: {exc}") from None

    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise SecretStoreError(f"could not write {path}: {exc}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    return True


def _scan_open_file(name: str, text: str) -> None:
    """The open half of the store leaves the host, so it passes the same gate.

    `checkpoint.py` blocks a checkpoint when `redact()` changes what it ships; the catalog is exported
    plaintext with the same reach, so a value pasted into a purpose field stops here.
    """
    if redact(text) != text:
        raise SecretStoreValidationError(f"secret detected in {name}")


def _assert_key_not_exported() -> None:
    """The key is excluded by not matching the export allowlist; refuse to keep it if it ever did."""
    if is_exported(KEY_RELATIVE):
        raise SecretStoreError(
            f"{KEY_RELATIVE} is matched by the snapshot export allowlist; refusing to keep a key "
            "that would leave the host"
        )


def _live_root(instance_dir: Path) -> Path:
    """The live root the store lives in: an existing directory, Git work tree or not."""
    root = Path(instance_dir).expanduser().resolve()
    if not root.is_dir():
        raise SecretStoreStateError(f"instance directory not found: {root}")
    return root


def store_revision(instance_dir: Path) -> str:
    """The store's content revision: `_fsutil.content_revision` over its exported files' bytes.

    The files are the catalog, the key parameters and every envelope, named by their live-root
    path; the installation key and the undo area are not part of it. The same store gives the same
    revision wherever it is computed, and any changed byte changes it.
    """
    root = secrets_dir(instance_dir).parent
    names = [
        f"{SECRETS_DIR_NAME}/{name}"
        for name in (CATALOG_NAME, KEY_PARAMS_NAME)
        if _is_plain_file(secrets_dir(instance_dir) / name)
    ]
    values = secrets_dir(instance_dir) / VALUES_DIRNAME
    if values.is_dir() and not values.is_symlink():
        names.extend(
            f"{SECRETS_DIR_NAME}/{VALUES_DIRNAME}/{path.name}"
            for path in sorted(values.iterdir())
            if _is_plain_file(path) and is_exported(f"{SECRETS_DIR_NAME}/{VALUES_DIRNAME}/{path.name}")
        )
    try:
        return files_revision(root, names)
    except RuntimeError as exc:
        raise SecretStoreError(f"could not read the secret store: {exc}") from None


def _is_plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


@contextmanager
def _locked_store(instance_dir: Path) -> Iterator[None]:
    """Hold the live-root writer lock, after restoring whatever a crashed store write left behind."""
    with state_repo.state_repo_lock(instance_dir):
        root = secrets_dir(instance_dir)
        if root.is_dir():
            try:
                recover_canon_undo(root)
            except (OSError, RuntimeError) as exc:
                raise SecretStoreError(
                    f"could not restore an interrupted secret store write: {exc}"
                ) from None
        yield


@contextmanager
def _store_write(instance_dir: Path) -> Iterator[CanonTransaction]:
    """One all-or-nothing write of `secrets/`, under :func:`_locked_store`.

    A failure anywhere inside restores every path the block touched and removes the directories it
    created, `secrets/` included, so the store is byte-identical to before.
    """
    root = secrets_dir(instance_dir)
    existed = os.path.lexists(root)
    try:
        with canon_transaction(root, root, label="secret store") as transaction:
            yield transaction
    except BaseException as exc:
        if not existed:
            with suppress(OSError):
                root.rmdir()
        if isinstance(exc, RuntimeError) and not isinstance(exc, SecretStoreError):
            raise SecretStoreError(f"could not write the secret store: {exc}") from None
        raise


def _write(transaction: CanonTransaction, path: Path, text: str) -> None:
    """Replace `path` with `text` inside `transaction`, unless it already holds exactly that."""
    try:
        if _is_plain_file(path) and path.read_bytes() == text.encode("utf-8"):
            return
    except OSError:
        pass
    try:
        transaction.write(path, text)
    except SecretStoreError:
        raise
    except RuntimeError as exc:
        raise SecretStoreError(f"could not write the secret store: {exc}") from None


def _clean_actor(actor: str) -> str:
    value = str(actor).strip()
    if not value:
        raise SecretStoreValidationError("actor is required")
    return value


def _clean_secret_id(secret_id: str) -> str:
    value = str(secret_id).strip()
    if not value or len(value) > 128:
        raise SecretStoreValidationError("secret id must be 1..128 characters")
    if value[0] not in _ID_ALLOWED - set("._-") or any(char not in _ID_ALLOWED for char in value):
        raise SecretStoreValidationError(
            "secret id must be lowercase letters, digits, dot, dash or underscore, "
            "starting with a letter or digit"
        )
    if ".." in value:
        raise SecretStoreValidationError("secret id must not contain '..'")
    return value


def _clean_scope(scope: str) -> str:
    value = str(scope).strip()
    if value == INSTALLATION_SCOPE:
        return value
    if value.startswith(PROJECT_SCOPE_PREFIX):
        project = value[len(PROJECT_SCOPE_PREFIX) :]
        if (
            project
            and project[0] in _ID_ALLOWED - set("._-")
            and all(char in _ID_ALLOWED for char in project)
        ):
            return value
    raise SecretStoreValidationError(f"scope must be '{INSTALLATION_SCOPE}' or '{PROJECT_SCOPE_PREFIX}<id>'")


def _clean_purpose(purpose: str) -> str:
    value = " ".join(str(purpose).split())
    if not value:
        raise SecretStoreValidationError("purpose is required")
    if len(value) > 500:
        raise SecretStoreValidationError("purpose must be at most 500 characters")
    return value


def _clean_environment(environment: str | None) -> str | None:
    if environment is None:
        return None
    value = str(environment).strip()
    if not value:
        return None
    if len(value) > 64 or not _ENV_NAME_RE.match(value):
        raise SecretStoreValidationError("environment must be an environment variable name")
    return value


def _clean_materialize(materialize: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize one materialization instruction. `order` may be filled in later."""
    if materialize is None:
        return None
    if not isinstance(materialize, dict):
        raise SecretStoreValidationError("materialize must be a mapping")
    target = str(materialize.get("target", "")).strip()
    if target not in MATERIALIZE_TARGETS:
        raise SecretStoreValidationError(
            f"materialization target must be one of {', '.join(MATERIALIZE_TARGETS)}"
        )
    cleaned: dict[str, Any] = {"target": target}
    if target == MATERIALIZE_RUNTIME_ENV:
        if materialize.get("path"):
            raise SecretStoreValidationError(
                f"the {MATERIALIZE_RUNTIME_ENV} target carries no path; it is resolved at write time"
            )
    else:
        path = str(materialize.get("path", "")).strip()
        if not path:
            raise SecretStoreValidationError(f"the {MATERIALIZE_FILE} target needs a path")
        cleaned["path"] = path
    order = materialize.get("order")
    if order is not None:
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise SecretStoreValidationError("materialization order must be a line number from 0")
        cleaned["order"] = order
    return cleaned


def _materialize_slot(materialize: dict[str, Any]) -> tuple[str, str]:
    """The file an instruction writes into, as far as the catalog can tell."""
    return (str(materialize.get("target", "")), str(materialize.get("path", "")))


def _shift_foreign_lines(
    entries: dict[str, dict[str, Any]],
    materialize: dict[str, Any],
    imported_lines: int,
    *,
    keep: set[str],
) -> None:
    """Move whatever else writes into this file below the imported block.

    An import owns the top of the file it came from, line for line, so no two secrets end up
    claiming the same line.
    """
    slot = _materialize_slot(materialize)
    foreign = [
        name
        for name, entry in entries.items()
        if name not in keep and entry.get("materialize") and _materialize_slot(entry["materialize"]) == slot
    ]
    foreign.sort(key=lambda name: (entries[name]["materialize"].get("order", 0), name))
    for offset, name in enumerate(foreign):
        entry = dict(entries[name])
        entry["materialize"] = {**entry["materialize"], "order": imported_lines + offset}
        entries[name] = entry


def _assign_order(
    entries: dict[str, dict[str, Any]],
    *,
    secret_id: str,
    materialize: dict[str, Any] | None,
    existing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Give a materializing secret its line number in the target file.

    A caller that names no order keeps the one it already had, or takes the next free line. Nothing
    an existing entry holds moves, so a file that came in through `import` keeps its order.
    """
    if materialize is None:
        return None
    if "order" in materialize:
        return materialize
    slot = _materialize_slot(materialize)
    previous = (existing or {}).get("materialize") or {}
    if "order" in previous and _materialize_slot(previous) == slot:
        return {**materialize, "order": previous["order"]}
    used = [
        entry["materialize"]["order"]
        for name, entry in entries.items()
        if name != secret_id
        and entry.get("materialize")
        and _materialize_slot(entry["materialize"]) == slot
        and "order" in entry["materialize"]
    ]
    return {**materialize, "order": max(used) + 1 if used else 0}


def _check_value(value: bytes) -> None:
    if not isinstance(value, (bytes, bytearray)):
        raise SecretStoreValidationError("secret value must be bytes")
    if not value:
        raise SecretStoreValidationError("secret value is empty")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: Any, what: str, *, length: int | None = None) -> bytes:
    if not isinstance(text, str):
        raise SecretStoreStateError(f"{what} is not valid base64")
    try:
        value = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise SecretStoreStateError(f"{what} is not valid base64") from None
    if length is not None and len(value) != length:
        raise SecretStoreStateError(f"{what} has the wrong length")
    return value
