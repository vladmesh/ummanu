"""Writer for long recoverable documents in `state/knowledge`.

Contract: docs/ARCHITECTURE.md, "Knowledge planes", and docs/RECOVERY.md, "Writers". Knowledge holds
the long reasoning behind a decision: brainstorms, decision logs, incident write-ups. It is plain
markdown in the live root, and `state/knowledge/**` is in the snapshot export allowlist, so a
document written here leaves the host with the next checkpoint (the legacy tick commits it, the
snapshot exporter copies it) and survives a move to another machine.

The writer starts no Git child. It writes files only, under `state_repo_lock`, the live-root writer
lock the tick holds while it commits or cuts, so neither ever sees half a write. A document is one
atomic file replace; a directory is swapped in whole through `state/.knowledge-swap` and put back on
failure. Where a commit id used to be, the result carries the content revision of what was written
(`_fsutil.content_revision`): the same content gives the same revision.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ummanu import state_repo
from ummanu._fsutil import content_revision, write_text_atomic as _write_text_atomic
from ummanu.runtime.redact import redact


class KnowledgeError(RuntimeError):
    """A knowledge write did not happen."""


class KnowledgeValidationError(KnowledgeError):
    """The document or its path is not something this writer accepts.

    `reason` is a short fixed word a caller can branch on (`path`, `source_missing`, `source_empty`,
    `special_file`, `secret`, `size_cap`, `missing`); `""` where no caller needs one.
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


# The whole of one directory write, in bytes. A report directory holds a write-up and the scripts and
# data behind it, not a dataset; anything larger belongs outside the live root.
KNOWLEDGE_DIRECTORY_CAP_BYTES = 20 * 1024 * 1024

# Where a directory write stages the new contents and parks the previous ones. It is beside
# `state/knowledge`, on the same filesystem so the swap is a rename, and outside the export allowlist
# and the legacy tick's pathspecs, so a crash mid-swap leaves nothing a checkpoint can pick up.
KNOWLEDGE_SWAP_RELATIVE = Path("state") / ".knowledge-swap"
_SWAP_TARGET = "target"


@dataclass(frozen=True)
class KnowledgeDocument:
    """One knowledge write that has passed every check this writer makes before it writes.

    The product of :func:`check_knowledge_document`, and what :func:`write_knowledge_document`
    writes: the cleaned actor, the path relative to `state/knowledge`, the body, and the live root
    the write lands in.
    """

    actor: str
    document: PurePosixPath
    text: str
    instance_dir: Path


@dataclass(frozen=True)
class KnowledgeWriteResult:
    """One knowledge write. `commit` holds the content revision of what was written (the document, or
    every file of the directory, by its path below `state/knowledge`), not a Git commit: the writer
    makes none. An unchanged write answers the same revision as the write that put the content there."""

    document: str
    path: Path
    commit: str
    actor: str
    changed: bool


def check_knowledge_document(
    instance_dir: Path,
    *,
    document: str,
    actor: str,
    text: str | None = None,
    source_file: Path | None = None,
) -> KnowledgeDocument:
    """Everything :func:`write_knowledge_document` refuses, asked without writing anything.

    The preflight half of the one writer, split out rather than restated: a caller that has to
    know *before* it starts an operation whether the document at the end of it can be written --
    a sprint close, which must refuse before its first board write rather than halfway through --
    asks this, and the rules it is answered by are the write's own.
    """
    actor = _clean_actor(actor)
    relative = _clean_document(document)
    body = _document_text(text=text, source_file=source_file)
    # Knowledge leaves the host with the rest of the checkpoint, and a brainstorm
    # describes infrastructure by its nature, so it passes the same secret gate
    # the tick and memory writers apply.
    if redact(body) != body:
        raise KnowledgeValidationError(f"secret detected in state/knowledge/{relative}")
    root = _live_root(instance_dir)
    _check_parents(root, relative)
    return KnowledgeDocument(actor, relative, body, root)


