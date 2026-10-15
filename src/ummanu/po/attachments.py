"""Images the owner attaches to a PO message, stored once by content in the session's workspace directory.

A stored image is `<data>/po/sessions/<session>/attachments/<sha256>.<ext>`: inside the PO workspace,
so the head reads it by its absolute path with its own file tool. Only PNG, JPEG and WebP are kept,
recognised by their leading bytes and never by a name or a declared type.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ummanu._fsutil import write_bytes_atomic
from ummanu.po.workspace import workspace_dir

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
ATTACHMENT_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|webp)$")
_SESSION_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class AttachmentRefused(ValueError):
    """The bytes or the name are not an image this session may hold; nothing was written."""


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
    name = f"{hashlib.sha256(data).hexdigest()}.{image_kind(data)}"
    path = attachments_dir(data_dir, session_id) / name
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
