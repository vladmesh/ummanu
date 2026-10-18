"""Images the owner attaches to a PO message, stored once by content in the session's workspace directory.

A stored image is `<data>/po/sessions/<session>/attachments/<sha256>.<ext>`: inside the PO workspace,
so the head reads it by its absolute path with its own file tool. Only PNG, JPEG and WebP are kept,
recognised by their leading bytes and never by a name or a declared type. Publication takes the
installation's PO recovery target lock, the boundary a checkpoint inventory holds while it reads the
PO SQL cut.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from ummanu._fsutil import BeneathRefused, open_beneath, write_bytes_atomic
from ummanu.po.workspace import WORKSPACE_NAME, workspace_dir

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
ATTACHMENT_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|webp)$")
_SESSION_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class AttachmentRefused(ValueError):
    """The bytes or the name are not an image this session may hold; nothing was written."""


class AttachmentCaptureError(RuntimeError):
    """The stored images cannot be captured whole and exactly; nothing of them is quoted."""


class AttachmentPublicationError(ValueError):
    """A captured image cannot reach native parity without replacing or trusting unsafe content."""


def image_kind(data: bytes) -> str:
    """The extension of a PNG, JPEG or WebP image, read from its signature."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise AttachmentRefused("an attachment is a PNG, JPEG or WebP image")


def attachments_dir(data_dir: Path | str, session_id: str) -> Path:
    if not _SESSION_SEGMENT.match(session_id):
        raise AttachmentRefused(f"{session_id!r} is not a PO session id")
    return workspace_dir(data_dir) / "sessions" / session_id / "attachments"


def store_image(data_dir: Path | str, session_id: str, data: bytes) -> Path:
    """Keep `data` under its content name and answer its absolute path; the same bytes land once."""
    if not data:
        raise AttachmentRefused("an attachment needs the image bytes")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentRefused(f"an attachment is at most {MAX_ATTACHMENT_BYTES} bytes")
    from ummanu.po.recovery_admission import recovery_target_lock

    name = f"{hashlib.sha256(data).hexdigest()}.{image_kind(data)}"
    path = attachments_dir(data_dir, session_id) / name
    with recovery_target_lock(Path(data_dir)):
        if not path.is_file():
            write_bytes_atomic(path, data)
    return path.absolute()


def attachment_path(data_dir: Path | str, session_id: str, name: str) -> Path:
    """The absolute path of a stored attachment of this session; a name it does not hold is refused."""
    if not ATTACHMENT_NAME.match(name):
        raise AttachmentRefused(f"{name!r} is not an attachment name")
    path = attachments_dir(data_dir, session_id) / name
    if not path.is_file():
        raise AttachmentRefused(f"PO session {session_id} holds no attachment {name}")
    return path.absolute()


def media_type(name: str) -> str:
    return MEDIA_TYPES[name.rsplit(".", 1)[-1]]


@dataclass(frozen=True)
class StoredAttachment:
    """One stored image as an inventory saw it: its session, content name and size."""

    session_id: str
    name: str
    size: int


@dataclass(frozen=True)
class AttachmentInventory:
    """The stored images of the given sessions in (session, name) order, and how many had a directory."""

    sessions: int
    directories: int
    files: tuple[StoredAttachment, ...]


def inventory_attachments(data_dir: Path | str, session_ids: list[str]) -> AttachmentInventory:
    """List, under the publication lock, every stored image of these sessions without reading it.

    Directories are opened beneath the data directory without following links. Dot names are a
    publication in flight and other regular files are not images; a link, a non-regular entry or an
    image name whose size no upload can have refuses the whole inventory.
    """
    from ummanu.po.recovery_admission import RecoveryAdmissionError, recovery_target_lock

    files: list[StoredAttachment] = []
    directories = 0
    try:
        with recovery_target_lock(Path(data_dir)):
            root = _open_data_dir(data_dir)
            try:
                for session_id in sorted(session_ids):
                    if not _SESSION_SEGMENT.match(session_id):
                        continue
                    found = _session_files(root, session_id)
                    if found is not None:
                        directories += 1
                        files.extend(found)
            finally:
                os.close(root)
    except RecoveryAdmissionError as exc:
        raise AttachmentCaptureError(f"PO attachment inventory cannot take its lock: {exc}") from None
    return AttachmentInventory(len(session_ids), directories, tuple(files))


