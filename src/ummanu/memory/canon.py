"""The memory canon as files: content revision, fact digests and the undo-guarded write.

Contract: docs/RECOVERY.md, "Writers". The canon is the regular `*.md` files under
`state/memory/facts` of the live root, plus the pack ledgers under `state/memory/packs`. No Git
call is part of this path: a write is made atomic by an undo area, and what used to be a commit id
is the content revision of the fact set.

A canon write (`canon_transaction`) records the prior state of every path before it replaces or
removes that path, in `<data>/memory/.undo`. On a failure the transaction restores exactly that
set; after a crash the next writer, under the same locks, restores it before it proceeds. The undo
area is never under `state/memory`, so neither the snapshot allowlist nor the legacy tick commit
can see it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ummanu._fsutil import (
    REVISION_PREFIX as REVISION_PREFIX,
    content_revision as content_revision,
    regular_files_under,
    write_bytes_atomic,
    write_text_atomic,
)
from ummanu.memory import access as memory_access
from ummanu.memory_errors import MemoryValidationError

UNDO_DIR = ".undo"
UNDO_JOURNAL = "journal.json"
UNDO_VERSION = 1
FRONTMATTER_DELIMITER = re.compile(r"^---(?:\r?\n|\Z)", re.MULTILINE)


# ── The fact set and its revision ─────────────────────────────────────────────


def fact_files(facts_dir: Path) -> list[tuple[str, Path]]:
    """Every canon fact as `(id, path)`, sorted by id; the id is the path under `facts` without `.md`.

    Symlinks and anything under a `.git` are not facts, exactly as the export reads them.
    """
    if not facts_dir.is_dir():
        return []
    found = []
    for path, _status in regular_files_under(facts_dir, context="memory canon"):
        if path.suffix != ".md":
            continue
        found.append((path.relative_to(facts_dir).as_posix().removesuffix(".md"), path))
    return sorted(found)


def fact_digests(facts_dir: Path) -> dict[str, str]:
    """`id -> sha256` of every canon fact's bytes."""
    digests = {}
    for fact_id, path in fact_files(facts_dir):
        try:
            digests[fact_id] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(f"could not read memory fact {fact_id}: {exc}") from None
    return digests


def text_digest(text: str) -> str:
    """The digest a fact whose bytes are `text` in UTF-8 has in `fact_digests`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canon_revision(facts_dir: Path) -> str:
    """The content revision of the canon under `facts_dir` as it is on disk now."""
    return content_revision(fact_digests(facts_dir))


# ── One fact as the index sees it ─────────────────────────────────────────────


def scope_for_relative(path: Path) -> str:
    top = path.parts[0]
    if top == "product-ummanu":
        return "product:ummanu"
    if top == memory_access.PO_REVIEW_SCOPE_DIR:
        return memory_access.PO_REVIEW_SCOPE
    return "global" if top == "global" else f"project:{top}"


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a fact on whole ``---`` lines, preserving the body's line endings.

    Delimiters use LF or CRLF; the closing delimiter may also end at EOF. A fact without an
    opening delimiter is plain Markdown. Once a block is opened it must close and contain a
    YAML mapping (an empty block is allowed), otherwise writers and indexers refuse it.
    """
    opening = FRONTMATTER_DELIMITER.match(raw)
    if opening is None:
        return {}, raw
    closing = FRONTMATTER_DELIMITER.search(raw, opening.end())
    if closing is None:
        raise MemoryValidationError("fact frontmatter is not closed")
    try:
        loaded = yaml.safe_load(raw[opening.end() : closing.start()])
    except (yaml.YAMLError, ValueError):
        raise MemoryValidationError("fact frontmatter is invalid YAML") from None
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise MemoryValidationError("fact frontmatter must be a mapping")
    return {str(key): value for key, value in loaded.items()}, raw[closing.end() :]


def parse_fact_text(raw: str, path: str | Path, fact_id: str | None = None) -> dict:
    rel = Path(path)
    meta, body = parse_frontmatter(raw)
    tags = meta.get("tags")
    if isinstance(tags, str):
        tag_text = tags
    else:
        tag_text = ",".join(tags) if tags else None
    return {
        "id": fact_id or str(rel.with_suffix("")),
        "path": str(rel),
        "slug": rel.stem,
        "scope": scope_for_relative(rel),
        "text": body.strip(),
        "tags": tag_text,
        "source": meta.get("source"),
        "created_at": str(meta["created"]) if meta.get("created") else None,
        "meta": meta,
    }


