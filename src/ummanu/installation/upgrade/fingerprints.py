"""One invocation's source cut, with fail-closed metadata fences and no payload cache.

The filesystem must report normal writes in nanosecond mtime/ctime and replacement in
device/inode identity. This is coherence evidence, not adversarial metadata attestation.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ummanu.installation.upgrade.checkout import _git, _product_revision
from ummanu.installation.upgrade.types import (
    PRODUCT_SOURCE_PATHS,
    SCHEMA_PATHS,
    GitError,
    WebProcessReceiptError,
)


class SourceCutInvalidated(WebProcessReceiptError):
    """The selected source cut changed or its stability could not be established."""


def _file_stamp(info: os.stat_result) -> tuple[int, ...]:
    if not stat.S_ISREG(info.st_mode):
        raise OSError("not a regular file")
    stamp = (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )
    if any(type(value) is not int for value in stamp):
        raise OSError("incomplete source metadata")
    return stamp


def _directory_stamp(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"not a directory: {path}")
    # Creating untracked files/bytecode must not invalidate a tracked payload cut.
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _feed(digest, relative: str, contents: bytes) -> None:
    digest.update(b"\0file\0")
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(contents)


class SourceCut:
    """Lazy owner installed only by run_steps; closed copies cannot reacquire a cut."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.selected: Path | None = None
        self.revision = ""
        self.members: dict[tuple[str, ...], tuple[str, ...]] = {}
        self.files: dict[str, tuple[int, ...]] = {}
        self.directories: dict[Path, tuple[int, ...]] = {}
        self.digests: dict[tuple[str, ...], str] = {}
        self.failure: str | None = None

    def close(self) -> None:
        self.failure = "source cut scope is closed"
        self.root = None
        self.selected = None
        self.revision = ""
        self.members.clear()
        self.digests.clear()
        self.files.clear()
        self.directories.clear()

    def _list(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        if self.root is None:
            raise OSError("no selected product root")
        return tuple(filter(None, _git(self.root, ["ls-files", "--", *paths]).splitlines()))

    def _observe(self, selected: Path) -> None:
        if self.root is None:
            raise OSError("no selected product root")
        if selected.absolute() != self.selected or selected.resolve(strict=True) != self.root:
            raise OSError("selected product root changed")
        for directory, stamp in self.directories.items():
            if _directory_stamp(directory) != stamp:
                raise OSError(f"source directory identity changed: {directory}")
        if _product_revision(self.root, SourceCutInvalidated) != self.revision:
            raise OSError("product HEAD changed")
        for paths, members in self.members.items():
            if self._list(paths) != members:
                raise OSError(f"tracked membership changed: {paths}")
        for relative, stamp in self.files.items():
            if _file_stamp((self.root / relative).lstat()) != stamp:
                raise OSError(f"source file changed: {relative}")

    def check(self, selected: Path) -> None:
        if self.failure is not None:
            raise SourceCutInvalidated(self.failure)
        if self.root is None:
            return
        try:
            self._observe(selected)
        except (OSError, RuntimeError, GitError) as exc:
            self.failure = f"source cut invalidated: {exc}"
            raise SourceCutInvalidated(self.failure) from None

    def _capture(self, selected: Path) -> None:
        self.selected = selected.absolute()
        self.root = selected.resolve(strict=True)
        self.revision = _product_revision(self.root, SourceCutInvalidated)
        self.members = {paths: self._list(paths) for paths in (PRODUCT_SOURCE_PATHS, SCHEMA_PATHS)}
        sources = self.members[PRODUCT_SOURCE_PATHS]
        schemas = self.members[SCHEMA_PATHS]
        schema_set = set(schemas)
        if tuple(name for name in sources if name in schema_set) != schemas:
            raise OSError("schemas are not an ordered subset of tracked source inputs")
        directories = {self.root, *self.root.parents}
        for relative in sources:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise OSError(f"unsafe tracked source path: {relative}")
            directories.update((self.root / path).parents)
        self.directories = {path: _directory_stamp(path) for path in directories}
        self.files = {relative: _file_stamp((self.root / relative).lstat()) for relative in sources}
        self._observe(selected)
        digests = {paths: hashlib.sha256() for paths in self.members}
        for paths, digest in digests.items():
            digest.update(b"ummanu-web-input-v1\0")
            digest.update("\0".join(paths).encode("utf-8"))
        for relative, stamp in self.files.items():
            # No-follow + nonblocking prevents a swapped symlink/FIFO from being read.
            fd = os.open(self.root / relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if _file_stamp(os.fstat(stream.fileno())) != stamp:
                    raise OSError(f"source identity changed before read: {relative}")
                contents = stream.read()
                if len(contents) != stamp[6]:
                    raise OSError(f"incomplete source read: {relative}")
                if _file_stamp(os.fstat(stream.fileno())) != stamp:
                    raise OSError(f"source changed during read: {relative}")
            if _file_stamp((self.root / relative).lstat()) != stamp:
                raise OSError(f"source identity changed after read: {relative}")
            _feed(digests[PRODUCT_SOURCE_PATHS], relative, contents)
            if relative in schema_set:
                _feed(digests[SCHEMA_PATHS], relative, contents)
        self._observe(selected)
        self.digests = {paths: digest.hexdigest() for paths, digest in digests.items()}

    def digest(self, selected: Path, paths: tuple[str, ...], *, revision: str | None = None) -> str:
        self.check(selected)
        if self.root is None:
            try:
                self._capture(selected)
            except (OSError, RuntimeError, GitError) as exc:
                self.failure = f"source cut acquisition failed: {exc}"
                raise SourceCutInvalidated(self.failure) from None
        if revision is not None and revision != self.revision:
            self.failure = "source cut invalidated: product HEAD changed during input selection"
            raise SourceCutInvalidated(self.failure)
        return self.digests[paths]