def read_inventoried(data_dir: Path | str, item: StoredAttachment) -> bytes:
    """The exact bytes of one inventoried image; a vanished, changed or damaged file refuses."""
    shown = f"PO session {item.session_id} attachment {item.name}"
    root = _open_data_dir(data_dir)
    try:
        handle = _open(root, [*_SESSIONS, item.session_id, "attachments", item.name], shown, directory=False)
    finally:
        os.close(root)
    if handle is None:
        raise AttachmentCaptureError(f"{shown} vanished after the inventory")
    try:
        chunks: list[bytes] = []
        remaining = item.size + 1
        while remaining and (chunk := os.read(handle, min(remaining, 1 << 20))):
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise AttachmentCaptureError(f"{shown} cannot be read: {exc.strerror}") from None
    finally:
        os.close(handle)
    data = b"".join(chunks)
    if len(data) != item.size:
        raise AttachmentCaptureError(f"{shown} changed after the inventory")
    if not stored_image_matches(item.name, data):
        raise AttachmentCaptureError(f"{shown} does not hold the image its name names")
    return data


def stored_image_matches(name: str, data: bytes) -> bool:
    """Whether `data` is the image `store_image` names `name`: digest, signature and size."""
    try:
        kind = image_kind(data)
    except AttachmentRefused:
        return False
    return (0 < len(data) <= MAX_ATTACHMENT_BYTES
            and name == f"{hashlib.sha256(data).hexdigest()}.{kind}")


def publish_image(data_dir: Path, item: StoredAttachment, data: bytes) -> None:
    """Publish exact captured bytes privately without overwrite, syncing file and ancestry on every retry."""
    from ummanu.po.recovery_admission import RecoveryAdmissionError, recovery_target_lock

    shown = f"PO image publication session {item.session_id} attachment {item.name}"
    try:
        attachments_dir(data_dir, item.session_id)
        if item.size != len(data) or not stored_image_matches(item.name, data):
            raise AttachmentPublicationError("captured bytes do not match size, name or image type")
        if data_dir.resolve(strict=True) != data_dir.absolute():
            raise AttachmentPublicationError("data directory is not canonical")
        with recovery_target_lock(data_dir), ExitStack() as stack:
            root = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, root)
            _publication_identity(root, directory=True)
            directories = [root]
            for name in (*_SESSIONS, item.session_id, "attachments"):
                parent = directories[-1]
                opened = open_beneath(parent, [name], want_directory=True, missing_ok=True)
                if opened is None:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    opened = open_beneath(parent, [name], want_directory=True)
                stack.callback(os.close, opened)
                _publication_identity(opened, directory=True)
                directories.append(opened)
            _publish_image_beneath(directories[-1], item, data)
            # Existing names may come from a call interrupted before any ancestor's sync.
            for directory in reversed(directories):
                os.fsync(directory)
    except (AttachmentPublicationError, AttachmentRefused, RecoveryAdmissionError) as exc:
        raise AttachmentPublicationError(f"{shown}: {exc}") from None
    except BeneathRefused as exc:
        raise AttachmentPublicationError(f"{shown}: unsafe target ({exc.reason})") from None
    except OSError as exc:
        raise AttachmentPublicationError(f"{shown}: {exc.strerror or type(exc).__name__}") from None


def _publication_identity(handle: int, *, directory: bool) -> None:
    info = os.fstat(handle)
    unsafe_mode = bool(info.st_mode & 0o022) if directory else stat.S_IMODE(info.st_mode) != 0o600
    if info.st_uid != os.geteuid() or unsafe_mode:
        raise AttachmentPublicationError("target owner or permissions are unsafe")


