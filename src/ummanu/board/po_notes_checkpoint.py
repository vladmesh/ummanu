"""The PO workspace notes file in the board checkpoint: exact bytes, explicit coverage, no overwrite.

`<data>/po/NOTES.md` belongs to the PO. A capture is one read of that regular file, or a confirmed
absence; a checkpoint without the `po_notes` declaration predates the capability. Restore creates
the file from the selected capture and never replaces or removes what a target already holds.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu._fsutil import write_json
from ummanu.board.checkpoint_layout import open_checkpoint_board, read_export_summary, unique_json_fields
from ummanu.po.workspace import NOTES_FILE, WORKSPACE_NAME

NOTES_CHECKPOINT_FILE = "po_notes.json"
COVERAGE_FIELD = "po_notes"
LEGACY_NOTES_COVERAGE = "checkpoint has no PO notes coverage; the PO workspace notes file is unavailable"
ABSENT_NOTES_COVERAGE = (
    "checkpoint captured no PO notes file; nothing was restored and a target notes file is not checkpoint content"
)
RESTORED_NOTES_COVERAGE = "PO notes restored at byte parity into the PO workspace before admission"
# A crash between staging and linking leaves this name; only this module writes it.
_STAGED_NAME = f".{NOTES_FILE}.restore.tmp"
_SHOWN = f"{WORKSPACE_NAME}/{NOTES_FILE}"


class PoNotesError(ValueError):
    """PO notes cannot be captured or restored without changing their bytes or the PO's file."""


@dataclass(frozen=True)
class NotesCapture:
    """A declared capture; `content` is None when the source file was confirmed absent."""

    content: bytes | None