def write_knowledge_document(
    instance_dir: Path,
    *,
    document: str,
    actor: str,
    text: str | None = None,
    source_file: Path | None = None,
) -> KnowledgeWriteResult:
    """Write one document under `state/knowledge` in one atomic replace.

    `changed=False` means the document on disk already had this content and nothing was written.
    """
    checked = check_knowledge_document(
        instance_dir, document=document, actor=actor, text=text, source_file=source_file
    )
    actor, relative, body = checked.actor, checked.document, checked.text
    instance_dir = checked.instance_dir
    target = state_repo.knowledge_dir(instance_dir) / relative
    payload = body.encode("utf-8")
    revision = content_revision({str(relative): hashlib.sha256(payload).hexdigest()})
    with state_repo.state_repo_lock(instance_dir):
        _recover_interrupted_swaps(instance_dir)
        if _current_bytes(target) == payload:
            return KnowledgeWriteResult(
                document=str(relative), path=target, commit=revision, actor=actor, changed=False
            )
        created = _missing_parents(target, instance_dir)
        try:
            _write_text_atomic(target, body)
        except RuntimeError as exc:
            _remove_empty(created)
            raise KnowledgeError(f"could not write state/knowledge/{relative}: {exc}") from None
    return KnowledgeWriteResult(
        document=str(relative),
        path=target,
        commit=revision,
        actor=actor,
        changed=True,
    )


@dataclass(frozen=True)
class KnowledgeDirectory:
    """One directory write that has passed every check :func:`write_knowledge_directory` makes.

    `files` holds each regular file of the source, by its path relative to the source, with its bytes.
    """

    actor: str
    directory: PurePosixPath
    files: tuple[tuple[PurePosixPath, bytes], ...]
    instance_dir: Path


def check_knowledge_directory(
    instance_dir: Path,
    *,
    directory: str,
    actor: str,
    source_dir: Path,
) -> KnowledgeDirectory:
    """Everything :func:`write_knowledge_directory` refuses, asked without writing anything.

    The source must be a real, non-empty directory of regular files and subdirectories: a symlink or a
    special file anywhere in it is refused, as is a name Git reads (`_GIT_CONTROL_NAMES`), a total size
    over `KNOWLEDGE_DIRECTORY_CAP_BYTES`, and a text file the secret gate would change. A binary file (one
    that is not UTF-8 or holds a NUL byte) is copied unchanged and is not secret-scanned.
    """
    actor = _clean_actor(actor)
    relative = _clean_directory(directory)
    files = _directory_files(Path(str(source_dir)).expanduser())
    for name, data in files:
        text = _text_or_none(data)
        if text is not None and redact(text) != text:
            raise KnowledgeValidationError(
                f"secret detected in {name} (target state/knowledge/{relative}/{name})", "secret"
            )
    root = _live_root(instance_dir)
    _check_parents(root, relative)
    return KnowledgeDirectory(actor, relative, files, root)


def write_knowledge_directory(
    instance_dir: Path,
    *,
    directory: str,
    actor: str,
    source_dir: Path,
) -> KnowledgeWriteResult:
    """Replace one directory under `state/knowledge` with the source directory, all or nothing.

    Under one `state_repo_lock` the target's whole contents become the source's (a file the source no
    longer has disappears from the target). `changed=False` means the directory already held exactly
    this content and nothing was written. A failure at any point leaves `state/knowledge`
    byte-identical to before: the previous directory is put back and any parent the write created
    is removed.
    """
    checked = check_knowledge_directory(instance_dir, directory=directory, actor=actor, source_dir=source_dir)
    instance_dir = checked.instance_dir
    target = state_repo.knowledge_dir(instance_dir) / checked.directory
    # Named by the path below `state/knowledge`, as a document revision is: equal files written to
    # two different directories are two different writes.
    revision = content_revision(
        {
            (checked.directory / name).as_posix(): hashlib.sha256(data).hexdigest()
            for name, data in checked.files
        }
    )
    with state_repo.state_repo_lock(instance_dir):
        if target.exists() and (target.is_symlink() or not target.is_dir()):
            raise KnowledgeValidationError(
                f"state/knowledge/{checked.directory} exists and is not a directory", "path"
            )
        _recover_interrupted_swaps(instance_dir)
        if _directory_holds(target, checked.files):
            return KnowledgeWriteResult(
                document=f"{checked.directory}/",
                path=target,
                commit=revision,
                actor=checked.actor,
                changed=False,
            )
        swap = _replace_directory(instance_dir, target, checked.files)
        shutil.rmtree(swap, ignore_errors=True)
    return KnowledgeWriteResult(
        document=f"{checked.directory}/",
        path=target,
        commit=revision,
        actor=checked.actor,
        changed=True,
    )


