"""An exporter snapshot read back out of its bare repository: the manifest check and the live root.

Contract: docs/RECOVERY.md, "Fresh install and recovery" and "Snapshot repository". Recovery reads
the tip's tree with Git plumbing against the bare repository only. Every blob is written into a
private extraction directory and hashed on the way, and the result is checked against
`snapshot-manifest.json` before anything outside that directory is written. The board and runs are
read from the extraction, never from the live root.

The live root is the inverse of the exporter's allowlist copy: exactly the tree's paths
`is_exported` matches, with their bytes and executable bit. No second list of paths exists here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ummanu import state_repo
from ummanu.checkpoint import SNAPSHOT_MANIFEST, SNAPSHOT_MANIFEST_FORMAT, SNAPSHOT_MANIFEST_VERSION
from ummanu.infra.export_allowlist import is_exported

_REGULAR_MODE = "100644"
_EXECUTABLE_MODE = "100755"
_DIGEST = re.compile(r"[0-9a-f]{64}")
# Blobs read per `cat-file --batch`, so a large tree is never held in memory at once.
_BATCH = 256
# How many paths a refusal names before it counts the rest.
_NAMED = 3


class SnapshotTreeError(RuntimeError):
    """The snapshot cannot be recovered from as it is; the message names why."""


@dataclass(frozen=True)
class SnapshotTree:
    """The tip's tree, extracted below `root` and checked against its manifest."""

    tip: str
    root: Path
    # path -> Git file mode of every file of the tree, the manifest included.
    modes: dict[str, str]
    manifest: dict[str, Any]

    @property
    def live_paths(self) -> list[str]:
        """The paths the live root holds: the ones the exporter's allowlist copy would take."""
        return sorted(path for path in self.modes if is_exported(path))


def manifest_payload(repo: Path, tip: str) -> bytes | None:
    """The bytes of `snapshot-manifest.json` at the root of `tip`, or None when there is none."""
    listing = _git(repo, ["ls-tree", "-z", "--full-tree", tip, "--", SNAPSHOT_MANIFEST], "snapshot manifest")
    entries = [entry for entry in listing.split("\0") if entry]
    if not entries:
        return None
    mode, kind, oid = (entries[0].partition("\t")[0].split() + ["", "", ""])[:3]
    if kind != "blob" or mode not in (_REGULAR_MODE, _EXECUTABLE_MODE):
        raise SnapshotTreeError(f"snapshot manifest is malformed: {SNAPSHOT_MANIFEST} is not a file")
    return _blobs(repo, [oid])[oid]