def _read_published(directory: int, item: StoredAttachment, data: bytes) -> bool:
    handle = open_beneath(directory, [item.name], want_directory=False, missing_ok=True)
    if handle is None:
        return False
    try:
        _publication_identity(handle, directory=False)
        before = os.fstat(handle)
        if before.st_size != item.size or before.st_nlink != 1:
            raise AttachmentPublicationError("target size or link count conflicts with capture")
        chunks = []
        remaining = item.size + 1
        while remaining and (chunk := os.read(handle, min(remaining, 1 << 20))):
            chunks.append(chunk)
            remaining -= len(chunk)
        if b"".join(chunks) != data:
            raise AttachmentPublicationError("target bytes conflict with capture")
        os.fsync(handle)
        after = os.fstat(handle)
        named = os.stat(item.name, dir_fd=directory, follow_symlinks=False)
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)):
            raise AttachmentPublicationError("target changed during readback")
    finally:
        os.close(handle)
    return True


def _publish_image_beneath(directory: int, item: StoredAttachment, data: bytes) -> None:
    staged_name = f".{item.name}.restore.tmp"
    staged = open_beneath(directory, [staged_name], want_directory=False, missing_ok=True)
    if staged is not None:
        try:
            _publication_identity(staged, directory=False)
        finally:
            os.close(staged)
        os.unlink(staged_name, dir_fd=directory)
    if _read_published(directory, item, data):
        return
    staged = os.open(staged_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
    try:
        view = memoryview(data)
        while view:
            written = os.write(staged, view)
            if written == 0:
                raise AttachmentPublicationError("staged write made no progress")
            view = view[written:]
        os.fsync(staged)
    finally:
        os.close(staged)
    try:
        os.link(staged_name, item.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    except FileExistsError:
        pass
    os.unlink(staged_name, dir_fd=directory)
    if not _read_published(directory, item, data):
        raise AttachmentPublicationError("target vanished after publication")


_SESSIONS = (WORKSPACE_NAME, "sessions")
_REFUSALS = {"symlink": "a symlink", "directory": "not a plain directory", "regular": "not a regular file"}


def _open_data_dir(data_dir: Path | str) -> int:
    try:
        return os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise AttachmentCaptureError(f"PO attachment capture cannot open the data directory: {exc.strerror}") from None


def _open(root: int, names: list[str], shown: str, *, directory: bool) -> int | None:
    """`names` beneath `root`, never followed; None only when a component does not exist."""
    try:
        return open_beneath(root, names, want_directory=directory, missing_ok=True)
    except BeneathRefused as exc:
        raise AttachmentCaptureError(
            f"PO attachment capture refuses {shown}: {'/'.join(names[: exc.depth])} is "
            f"{_REFUSALS.get(exc.reason, exc.reason)}"
        ) from None
    except OSError as exc:
        raise AttachmentCaptureError(f"PO attachment capture refuses {shown}: {exc.strerror}") from None


def _session_files(root: int, session_id: str) -> list[StoredAttachment] | None:
    """The images of one session's directory in name order; None when it has no directory."""
    shown = f"PO session {session_id} attachments"
    directory = _open(root, [*_SESSIONS, session_id, "attachments"], shown, directory=True)
    if directory is None:
        return None
    try:
        names = sorted(os.listdir(directory))
        found: list[StoredAttachment] = []
        for position, name in enumerate(names, 1):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise AttachmentCaptureError(f"PO attachment capture refuses {shown} entry {position}: not a regular file")
            if not ATTACHMENT_NAME.match(name):
                continue
            if not 0 < info.st_size <= MAX_ATTACHMENT_BYTES:
                raise AttachmentCaptureError(f"PO attachment capture refuses {shown} entry {position}: its size")
            found.append(StoredAttachment(session_id, name, info.st_size))
        return found
    except OSError as exc:
        raise AttachmentCaptureError(f"PO attachment capture cannot list {shown}: {exc.strerror}") from None
    finally:
        os.close(directory)