def fact_content_hash(fact: dict) -> str:
    """Hash all indexed fields so metadata-only changes are not missed."""
    payload = {key: fact.get(key) for key in ("text", "scope", "tags", "source", "created_at")}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── The undo-guarded write ────────────────────────────────────────────────────


class CanonTransaction:
    """Replace and remove files under one canon root, keeping each path's prior state first."""

    def __init__(self, memory_dir: Path, root: Path, *, label: str = "memory canon") -> None:
        self.undo = memory_dir / UNDO_DIR
        self.label = label
        self.root = Path(root).expanduser().resolve()
        self._entries: list[dict[str, Any]] = []
        self._guarded: set[str] = set()

    def write(self, path: Path, text: str) -> None:
        self._guard(path)
        write_text_atomic(path, text)

    def remove(self, path: Path) -> None:
        self._guard(path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(f"could not remove {path}: {exc}") from None

    def rollback(self) -> None:
        """Restore every guarded path; keep the undo area for the next writer if that fails."""
        if not self._entries:
            _discard(self.undo)
            return
        try:
            _restore(self.root, self.undo, self._entries)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"{self.label} rollback failed: {exc}; the undo at {self.undo} is kept for the next writer"
            ) from None
        _discard(self.undo)

    def commit(self) -> None:
        """Drop the undo area: from here on the write stands."""
        try:
            _discard(self.undo, strict=True)
        except OSError as exc:
            self.rollback()
            raise RuntimeError(f"could not finish the {self.label} write: {exc}") from None

    def guard(self, path: Path) -> None:
        """Keep the prior state of `path` before a caller changes it by its own means."""
        self._guard(path)

    def _guard(self, path: Path) -> None:
        relative = self._relative(path)
        if relative in self._guarded:
            return
        created = []
        parent = PurePosixPath(relative).parent
        while str(parent) != "." and not (self.root / parent).exists():
            created.append(parent.as_posix())
            parent = parent.parent
        entry: dict[str, Any] = {"path": relative, "created_dirs": created}
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise RuntimeError(f"could not inspect {self.label} path {relative}: {exc}") from None
        if not self._entries:
            try:
                self.undo.mkdir(parents=True)
            except OSError as exc:
                raise RuntimeError(
                    f"could not create the {self.label} undo area {self.undo}: {exc}"
                ) from None
        if info is None:
            entry["state"] = "absent"
        elif stat.S_ISLNK(info.st_mode):
            entry["state"] = "link"
            entry["target"] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            saved = f"{len(self._entries):06d}"
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise RuntimeError(f"could not keep {self.label} path {relative}: {exc}") from None
            write_bytes_atomic(self.undo / saved, payload)
            entry.update(
                state="file", saved=saved, mode=stat.S_IMODE(info.st_mode), owner=[info.st_uid, info.st_gid]
            )
        else:
            raise RuntimeError(f"{self.label} path is not a regular file: {relative}")
        self._entries.append(entry)
        # The journal names a path only after its prior state is kept, and the path changes only
        # after the journal names it: a crash anywhere in between restores bytes that are current.
        _write_journal(self.undo, self.root, self._entries)
        self._guarded.add(relative)

    def _relative(self, path: Path) -> str:
        try:
            relative = Path(path).relative_to(self.root)
        except ValueError:
            raise RuntimeError(f"{self.label} write outside its root: {path}") from None
        if not relative.parts or ".." in relative.parts:
            raise RuntimeError(f"{self.label} write outside its root: {path}")
        return relative.as_posix()


@contextmanager
def canon_transaction(
    memory_dir: Path, root: Path, *, label: str = "memory canon"
) -> Iterator[CanonTransaction]:
    """One all-or-nothing canon write under `root` (`state/memory` of the live root).

    The caller holds the memory lock and the live-root writer lock. An undo a crashed writer left
    is restored first; a failure inside the block restores every path the block touched. The secret
    store uses the same transaction over `secrets/`, with its undo area in `secrets/.undo`.
    """
    recover_canon_undo(memory_dir)
    transaction = CanonTransaction(memory_dir, root, label=label)
    try:
        yield transaction
    except BaseException:
        transaction.rollback()
        raise
    transaction.commit()


