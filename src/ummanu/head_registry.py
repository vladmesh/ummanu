"""The installation head registry: materialized from a TOML canon, read by the tick.

Two generated files live side by side under ``<data>/heads/``, the installation's data directory,
never its live root. ``heads.yaml`` is the registry the running installation uses; ``source.yaml``
records which canonical file, product checkout and revision ``ummanu upgrade`` generated it from,
and fingerprints the snapshot, so a live tick accepts only a matching installed pair. It reads no
product checkout: only an upgrade (or a recover) moves the installation, and neither commits the
pair: it is regenerated from the canon, so it is not recovery canon (docs/RECOVERY.md).

Every reader and writer finds the pair through :func:`installed_pair` or :func:`generated_pair`;
no other module spells its path.

An installation may own ``heads/heads.toml`` in its instance directory, and that file is then the
canon; one that owns no such file falls back to the product's small default registry. A canon
that is *there* but unusable is a broken installation, not a portable one: it stops the upgrade
by name rather than quietly reverting the host to product heads the operator never chose.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ummanu import _proc
from ummanu._fsutil import write_text_atomic
from ummanu.runtime.heads import (
    HeadRegistryError,
    load_registry,
    validate_registry,
    validate_role_defaults,
)
from ummanu.runtime.paths import configured_product_root, instance_file

HEADS_RELATIVE = Path("src") / "ummanu" / "runtime" / "heads.toml"
INSTANCE_HEADS_RELATIVE = Path("heads") / "heads.toml"
# The pair's directory, below the data directory and never below the live root. It is also the
# local-pty runtime root, whose readers take only run directories, so the two files sit beside them
# unread.
PAIR_DIRECTORY = Path("heads")
SNAPSHOT_NAME = "heads.yaml"
SOURCE_NAME = "source.yaml"
SOURCE_HEADER = (
    "# The canonical registry, checkout and revision `ummanu upgrade` generated heads.yaml from.\n"
    "# Do not edit this pin by hand.\n"
)
UNKNOWN_REVISION = "unknown"
PRODUCT_ORIGIN = "product"
INSTANCE_ORIGIN = "instance"
SOURCE_REQUIRED_FIELDS = ("canonical", "canonical_owner", "product_root", "revision", "snapshot_sha256")


def snapshot_header(canonical: Path) -> str:
    """Name the file this snapshot came from, so a reader is never left guessing which canon won."""
    return f"# Generated from {canonical} by `ummanu upgrade`.\n# Do not edit this snapshot by hand.\n"


class HeadRegistryConfigError(RuntimeError):
    """The canonical registry or installation snapshot cannot be loaded."""


def _validated_registry(data: dict[str, Any], origin: Path) -> dict[str, Any]:
    """The three registry tables, checked the same way wherever they came from.

    Nothing malformed may leave this function as a raw AttributeError or TypeError: an upgrade step
    and `ummanu status` both report on what they find at a path, so a broken registry has to
    arrive as a message about that path.
    """
    tables: dict[str, Any] = {}
    for key in ("resources", "profiles", "role_defaults"):
        value = data.get(key)
        if not isinstance(value, dict):
            raise HeadRegistryConfigError(f"head registry {origin} has no [{key}] table")
        tables[key] = value
    try:
        validate_registry(tables["resources"], tables["profiles"])
        validate_role_defaults(tables["role_defaults"], tables["profiles"])
    except HeadRegistryError as exc:
        raise HeadRegistryConfigError(f"head registry {origin} is invalid: {exc}") from None
    return tables


def _instance_dir(instance_path: Path) -> Path:
    """The instance directory, whether the caller named it or its ``instance.yaml``.

    A path that cannot even be stat'd still has to yield a directory, because the caller's job is
    then to raise a bounded error naming a file under it.
    """
    try:
        return instance_file(instance_path).parent
    except OSError:
        return instance_path if instance_path.name != "instance.yaml" else instance_path.parent


def canonical_path(product_root: Path, instance_path: Path | None = None) -> tuple[Path, str]:
    """The registry that wins, and which side of the boundary owns it.

    An installation that ships ``heads/heads.toml`` has decided what its own heads are. "Has not"
    means the file is genuinely absent — anything else at that path is a canon the operator meant to
    have, so it fails here by name.

    Every probe goes through one ``lstat``, and every way it can fail is answered here: a
    ``Path.is_file()`` would raise a bare ``PermissionError`` out of an unreadable ``heads/``
    directory, past the ``HeadRegistryConfigError`` an upgrade step knows how to report.
    """
    if instance_path is None:
        return product_root / HEADS_RELATIVE, PRODUCT_ORIGIN
    owned = _instance_dir(instance_path) / INSTANCE_HEADS_RELATIVE
    try:
        mode = owned.lstat().st_mode
    except FileNotFoundError:
        return product_root / HEADS_RELATIVE, PRODUCT_ORIGIN
    except OSError as exc:
        raise HeadRegistryConfigError(f"cannot inspect instance head registry {owned}: {exc}") from None
    if stat.S_ISLNK(mode):
        try:
            mode = owned.stat().st_mode
        except FileNotFoundError:
            raise HeadRegistryConfigError(f"instance head registry {owned} is a dangling symlink") from None
        except OSError as exc:
            raise HeadRegistryConfigError(f"cannot inspect instance head registry {owned}: {exc}") from None
    if stat.S_ISREG(mode):
        return owned, INSTANCE_ORIGIN
    if stat.S_ISDIR(mode):
        raise HeadRegistryConfigError(f"instance head registry {owned} is a directory, not a file")
    raise HeadRegistryConfigError(f"instance head registry {owned} is not a regular file")


def canonical_heads(product_root: Path, instance_path: Path | None = None) -> dict[str, Any]:
    path, _ = canonical_path(product_root, instance_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        # Reuse the triggered-agents validator so both consumers reject the same malformed canon.
        load_registry(path)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, HeadRegistryError) as exc:
        raise HeadRegistryConfigError(f"cannot load canonical head registry {path}: {exc}") from None
    return _validated_registry(data, path)


def render_snapshot(heads: dict[str, Any], canonical: Path) -> str:
    return snapshot_header(canonical) + yaml.safe_dump(
        heads,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


@dataclass(frozen=True)
class RegistryPair:
    """Where one installation's generated pair is."""

    snapshot: Path
    source: Path


