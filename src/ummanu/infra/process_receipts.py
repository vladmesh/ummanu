"""Private process receipts: one wire codec, bounded reader and atomic publisher.

Services and upgrade retain input acquisition, probes and restart policy. Dependency receipts
share only the JSON I/O, with their own schema and evidence in upgrade.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu._fsutil import fsync_directory
from ummanu.host_apply import UnitProcessIdentity

RECEIPT_MAX_BYTES = 64 * 1024
REVISION = r"[0-9a-f]{40,64}"
SHA256 = r"[0-9a-f]{64}"


class ReceiptError(RuntimeError):
    """A receipt or one of the inputs it binds cannot be read or written."""


@dataclass(frozen=True)
class ProcessReceiptSpec:
    label: str
    has_unit: bool
    # Key order is also the order of stale-input diagnostics. Only the memory pack is nullable.
    fields: tuple[tuple[str, str, bool], ...]
    input_mismatch: str | None = None
    stale_prefix: str = ""


COMMON_FIELDS = (
    ("product_revision", REVISION, False),
    ("product_sha256", SHA256, False),
    ("dependency_sha256", SHA256, False),
)
MEMORY = ProcessReceiptSpec(
    "memory process receipt", True,
    (*COMMON_FIELDS, ("memory_model", r"[\x21-\x7e]{1,256}", False),
     ("memory_pack_sha256", SHA256, True)),
)
WEB = ProcessReceiptSpec(
    "web process receipt", True,
    (*COMMON_FIELDS, ("schemas_sha256", SHA256, False), ("web_units_sha256", SHA256, False),
     ("head_registry_sha256", SHA256, False)),
    input_mismatch="the web process receipt inputs do not match the materialized state",
)
PO = ProcessReceiptSpec(
    "PO process receipt", False, (*COMMON_FIELDS, ("schemas_sha256", SHA256, False)),
    stale_prefix="the PO process receipt is stale: ",
)


@dataclass(frozen=True)
class ProcessReceipt:
    payload: dict[str, Any]
    identity: UnitProcessIdentity
    inputs: dict[str, Any]


def load_receipt(path: Path, label: str) -> tuple[dict[str, Any] | None, str]:
    """Read at most 64 KiB + 1 from a no-follow regular descriptor, failing closed.

    The lstat preserves missing/unreadable diagnostics and rejects obvious nonfiles. The
    descriptor check handles replacement after lstat; O_NONBLOCK prevents a swapped FIFO from
    blocking open. Parent-chain no-follow is deliberately outside this contract.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, f"the {label} is missing"
    except OSError as exc:
        return None, f"the {label} cannot be read: {exc}"
    malformed = f"the {label} is malformed"
    if not stat.S_ISREG(info.st_mode) or info.st_size > RECEIPT_MAX_BYTES:
        return None, malformed
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        source = os.fdopen(descriptor, "rb")
        descriptor = None
        with source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > RECEIPT_MAX_BYTES:
                return None, malformed
            raw = source.read(RECEIPT_MAX_BYTES + 1)
        payload = json.loads(raw.decode("utf-8")) if len(raw) <= RECEIPT_MAX_BYTES else None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None, malformed
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, dict):
        return None, malformed
    return payload, ""


def process_identity(value: object) -> UnitProcessIdentity | None:
    if not isinstance(value, dict) or set(value) != {"pid", "start_ticks", "invocation_id"}:
        return None
    pid = value["pid"]
    ticks = value["start_ticks"]
    invocation = value["invocation_id"]
    if (type(pid) is not int or pid <= 0 or type(ticks) is not int or ticks <= 0
            or not isinstance(invocation, str) or not invocation):
        return None
    return UnitProcessIdentity(pid, ticks, invocation)


def valid_inputs(spec: ProcessReceiptSpec, value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {key for key, _, _ in spec.fields}:
        return None
    result = {}
    for key, pattern, nullable in spec.fields:
        item = value[key]
        if item is None and nullable:
            result[key] = None
        elif isinstance(item, str) and re.fullmatch(pattern, item):
            result[key] = item
        else:
            return None
    return result


def read_process_receipt(
    path: Path, spec: ProcessReceiptSpec, unit: str | None = None,
) -> tuple[ProcessReceipt | None, str]:
    payload, reason = load_receipt(path, spec.label)
    if payload is None:
        return None, reason
    malformed = f"the {spec.label} is malformed"
    keys = {"version", "process", "inputs"} | ({"unit"} if spec.has_unit else set())
    if set(payload) != keys:
        return None, malformed
    version = payload["version"]
    if type(version) is not int or version != 1 or (spec.has_unit and payload["unit"] != unit):
        return None, malformed
    identity = process_identity(payload["process"])
    inputs = valid_inputs(spec, payload["inputs"])
    if identity is None or inputs is None:
        return None, malformed
    return ProcessReceipt(payload, identity, inputs), ""


def short(value: str | None) -> str:
    if value is None:
        return "none"
    return value[:12] if re.fullmatch(REVISION, value) else value


def mismatch(
    spec: ProcessReceiptSpec, recorded_identity: UnitProcessIdentity, recorded: dict[str, Any],
    identity: UnitProcessIdentity, inputs: dict[str, Any],
) -> str:
    """Identity precedes input comparison; adapters retain their success summaries."""
    if recorded_identity != identity:
        return f"the {spec.label} belongs to a different process generation"
    if spec.input_mismatch is not None:
        return spec.input_mismatch if recorded != inputs else ""
    moved = [key for key, _, _ in spec.fields if recorded[key] != inputs[key]]
    if not moved:
        return ""
    return spec.stale_prefix + "; ".join(
        f"{key.replace('_', ' ')} {short(recorded[key])} -> {short(inputs[key])}" for key in moved
    )


def write_process_receipt(
    path: Path, spec: ProcessReceiptSpec, identity: UnitProcessIdentity, inputs: dict[str, Any],
    runtime_user: str | None, error: type[ReceiptError], unit: str | None = None,
) -> None:
    payload = {
        "version": 1,
        "process": {"pid": identity.pid, "start_ticks": identity.start_ticks,
                    "invocation_id": identity.invocation_id},
        "inputs": inputs,
    }
    if spec.has_unit:
        payload["unit"] = unit
    write_private_receipt(path, payload, runtime_user, error, spec.label)


def _receipt_owner(runtime_user: str | None, error: type[ReceiptError]) -> tuple[int, int] | None:
    if not runtime_user or os.geteuid() != 0:
        return None
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise error(f"runtime user {runtime_user!r} does not exist") from None
    return account.pw_uid, account.pw_gid


def write_private_receipt(
    path: Path, payload: dict[str, Any], runtime_user: str | None,
    error: type[ReceiptError], label: str,
) -> None:
    """Write one receipt as a private (0600, runtime-owned) atomic replacement."""
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner = _receipt_owner(runtime_user, error)
        parent_info = path.parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise OSError("receipt directory is not a real directory")
        if owner is not None:
            os.chown(path.parent, *owner, follow_symlinks=False)
            os.chmod(path.parent, 0o700, follow_symlinks=False)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        if owner is not None:
            os.fchown(descriptor, *owner)
        os.fchmod(descriptor, 0o600)
        output = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = None  # output owns it, including on serialization/fsync failure.
        with output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        fsync_directory(path.parent)
    except OSError as exc:
        raise error(f"could not write {label} {path}: {exc}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