def delete_knowledge_document(instance_dir: Path, *, document: str, actor: str) -> KnowledgeWriteResult:
    """Remove one document from `state/knowledge` under the writer lock, with any parent it leaves empty.

    `commit` is the content revision of the removed document. A document that is not there is refused
    with reason `missing`.
    """
    actor = _clean_actor(actor)
    relative = _clean_document(document)
    root = _live_root(instance_dir)
    knowledge = state_repo.knowledge_dir(root)
    target = knowledge / relative
    with state_repo.state_repo_lock(root):
        _recover_interrupted_swaps(root)
        payload = _current_bytes(target)
        if payload is None:
            raise KnowledgeValidationError(f"state/knowledge/{relative} is not a document", "missing")
        try:
            target.unlink()
        except OSError as exc:
            raise KnowledgeError(f"could not remove state/knowledge/{relative}: {exc}") from None
        parent = target.parent
        while parent != knowledge and knowledge in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return KnowledgeWriteResult(
        document=str(relative),
        path=target,
        commit=content_revision({str(relative): hashlib.sha256(payload).hexdigest()}),
        actor=actor,
        changed=True,
    )


def list_knowledge_documents(instance_dir: Path) -> tuple[str, ...]:
    """Every markdown document currently under `state/knowledge`."""
    root = state_repo.knowledge_dir(instance_dir)
    if not root.is_dir():
        return ()
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*.md") if path.is_file()))


def _live_root(instance_dir: Path) -> Path:
    """The live root a write lands in: an existing directory, Git work tree or not."""
    root = Path(instance_dir).expanduser().resolve()
    if not root.is_dir():
        raise KnowledgeError(f"instance directory not found: {root}")
    return root


def _check_parents(root: Path, relative: PurePosixPath) -> None:
    """Refuse a target whose way down from the live root passes something that is not a directory."""
    path = root
    for part in (*state_repo.KNOWLEDGE_RELATIVE.parts, *relative.parent.parts):
        path = path / part
        if os.path.lexists(path) and not path.is_dir():
            raise KnowledgeValidationError(f"{path.relative_to(root).as_posix()} exists and is not a directory", "path")