def _data_dir(instance_path: Path) -> Path:
    from ummanu.config import DataDirError, generated_state_dir

    try:
        return generated_state_dir(_instance_dir(instance_path))
    except DataDirError as exc:
        raise HeadRegistryConfigError(
            f"cannot locate the head registry of {_instance_dir(instance_path)}: {exc}"
        ) from None


def generated_pair(instance_path: Path, data_dir: Path | None = None) -> RegistryPair:
    """Where `ummanu upgrade` and `recover` write the pair: `<data>/heads/`, never the live root.

    ``data_dir`` is the caller's already resolved data directory; without it, the instance's
    configured ``data_dir`` is read.
    """
    root = Path(data_dir).expanduser() if data_dir is not None else _data_dir(instance_path)
    heads = root / PAIR_DIRECTORY
    return RegistryPair(heads / SNAPSHOT_NAME, heads / SOURCE_NAME)


def installed_pair(instance_path: Path, data_dir: Path | None = None) -> RegistryPair:
    """The pair a reader reads: `<data>/heads/`, the only place it lives.

    A live root's own `heads/heads.yaml`, which an upgrade before the data-directory move committed
    there, is not read: an installation whose data directory has no pair has not been upgraded, and
    the reader's error names `ummanu upgrade` (:func:`load_snapshot`).
    """
    return generated_pair(instance_path, data_dir)