def parse_manifest(payload: bytes) -> dict[str, Any]:
    """The manifest, refused by name when it is not one this product reads."""
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise SnapshotTreeError("snapshot manifest is malformed: not JSON") from None
    if not isinstance(manifest, dict):
        raise SnapshotTreeError("snapshot manifest is malformed: not a JSON object")
    if manifest.get("format") != SNAPSHOT_MANIFEST_FORMAT:
        raise SnapshotTreeError(
            f"snapshot manifest is malformed: format {manifest.get('format')!r} is not {SNAPSHOT_MANIFEST_FORMAT!r}"
        )
    version = manifest.get("version")
    if type(version) is not int:
        raise SnapshotTreeError(f"snapshot manifest is malformed: version {version!r} is not a number")
    if version != SNAPSHOT_MANIFEST_VERSION:
        raise SnapshotTreeError(
            f"snapshot manifest version {version} is unknown to this product, which reads version "
            f"{SNAPSHOT_MANIFEST_VERSION}"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(path, str) and path and isinstance(digest, str) and _DIGEST.fullmatch(digest)
        for path, digest in files.items()
    ):
        raise SnapshotTreeError("snapshot manifest is malformed: files is not a map of path to sha256")
    for field in ("board_schema_head", "product_revision"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise SnapshotTreeError(f"snapshot manifest is malformed: {field} is missing")
    return manifest


def extract(repo: Path, tip: str, root: Path, manifest: dict[str, Any]) -> SnapshotTree:
    """Write every file of `tip` below `root` (which must not exist) and check it against `manifest`."""
    listing = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", tip], "snapshot tree")
    entries: list[tuple[str, str, str]] = []
    for record in filter(None, listing.split("\0")):
        meta, _, path = record.partition("\t")
        mode, kind, oid = (meta.split() + ["", "", ""])[:3]
        if kind != "blob" or mode not in (_REGULAR_MODE, _EXECUTABLE_MODE):
            raise SnapshotTreeError(f"snapshot tree holds a non-file entry: {path}")
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or any(part in {"", ".", "..", ".git"} for part in parts):
            raise SnapshotTreeError(f"snapshot tree holds an unsafe path: {path!r}")
        entries.append((path, mode, oid))
    root.mkdir(mode=0o700)
    digests: dict[str, str] = {}
    for start in range(0, len(entries), _BATCH):
        chunk = entries[start : start + _BATCH]
        blobs = _blobs(repo, sorted({oid for _, _, oid in chunk}))
        for path, mode, oid in chunk:
            payload = blobs[oid]
            _write_file(root / path, payload, executable=mode == _EXECUTABLE_MODE)
            digests[path] = hashlib.sha256(payload).hexdigest()
    _verify(manifest, digests)
    return SnapshotTree(
        tip=tip, root=root, modes={path: mode for path, mode, _ in entries}, manifest=manifest
    )


def require_known_schema(manifest: dict[str, Any], lineage: tuple[str, ...]) -> None:
    """Refuse a board schema head this product's migration lineage does not reach."""
    head = manifest["board_schema_head"]
    if head not in lineage:
        running = lineage[-1] if lineage else "none"
        raise SnapshotTreeError(
            f"snapshot board schema head {head} is newer than this product's schema head {running} "
            "(or unknown to it); recover with a product that ships it"
        )


def live_root_state(live_root: Path, tree: SnapshotTree) -> str:
    """`absent`, `empty` or `same` (a materialisation of this tip); a divergent live root is refused.

    A file the allowlist does not match (`secrets/installation.key`, `runtime.env`, a bootstrap stamp)
    is the host's own and never a divergence; every exported path must be the tree's, byte for byte,
    and no exported path may be extra.
    """
    if not live_root.exists() and not live_root.is_symlink():
        return "absent"
    if live_root.is_symlink() or not live_root.is_dir():
        raise SnapshotTreeError(f"live root {live_root} is not a directory; no files were overwritten")
    if not any(live_root.iterdir()):
        return "empty"
    differences = _differences(live_root, tree)
    if differences:
        named = ", ".join(differences[:_NAMED])
        more = f" and {len(differences) - _NAMED} more" if len(differences) > _NAMED else ""
        raise SnapshotTreeError(
            f"live root {live_root} is not empty and is not snapshot {tree.tip[:12]} ({named}{more}); "
            "preserve and inspect it, or choose a fresh --instance-dir, no files were overwritten"
        )
    return "same"


def stage_live_root(tree: SnapshotTree, staging: Path) -> None:
    """Lay the live root out in `staging` (which must not exist): the tree's exported files only."""
    staging.mkdir()
    for path in tree.live_paths:
        _write_file(
            staging / path, (tree.root / path).read_bytes(), executable=tree.modes[path] == _EXECUTABLE_MODE
        )


def _differences(live_root: Path, tree: SnapshotTree) -> list[str]:
    expected = set(tree.live_paths)
    found: list[str] = []
    if (live_root / ".git").exists() or (live_root / ".git").is_symlink():
        found.append(".git (a Git work tree)")
    for path in sorted(expected):
        info = _regular_beneath(live_root, path)
        if info is None:
            found.append(f"{path} missing")
        elif bool(info.st_mode & stat.S_IXUSR) != (tree.modes[path] == _EXECUTABLE_MODE):
            found.append(f"{path} mode")
        elif (live_root / path).read_bytes() != (tree.root / path).read_bytes():
            found.append(f"{path} differs")
    for directory, directories, files in os.walk(live_root):
        for name in (*directories, *files):
            candidate = Path(directory, name)
            relative = candidate.relative_to(live_root).as_posix()
            if relative == ".git" or relative.startswith(".git/"):
                continue
            exported_entry = name in files or candidate.is_symlink()
            if exported_entry and is_exported(relative) and relative not in expected:
                found.append(f"{relative} extra")
    return found


def _regular_beneath(root: Path, relative: str) -> os.stat_result | None:
    """The status of `root/relative` when every parent is a real directory and it is a regular file."""
    current = root
    parts = relative.split("/")
    try:
        for part in parts[:-1]:
            current = current / part
            if not stat.S_ISDIR(current.lstat().st_mode):
                return None
        info = (current / parts[-1]).lstat()
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _verify(manifest: dict[str, Any], digests: dict[str, str]) -> None:
    listed: dict[str, str] = manifest["files"]
    actual = {path: digest for path, digest in digests.items() if path != SNAPSHOT_MANIFEST}
    problems: list[str] = []
    for label, paths in (
        ("in the tree but not the manifest", sorted(set(actual) - set(listed))),
        ("in the manifest but not the tree", sorted(set(listed) - set(actual))),
        (
            "with a digest the manifest does not hold",
            sorted(p for p in set(actual) & set(listed) if actual[p] != listed[p]),
        ),
    ):
        if paths:
            more = f" and {len(paths) - _NAMED} more" if len(paths) > _NAMED else ""
            problems.append(f"{len(paths)} file(s) {label}: {', '.join(paths[:_NAMED])}{more}")
    if problems:
        raise SnapshotTreeError("snapshot manifest does not match the tree: " + "; ".join(problems))


def _write_file(path: Path, payload: bytes, *, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if executable else 0o644
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)


def _git(repo: Path, args: list[str], label: str) -> str:
    # `--git-dir` names the bare repository outright, so Git never discovers another one.
    return state_repo.git(repo, ["--git-dir", str(repo), *args], label=label, error=SnapshotTreeError)


def _blobs(repo: Path, oids: list[str]) -> dict[str, bytes]:
    return state_repo.read_blobs(repo, oids, git_args=["--git-dir", str(repo)], error=SnapshotTreeError)