def _current_bytes(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _directory_holds(target: Path, files: tuple[tuple[PurePosixPath, bytes], ...]) -> bool:
    """Whether `target` is a plain directory holding exactly `files`, byte for byte, and nothing else."""
    if target.is_symlink() or not target.is_dir():
        return False
    expected = {name.as_posix(): data for name, data in files}
    seen = 0
    for root, dirnames, filenames in os.walk(target):
        for name in [*dirnames, *filenames]:
            path = Path(root) / name
            if path.is_symlink():
                return False
            if path.is_dir():
                continue
            relative = path.relative_to(target).as_posix()
            if expected.get(relative) != _current_bytes(path):
                return False
            seen += 1
    return seen == len(expected)


def _missing_parents(path: Path, stop: Path) -> list[Path]:
    """The ancestors of `path` below `stop` that do not exist yet, deepest first."""
    missing = []
    parent = path.parent
    while parent != stop and stop in parent.parents and not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    return missing


def _remove_empty(directories: list[Path]) -> None:
    """Remove directories a failed write created, deepest first, while they are still empty."""
    for directory in directories:
        with contextlib.suppress(OSError):
            directory.rmdir()


def _clean_actor(actor: str) -> str:
    value = actor.strip()
    if not value:
        raise KnowledgeValidationError("actor is required")
    return value


_PATH_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _clean_document(document: str) -> PurePosixPath:
    value = document.strip()
    if not value:
        raise KnowledgeValidationError("document path is required")
    if value.startswith(("/", "~")):
        raise KnowledgeValidationError("document path must be relative to state/knowledge")
    value = value.removeprefix("state/knowledge/").rstrip("/")
    if not value.endswith(".md"):
        raise KnowledgeValidationError("knowledge document must be a .md file")
    return _clean_parts(value, "document")


def _clean_directory(directory: str) -> PurePosixPath:
    value = directory.strip()
    if not value:
        raise KnowledgeValidationError("directory path is required", "path")
    if value.startswith(("/", "~")):
        raise KnowledgeValidationError("directory path must be relative to state/knowledge", "path")
    value = value.removeprefix("state/knowledge/").rstrip("/")
    if not value or value == "state/knowledge":
        raise KnowledgeValidationError("directory path must name a directory below state/knowledge", "path")
    return _clean_parts(value, "directory")


def _clean_parts(value: str, what: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    for part in value.split("/"):
        if part in {"", ".", ".."} or any(char not in _PATH_CHARACTERS for char in part):
            raise KnowledgeValidationError(f"{what} path contains unsupported part: {part}", "path")
    return relative


# Names Git itself reads: `.git` is a repository, and the other three change what a later commit of
# the instance repository records. Any other dot name, `.github` included, is plain content.
_GIT_CONTROL_NAMES = frozenset({".git", ".gitignore", ".gitattributes", ".gitmodules"})


def _directory_files(source: Path) -> tuple[tuple[PurePosixPath, bytes], ...]:
    """Every regular file below `source`, sorted, after the structural and size refusals."""
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError:
        raise KnowledgeValidationError(f"source directory not found: {source}", "source_missing") from None
    except OSError as exc:
        raise KnowledgeError(f"could not read {source}: {exc}") from None
    if stat.S_ISLNK(mode):
        raise KnowledgeValidationError(f"source directory is a symlink: {source}", "special_file")
    if not stat.S_ISDIR(mode):
        raise KnowledgeValidationError(f"source is not a directory: {source}", "source_missing")
    found: list[tuple[PurePosixPath, Path, int]] = []
    total = 0
    for root, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted([*dirnames, *filenames]):
            path = Path(root) / name
            relative = PurePosixPath(path.relative_to(source).as_posix())
            if name in _GIT_CONTROL_NAMES:
                raise KnowledgeValidationError(f"source holds a name Git reads: {relative}", "special_file")
            try:
                entry = path.lstat()
            except OSError as exc:
                raise KnowledgeError(f"could not read {path}: {exc}") from None
            if stat.S_ISLNK(entry.st_mode):
                raise KnowledgeValidationError(f"source holds a symlink: {relative}", "special_file")
            if stat.S_ISDIR(entry.st_mode):
                continue
            if not stat.S_ISREG(entry.st_mode):
                raise KnowledgeValidationError(f"source holds a special file: {relative}", "special_file")
            total += entry.st_size
            if total > KNOWLEDGE_DIRECTORY_CAP_BYTES:
                raise KnowledgeValidationError(
                    f"source directory is over the {KNOWLEDGE_DIRECTORY_CAP_BYTES // (1024 * 1024)} MiB cap: "
                    f"{source}",
                    "size_cap",
                )
            found.append((relative, path, entry.st_size))
    if not found:
        raise KnowledgeValidationError(f"source directory holds no files: {source}", "source_empty")
    files: list[tuple[PurePosixPath, bytes]] = []
    for relative, path, _size in found:
        try:
            files.append((relative, path.read_bytes()))
        except OSError as exc:
            raise KnowledgeError(f"could not read {path}: {exc}") from None
    if sum(len(data) for _name, data in files) > KNOWLEDGE_DIRECTORY_CAP_BYTES:
        raise KnowledgeValidationError(
            f"source directory is over the {KNOWLEDGE_DIRECTORY_CAP_BYTES // (1024 * 1024)} MiB cap: {source}",
            "size_cap",
        )
    return tuple(files)


def _text_or_none(data: bytes) -> str | None:
    """The file as text when it is UTF-8 without NUL bytes; None for a binary file."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _replace_directory(instance_dir: Path, target: Path, files: tuple[tuple[PurePosixPath, bytes], ...]) -> Path:
    """Swap `target` for a directory holding `files`; the swap directory is returned, with the previous
    `target` moved aside as its `old` when there was one.

    Staging happens in one swap directory under `KNOWLEDGE_SWAP_RELATIVE`: `new` is written there, the
    previous `target` is moved to `old` beside it, and `target` names the knowledge path both belong to,
    written first so :func:`_recover_interrupted_swaps` can put `old` back after a crash.
    """
    swap_root = Path(instance_dir) / KNOWLEDGE_SWAP_RELATIVE
    created = _missing_parents(target, Path(instance_dir))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        swap_root.mkdir(parents=True, exist_ok=True)
        swap = Path(tempfile.mkdtemp(prefix="swap.", dir=swap_root))
        (swap / _SWAP_TARGET).write_text(str(target.relative_to(instance_dir)), encoding="utf-8")
    except (OSError, ValueError) as exc:
        _remove_empty(created)
        raise KnowledgeError(f"could not write {target}: {exc}") from None
    staging = swap / "new"
    previous = swap / "old"
    moved_aside = False
    try:
        for name, data in files:
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        staging.mkdir(exist_ok=True)
        if os.path.lexists(target):
            os.replace(target, previous)
            moved_aside = True
        os.replace(staging, target)
    except Exception as exc:
        # A failure at any step leaves the knowledge tree as it was: the previous directory comes
        # back and parents this write created are removed. A crash that kills the process instead
        # is finished by the next writer (`_recover_interrupted_swaps`).
        if moved_aside:
            with contextlib.suppress(OSError):
                os.replace(previous, target)
        if not os.path.lexists(previous):
            shutil.rmtree(swap, ignore_errors=True)
        _remove_empty(created)
        if isinstance(exc, OSError):
            raise KnowledgeError(f"could not write {target}: {exc}") from None
        raise
    return swap


def _recover_interrupted_swaps(instance_dir: Path) -> None:
    """Finish every swap a crashed directory write left behind, before anything else is written.

    Called under `state_repo_lock`, so no swap is in flight. A swap whose previous directory was moved
    aside and whose target is gone has that directory put back; everything else in the swap root is
    staging nobody will use and is removed.
    """
    swap_root = Path(instance_dir) / KNOWLEDGE_SWAP_RELATIVE
    if not swap_root.is_dir() or swap_root.is_symlink():
        return
    knowledge = state_repo.knowledge_dir(instance_dir)
    for swap in sorted(swap_root.iterdir()):
        previous = swap / "old"
        try:
            relative = (swap / _SWAP_TARGET).read_text(encoding="utf-8").strip()
        except OSError:
            relative = ""
        target = Path(instance_dir) / relative if relative else None
        if (
            target is not None
            and previous.is_dir()
            and not previous.is_symlink()
            and target.parent.resolve().is_relative_to(knowledge)
            and not os.path.lexists(target)
        ):
            with contextlib.suppress(OSError):
                os.replace(previous, target)
        if swap.is_dir() and not swap.is_symlink():
            shutil.rmtree(swap, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                swap.unlink()


def _document_text(*, text: str | None, source_file: Path | None) -> str:
    if (text is None) == (source_file is None):
        raise KnowledgeValidationError("pass exactly one of text or source_file")
    if text is None:
        source_file = Path(str(source_file)).expanduser()
        try:
            text = source_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise KnowledgeValidationError(f"document file not found: {source_file}") from None
        except OSError as exc:
            raise KnowledgeError(f"could not read {source_file}: {exc}") from None
        except UnicodeError as exc:
            raise KnowledgeValidationError(f"could not decode {source_file}: {exc}") from None
    if not text.strip():
        raise KnowledgeValidationError("document is empty")
    return text if text.endswith("\n") else text + "\n"