def missing_snapshot(instance_path: Path, path: Path) -> HeadRegistryConfigError:
    """The one error for an installation whose `<data>/heads/` has no snapshot: it names the fix."""
    return HeadRegistryConfigError(
        f"installation head snapshot {path} is missing; run `ummanu upgrade --instance "
        f"{_instance_dir(instance_path)}` to generate the pair"
    )


def load_snapshot(instance_path: Path, pair: RegistryPair | None = None) -> dict[str, Any]:
    path = (pair or installed_pair(instance_path)).snapshot
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise missing_snapshot(instance_path, path) from None
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HeadRegistryConfigError(f"cannot load installation head snapshot {path}: {exc}") from None
    if not isinstance(loaded, dict):
        raise HeadRegistryConfigError(f"installation head snapshot {path} has an unsupported shape")
    return loaded


def product_revision(product_root: Path) -> str:
    """The commit the checkout is on, or ``unknown`` when it cannot be read."""
    try:
        result = _proc.run(
            ["git", "-c", f"safe.directory={product_root}", "-C", str(product_root), "rev-parse", "HEAD"],
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN_REVISION
    if result.returncode != 0:
        return UNKNOWN_REVISION
    return (result.stdout or "").strip() or UNKNOWN_REVISION


def read_source(instance_path: Path, pair: RegistryPair | None = None) -> dict[str, Any] | None:
    """The recorded canon source, or None when this installation has never been upgraded."""
    path = (pair or installed_pair(instance_path)).source
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HeadRegistryConfigError(f"cannot load head registry source pin {path}: {exc}") from None
    if not isinstance(loaded, dict):
        raise HeadRegistryConfigError(f"head registry source pin {path} has an unsupported shape")
    return loaded


def _snapshot_sha256(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def _validated_source_pair(instance_path: Path, pair: RegistryPair, snapshot: str) -> dict[str, Any]:
    """Validate the source pin that makes an installed snapshot trustworthy.

    The pin records the exact source checkout and fingerprint of the generated file, so a reader
    cannot combine a new snapshot with an old pin or accept one copied from an unrelated checkout.
    """
    path = pair.source
    source = read_source(instance_path, pair)
    if source is None:
        raise HeadRegistryConfigError(
            f"installation head registry source pin {path} is missing; run `ummanu upgrade --instance "
            f"{_instance_dir(instance_path)}` to regenerate the pair"
        )
    missing = [
        key for key in SOURCE_REQUIRED_FIELDS if not isinstance(source.get(key), str) or not source[key]
    ]
    if missing:
        raise HeadRegistryConfigError(
            f"head registry source pin {path} is incomplete ({', '.join(missing)}); run `ummanu upgrade --instance "
            f"{_instance_dir(instance_path)}` to regenerate the pair"
        )
    if source["canonical_owner"] not in (PRODUCT_ORIGIN, INSTANCE_ORIGIN):
        raise HeadRegistryConfigError(
            f"head registry source pin {path} has an invalid canonical_owner; run `ummanu upgrade --instance "
            f"{_instance_dir(instance_path)}` to regenerate the pair"
        )
    try:
        expected_header = snapshot_header(Path(source["canonical"]))
    except (TypeError, ValueError):
        raise HeadRegistryConfigError(
            f"head registry source pin {path} has an invalid canonical path; run `ummanu upgrade --instance "
            f"{_instance_dir(instance_path)}` to regenerate the pair"
        ) from None
    if not snapshot.startswith(expected_header) or source["snapshot_sha256"] != _snapshot_sha256(snapshot):
        raise HeadRegistryConfigError(
            f"head registry pair {pair.snapshot} and {path} is stale or mismatched; "
            f"run `ummanu upgrade --instance {_instance_dir(instance_path)}` to regenerate it"
        )
    return source


def pinned_product_root(instance_path: Path) -> Path:
    """Which checkout this installation runs: the recorded pin, else the configured one.

    An installation materialized from an alternate checkout keeps that path in its pin, and it is the
    only durable record of it. An unreadable or absent pin falls back to what this process is
    configured with rather than failing a read-only view.
    """
    try:
        source = read_source(instance_path)
    except HeadRegistryConfigError:
        source = None
    recorded = source.get("product_root") if isinstance(source, dict) else None
    if isinstance(recorded, str) and recorded.strip():
        return Path(recorded).expanduser()
    return configured_product_root()


def record_source(
    instance_path: Path, product_root: Path, *, dry_run: bool = False, data_dir: Path | None = None
) -> bool:
    """Write which canon, checkout and revision the snapshot came from. Returns whether it moved."""
    target = generated_pair(instance_path, data_dir).source
    canonical, origin = canonical_path(product_root, instance_path)
    canonical = canonical.expanduser().resolve(strict=False)
    snapshot = render_snapshot(canonical_heads(product_root, instance_path), canonical)
    desired = SOURCE_HEADER + yaml.safe_dump(
        {
            "canonical": str(canonical),
            "canonical_owner": origin,
            "product_root": str(Path(product_root).expanduser().resolve(strict=False)),
            "revision": product_revision(product_root),
            "snapshot_sha256": _snapshot_sha256(snapshot),
        },
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    except (OSError, UnicodeError) as exc:
        raise HeadRegistryConfigError(f"cannot read head registry source pin {target}: {exc}") from None
    if current == desired:
        return False
    if not dry_run:
        try:
            write_text_atomic(target, desired)
        except RuntimeError as exc:
            raise HeadRegistryConfigError(str(exc)) from None
    return True


def installed_heads(instance_path: Path, pair: RegistryPair | None = None) -> dict[str, Any]:
    """The registry this installation runs off, validated as a matching pair.

    This is what a live tick reads. It deliberately does not compare against any checkout's
    ``heads.toml``: the installation moves when `ummanu upgrade` moves it and at no other time. A
    snapshot that is itself broken still stops the caller, by name.
    """
    pair = pair or installed_pair(instance_path)
    path = pair.snapshot
    try:
        snapshot = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise missing_snapshot(instance_path, path) from None
    except (OSError, UnicodeError) as exc:
        raise HeadRegistryConfigError(f"cannot load installation head snapshot {path}: {exc}") from None
    loaded = load_snapshot(instance_path, pair)
    registry = _validated_registry(loaded, path)
    _validated_source_pair(instance_path, pair, snapshot)
    return registry


def materialize_snapshot(
    instance_path: Path, product_root: Path, *, dry_run: bool = False, data_dir: Path | None = None
) -> bool:
    """Write the canonical snapshot into `<data>/heads/`. Returns whether the target differs."""
    target = generated_pair(instance_path, data_dir).snapshot
    canonical, _ = canonical_path(product_root, instance_path)
    canonical = canonical.expanduser().resolve(strict=False)
    desired = render_snapshot(canonical_heads(product_root, instance_path), canonical)
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    except (OSError, UnicodeError) as exc:
        raise HeadRegistryConfigError(f"cannot read installation head snapshot {target}: {exc}") from None
    if current == desired:
        return False
    if not dry_run:
        try:
            write_text_atomic(target, desired)
        except RuntimeError as exc:
            raise HeadRegistryConfigError(str(exc)) from None
    return True


def assert_snapshot_current(
    instance_path: Path, product_root: Path, data_dir: Path | None = None
) -> dict[str, Any]:
    canonical = canonical_heads(product_root, instance_path)
    pair = generated_pair(instance_path, data_dir)
    snapshot = load_snapshot(instance_path, pair)
    if snapshot != canonical:
        target = pair.snapshot
        raise HeadRegistryConfigError(
            f"installation head snapshot {target} is stale; run `ummanu upgrade --instance "
            f"{instance_path}` to regenerate it"
        )
    return canonical