def notes_coverage(content: bytes | None) -> dict[str, Any]:
    """Bind a present file, including an empty one, to its exact size and digest."""
    if content is None:
        return {"version": 1, "state": "absent"}
    return {"version": 1, "state": "present", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _open_beneath(directory: int, name: str, *, as_directory: bool) -> int | None:
    """Open one entry beneath a descriptor without following it; None when it does not exist."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_DIRECTORY if as_directory else os.O_NONBLOCK)
    try:
        opened = os.open(name, flags, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PoNotesError(f"PO notes refuse {_SHOWN}: a symlink or unsupported file type") from None
        raise PoNotesError(f"PO notes refuse {_SHOWN}: {exc.strerror or type(exc).__name__}") from None
    try:
        mode = os.fstat(opened).st_mode
    except OSError as exc:
        os.close(opened)
        raise PoNotesError(f"PO notes refuse {_SHOWN}: {exc.strerror or type(exc).__name__}") from None
    if not (stat.S_ISDIR(mode) if as_directory else stat.S_ISREG(mode)):
        os.close(opened)
        raise PoNotesError(f"PO notes refuse {_SHOWN}: a symlink or unsupported file type")
    return opened


def _open_data_dir(data_dir: Path) -> int:
    try:
        return os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise PoNotesError(f"PO notes cannot open the data directory: {exc.strerror or type(exc).__name__}") from None


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_notes(workspace: int, *, stable: bool) -> bytes | None:
    """The bytes of the notes file beneath `workspace`; `stable` refuses a file that changed while read."""
    handle = _open_beneath(workspace, NOTES_FILE, as_directory=False)
    if handle is None:
        return None
    try:
        before = os.fstat(handle)
        content = _read_all(handle)
        after = os.fstat(handle)
    except OSError as exc:
        raise PoNotesError(f"PO notes refuse {_SHOWN}: {exc.strerror or type(exc).__name__}") from None
    finally:
        os.close(handle)
    if stable:
        try:
            named = os.stat(NOTES_FILE, dir_fd=workspace, follow_symlinks=False)
        except OSError:
            named = None
        if (
            _identity(before) != _identity(after)
            or len(content) != before.st_size
            or named is None
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PoNotesError(f"PO notes refuse {_SHOWN}: the file changed during capture")
    return content


def capture_notes(data_dir: Path) -> bytes | None:
    """Read `<data>/po/NOTES.md` once as a UTF-8 regular file; None when the file or `po/` is absent."""
    root = _open_data_dir(data_dir)
    try:
        workspace = _open_beneath(root, WORKSPACE_NAME, as_directory=True)
        if workspace is None:
            return None
        try:
            content = _read_notes(workspace, stable=True)
        finally:
            os.close(workspace)
    finally:
        os.close(root)
    if content is not None:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise PoNotesError(f"PO notes refuse {_SHOWN}: the content is not UTF-8 text") from None
    return content


def write_notes_capture(directory: Path, content: bytes | None) -> dict[str, Any]:
    """Stage the payload for a present capture and return the coverage that declares it."""
    if content is not None:
        write_json(directory / NOTES_CHECKPOINT_FILE, {"version": 1, "text": content.decode("utf-8")})
    return notes_coverage(content)


def _payload_text(payload: str) -> str:
    value = json.loads(payload, object_pairs_hook=unique_json_fields)
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "text"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["text"], str)
    ):
        raise PoNotesError("PO notes payload fields are invalid")
    return value["text"]


def read_checkpoint_notes(directory: Path) -> NotesCapture | None:
    """Validate the declared capture before any write; no declaration and no payload is legacy."""
    try:
        board = open_checkpoint_board(directory)
        summary = read_export_summary(board)
        if COVERAGE_FIELD not in summary:
            if board.has(NOTES_CHECKPOINT_FILE):
                raise PoNotesError("undeclared PO notes coverage")
            return None
        coverage = summary[COVERAGE_FIELD]
        if not isinstance(coverage, dict) or type(coverage.get("version")) is not int or coverage["version"] != 1:
            raise PoNotesError("unsupported PO notes coverage")
        if coverage.get("state") == "absent":
            if coverage != notes_coverage(None):
                raise PoNotesError("PO notes absence coverage is invalid")
            if board.has(NOTES_CHECKPOINT_FILE):
                raise PoNotesError("PO notes payload contradicts the captured absence")
            return NotesCapture(None)
        if coverage.get("state") != "present" or type(coverage.get("bytes")) is not int:
            raise PoNotesError("unsupported PO notes coverage")
        content = _payload_text(board.read_text(NOTES_CHECKPOINT_FILE)).encode("utf-8")
        if coverage != notes_coverage(content):
            raise PoNotesError("PO notes size/digest mismatch")
        return NotesCapture(content)
    except PoNotesError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PoNotesError("PO notes checkpoint is unreadable or invalid") from None


def scan_notes_secrets(text: str, scrub: Callable[[str], str]) -> None:
    """Scan the decoded notes, so JSON escaping cannot hide a known secret value; name no content."""
    try:
        notes = _payload_text(text)
    except ValueError:
        raise PoNotesError("PO notes payload is unreadable") from None
    if scrub(notes) != notes:
        raise PoNotesError("secret detected in PO notes")


def _conflict() -> PoNotesError:
    return PoNotesError(f"PO notes target conflict: {_SHOWN} holds other content and was left unchanged")


def publish_notes(data_dir: Path, content: bytes) -> bool:
    """Create `<data>/po/NOTES.md` as a private file holding `content`; True when this call created it.

    A target file with exactly these bytes is parity; any other file there is a conflict, never
    replaced. Publication is one hard link of a synced staged file, so a crash leaves either no
    notes file or the whole one.
    """
    root = _open_data_dir(data_dir)
    try:
        workspace = _open_beneath(root, WORKSPACE_NAME, as_directory=True)
        if workspace is None:
            try:
                os.mkdir(WORKSPACE_NAME, 0o700, dir_fd=root)
            except FileExistsError:
                pass
            except OSError as exc:
                raise PoNotesError(f"PO notes cannot create {WORKSPACE_NAME}/: {exc.strerror}") from None
            workspace = _open_beneath(root, WORKSPACE_NAME, as_directory=True)
            if workspace is None:
                raise PoNotesError(f"PO notes refuse {WORKSPACE_NAME}/: it vanished during publication")
        try:
            return _publish_beneath(workspace, content)
        except OSError as exc:
            raise PoNotesError(f"PO notes publication failed: {exc.strerror or type(exc).__name__}") from None
        finally:
            os.close(workspace)
    finally:
        os.close(root)


def _publish_beneath(workspace: int, content: bytes) -> bool:
    existing = _read_notes(workspace, stable=False)
    if existing is not None:
        if existing != content:
            raise _conflict()
        _discard_staged(workspace)
        return False
    _discard_staged(workspace)
    staged = os.open(
        _STAGED_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=workspace
    )
    try:
        view = memoryview(content)
        while view:
            view = view[os.write(staged, view) :]
        os.fchmod(staged, 0o600)
        os.fsync(staged)
    finally:
        os.close(staged)
    try:
        os.link(_STAGED_NAME, NOTES_FILE, src_dir_fd=workspace, dst_dir_fd=workspace, follow_symlinks=False)
        created = True
    except FileExistsError:
        created = False
    _discard_staged(workspace)
    os.fsync(workspace)
    if _read_notes(workspace, stable=False) != content:
        raise _conflict() if not created else PoNotesError("PO notes target parity mismatch after publication")
    return created


def _discard_staged(workspace: int) -> None:
    try:
        os.unlink(_STAGED_NAME, dir_fd=workspace)
    except FileNotFoundError:
        pass
