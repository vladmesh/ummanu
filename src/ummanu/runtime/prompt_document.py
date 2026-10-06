"""The document a head is given, and the one short line (nudge) that points it there.

Large pastes into an interactive head's input are unreliable, so the task lives in a file and the
pane gets a bounded line naming its absolute path. Invariants owned here:

  * the nudge is bounded and single-line whatever the document says (only its path is derived);
  * the document lives outside the worktree it describes, since receipts hash tracked diff plus
    untracked files.

The document is durable: it is the run's record of what the head was asked to do.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

# Bytes, because the terminal receives bytes. Kept below 256 because only short lines are proven
# reliable on the legacy pane transport.
NUDGE_MAX_BYTES = 256
# Telemetry records mode, nudge size and document path, never the document text.
NUDGE_FILE_MODE = "nudge-file"
_NUDGE_TEMPLATE = "Read {path} and do its task."
_DOCUMENT_MODE = 0o600
_DOCUMENT_DIR_MODE = 0o700


class PromptDocumentError(RuntimeError):
    """A document or nudge that would break the module's guarantees."""


def nudge_for(path: str | Path, note: str = "") -> str:
    """The one line a pane receives for a head that has a document waiting.

    The path must be absolute (the head's cwd is unknown), ASCII (terminal encoding is unprovable)
    and free of control bytes (a newline would split the nudge). `note` is appended and the ceiling
    applies to the whole line; a line that does not fit is refused, never truncated.
    """
    location = str(path)
    if not os.path.isabs(location):
        raise PromptDocumentError(f"a nudge names its document by absolute path, and {location!r} is not one")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in location):
        raise PromptDocumentError("a document path carrying control bytes cannot be nudged at")
    try:
        location.encode("ascii")
    except UnicodeEncodeError:
        raise PromptDocumentError(
            f"a nudge carries an ASCII document path, and {location!r} is not one"
        ) from None
    nudge = _NUDGE_TEMPLATE.format(path=location)
    if note:
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in note):
            raise PromptDocumentError("a nudge note carrying control bytes cannot be delivered")
        nudge = f"{nudge} {note}"
    size = len(nudge.encode("utf-8"))
    if size > NUDGE_MAX_BYTES:
        raise PromptDocumentError(
            f"the nudge for {location} is {size} bytes, over the {NUDGE_MAX_BYTES}-byte ceiling"
        )
    return nudge


def write_prompt_document(path: str | Path, text: str, *, outside: str | Path | None = None) -> Path:
    """Write one head's task document atomically, private (0600, directory 0700).

    `outside` is the worktree the document describes; a path inside it is refused. A retry with
    identical content keeps content and mtime (mtime means "last given a task") and only fixes mode.
    """
    document = Path(path)
    if not document.is_absolute():
        raise PromptDocumentError(f"a prompt document is written by absolute path, and {document} is not one")
    if outside is not None:
        _refuse_inside(document, Path(outside))
    directory = document.parent
    try:
        directory.mkdir(mode=_DOCUMENT_DIR_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise PromptDocumentError(f"prompt document directory {directory} is unusable: {exc}") from None
    if _already_holds(document, text):
        _make_private(document)
        return document
    _replace_atomically(document, text)
    return document


def _make_private(document: Path) -> None:
    """Enforce 0600 on an existing correct document without rewriting it (keeps mtime)."""
    try:
        if stat.S_IMODE(document.stat().st_mode) != _DOCUMENT_MODE:
            os.chmod(document, _DOCUMENT_MODE)
    except OSError as exc:
        raise PromptDocumentError(f"prompt document {document} could not be made private: {exc}") from None


def _encoded(text: str) -> bytes:
    """The document's UTF-8 bytes; unencodable text is a caller bug, refused rather than written lossily."""
    try:
        return str(text).encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise PromptDocumentError(f"prompt document text is not encodable as UTF-8: {exc}") from None


def _refuse_inside(document: Path, worktree: Path) -> None:
    """Refuse a document inside the checkout it is about; both sides resolved through symlinks."""
    resolved = Path(os.path.realpath(document))
    tree = Path(os.path.realpath(worktree))
    if resolved == tree or tree in resolved.parents:
        raise PromptDocumentError(
            f"a prompt document may not live inside the worktree it describes: {resolved} is under {tree}"
        )


def _already_holds(document: Path, text: str) -> bool:
    try:
        return document.read_bytes() == _encoded(text)
    except OSError:
        return False


def _replace_atomically(document: Path, text: str) -> None:
    """Write the document's bytes unmodified and swap it into place atomically.

    Binary mode preserves CRLF from web-form prompts.
    """
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{document.name}.", suffix=".tmp", dir=document.parent)
        temp_path = Path(temp_name)
        os.chmod(temp_path, _DOCUMENT_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_encoded(text))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, document)
        temp_path = None
    except OSError as exc:
        raise PromptDocumentError(f"prompt document {document} could not be written: {exc}") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