def pending_undo(memory_dir: Path) -> tuple[str, ...] | None:
    """The canon paths an unfinished write left to restore, or None when there is no undo area."""
    undo = memory_dir / UNDO_DIR
    if not undo.exists():
        return None
    try:
        _root, entries = _read_journal(undo)
    except FileNotFoundError:
        return ()
    return tuple(entry["path"] for entry in entries)


def recover_canon_undo(memory_dir: Path) -> tuple[str, ...]:
    """Restore what an unfinished write left behind; returns the restored paths.

    Called under the same locks as a write. An undo area without a journal recorded no path yet, so
    nothing was changed and it is only removed.
    """
    for leftover in memory_dir.glob(f"{UNDO_DIR}-*.tmp"):
        shutil.rmtree(leftover, ignore_errors=True)
    undo = memory_dir / UNDO_DIR
    if not undo.exists():
        return ()
    try:
        root, entries = _read_journal(undo)
    except FileNotFoundError:
        _discard(undo)
        return ()
    _restore(root, undo, entries)
    _discard(undo)
    return tuple(entry["path"] for entry in entries)


def _write_journal(undo: Path, root: Path, entries: list[dict[str, Any]]) -> None:
    payload = {"version": UNDO_VERSION, "root": str(root), "entries": entries}
    write_bytes_atomic(
        undo / UNDO_JOURNAL, (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    )


def _read_journal(undo: Path) -> tuple[Path, list[dict[str, Any]]]:
    journal = undo / UNDO_JOURNAL
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"memory undo journal is unreadable: {journal}: {exc}") from None
    if not isinstance(payload, dict) or payload.get("version") != UNDO_VERSION:
        raise RuntimeError(f"memory undo journal is invalid: {journal}")
    root = payload.get("root")
    entries = payload.get("entries")
    if not isinstance(root, str) or not Path(root).is_absolute() or not isinstance(entries, list):
        raise RuntimeError(f"memory undo journal is invalid: {journal}")
    for entry in entries:
        relative = entry.get("path") if isinstance(entry, dict) else None
        if (
            not isinstance(relative, str)
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or entry.get("state") not in {"absent", "file", "link"}
            or not isinstance(entry.get("created_dirs", []), list)
        ):
            raise RuntimeError(f"memory undo journal is invalid: {journal}")
    return Path(root), entries


def _restore(root: Path, undo: Path, entries: list[dict[str, Any]]) -> None:
    for entry in reversed(entries):
        path = root / entry["path"]
        if path.is_dir() and not path.is_symlink():
            raise RuntimeError(f"memory canon path became a directory: {entry['path']}")
        if entry["state"] == "file":
            payload = (undo / str(entry["saved"])).read_bytes()
            write_bytes_atomic(path, payload, mode=int(entry.get("mode", 0o600)))
            owner = entry.get("owner")
            # A root writer (the pack under `upgrade`) gives a restored file back to its owner.
            if os.geteuid() == 0 and isinstance(owner, list) and len(owner) == 2:
                os.chown(path, int(owner[0]), int(owner[1]))
            continue
        if path.is_symlink() or path.exists():
            path.unlink()
        if entry["state"] == "link":
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(entry["target"]), path)
    created = {directory for entry in entries for directory in entry.get("created_dirs", [])}
    for directory in sorted(created, key=lambda item: item.count("/"), reverse=True):
        try:
            (root / directory).rmdir()
        except OSError:
            pass


def _discard(undo: Path, *, strict: bool = False) -> None:
    """Retire the undo area with one rename, so a half-removed one is never read as a journal."""
    if not undo.exists():
        return
    retired = undo.with_name(f"{UNDO_DIR}-{uuid.uuid4().hex}.tmp")
    try:
        os.replace(undo, retired)
    except OSError:
        if strict:
            raise
        shutil.rmtree(undo, ignore_errors=True)
        return
    shutil.rmtree(retired, ignore_errors=True)
