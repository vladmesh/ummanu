from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat as stat_module
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import IO, Any, BinaryIO

from ummanu import _proc


def write_json(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def ndjson_line(row: Any) -> str:
    """Serialize one UTF-8 record without raw Unicode line separators."""
    text = json.dumps(row, ensure_ascii=False, sort_keys=True)
    for character in ("\u2028", "\u2029", "\u0085"):
        text = text.replace(character, f"\\u{ord(character):04x}")
    return text + "\n"


def ndjson_lines(text: str) -> list[str]:
    """Split LF/CRLF records, preserving Unicode separators inside old JSON strings."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def write_ndjson(path: Path, rows: list[dict[str, Any]]) -> None:
    body = "".join(ndjson_line(row) for row in rows)
    write_text_atomic(path, body)


def fsync_directory(directory: Path | str) -> None:
    """Flush a directory's entries, so a rename or a new name in it survives a host crash."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_text_atomic(path: Path, payload: str) -> None:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_path, path)
    except OSError as exc:
        raise RuntimeError(f"could not write export file {path}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def write_bytes_atomic(path: Path, payload: bytes, *, mode: int | None = None) -> None:
    """Replace `path` with `payload` through a synced temporary beside it, optionally with `mode`."""
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except OSError as exc:
        raise RuntimeError(f"could not write {path}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def write_private_text_atomic_privileged(
    path: Path,
    payload: str,
    *,
    owner: tuple[int, int],
    privileged_argv: Callable[[list[str]], list[str]],
) -> None:
    """Stage a mode-0600, correctly owned file beside a privileged target, then rename it.

    The caller supplies the existing host's non-interactive sudo argv boundary. No privileged
    interpreter loads code from a user-owned checkout, and the old target survives a failed stage
    or rename. The random staging name lives in the target directory, never in a shared temp dir.
    """
    staged = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")

    def run(command: list[str], label: str, *, input_text: str | None = None) -> None:
        try:
            result = _proc.run(privileged_argv(command), input=input_text, timeout=60)
        except FileNotFoundError:
            raise RuntimeError(f"could not {label} {path}: {command[0]} not found") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"could not {label} {path}: {command[0]} timed out") from None
        except OSError as exc:
            raise RuntimeError(f"could not {label} {path}: {exc}") from None
        if result.returncode:
            raise RuntimeError(f"could not {label} {path}: {command[0]} exited {result.returncode}")

    try:
        run(
            [
                "install", "-T", "-m", "0600", "-o", str(owner[0]), "-g", str(owner[1]),
                "/dev/stdin", str(staged),
            ],
            "stage private file",
            input_text=payload,
        )
        run(["mv", "-fT", "--", str(staged), str(path)], "replace private file")
    except RuntimeError as exc:
        try:
            run(["rm", "-f", "--", str(staged)], "remove staged private file")
        except RuntimeError as cleanup_exc:
            raise RuntimeError(f"{exc}; {cleanup_exc}") from None
        raise


def stage_text(path: Path, payload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return Path(name)


def publish_pair_atomic(first: Path, first_text: str, second: Path, second_text: str) -> None:
    """Publish two files and restore the first if publishing the second fails."""
    publish_state_atomic([(first, first_text), (second, second_text)])


def publish_pair_and_remove_atomic(
    first: Path,
    first_text: str,
    second: Path,
    second_text: str,
    remove: Path,
) -> None:
    """Publish two files and remove one stale file, rolling back on publish/remove errors."""
    publish_state_atomic([(first, first_text), (second, second_text)], removes=[remove])


def publish_state_atomic(
    writes: list[tuple[Path, str]],
    *,
    removes: list[Path] | None = None,
) -> None:
    """Apply one onboarding state transition and restore every path on failure."""
    removals = removes or []
    paths = [path for path, _ in writes] + removals
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    staged: list[tuple[Path, Path]] = []
    try:
        staged = [(path, stage_text(path, text)) for path, text in writes]
        for path, temp in staged:
            os.replace(temp, path)
        for path in removals:
            path.unlink(missing_ok=True)
    except OSError:
        for path in reversed(paths):
            _restore_file(path, before[path])
        raise
    finally:
        for _, temp in staged:
            temp.unlink(missing_ok=True)


def _restore_file(path: Path, before: bytes | None) -> None:
    if before is None:
        path.unlink(missing_ok=True)
    else:
        restore = stage_text(path, before.decode("utf-8"))
        os.replace(restore, path)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle, held_lock(handle):
        yield


@contextlib.contextmanager
def held_lock(handle: IO[str]) -> Iterator[None]:
    """Hold an exclusive `flock` on an already open lock file for the block."""
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def try_file_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def directory_lock(path: Path) -> Iterator[None]:
    """Serialize a state-file transition without creating a lock artifact."""
    path.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def copy_tree(source: Path, destination: Path) -> None:
    try:
        paths = sorted(source.rglob("*"))
    except OSError as exc:
        raise RuntimeError(f"could not list source tree {source}: {exc}") from None
    for path in paths:
        if has_git_part(source, path):
            continue
        relative = path.relative_to(source)
        target = destination / relative
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"could not inspect source file {relative}: {exc}") from None
        if stat_module.S_ISLNK(mode):
            continue
        if stat_module.S_ISDIR(mode):
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(f"could not copy source directory {relative}: {exc}") from None
        elif stat_module.S_ISREG(mode):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"could not copy source file {relative}: {exc}") from None


def regular_files_under(root: Path, *, context: str) -> list[tuple[Path, os.stat_result]]:
    files: list[tuple[Path, os.stat_result]] = []
    try:
        paths = sorted(root.rglob("*"))
    except OSError as exc:
        raise RuntimeError(f"could not list {context} {root}: {exc}") from None
    for path in paths:
        if has_git_part(root, path):
            continue
        try:
            file_stat = path.lstat()
        except OSError as exc:
            relative = display_relative(root, path)
            raise RuntimeError(f"could not inspect {context} {relative}: {exc}") from None
        mode = file_stat.st_mode
        if stat_module.S_ISLNK(mode):
            continue
        if stat_module.S_ISREG(mode):
            files.append((path, file_stat))
    return files


def has_git_part(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return ".git" in parts


def display_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def publish_component_entries(
    staging: Path,
    destination: Path,
    entries: list[str],
    label: str,
    *,
    publish_last: str | None = None,
) -> None:
    """Replace component files, optionally removing one proof file before all others.

    A proof file such as a manifest must disappear before a reader can observe
    any replaced input, but must be the final new file readers can observe.
    """
    if publish_last is not None and publish_last not in entries:
        raise RuntimeError(f"could not publish {label}: publish-last entry is not being published")
    remove_entries = entries
    publish_entries = entries
    if publish_last is not None:
        other_entries = [entry for entry in entries if entry != publish_last]
        remove_entries = [publish_last, *other_entries]
        publish_entries = [*other_entries, publish_last]
    try:
        backup = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}-old-", suffix=".tmp", dir=destination.parent)
        )
    except OSError as exc:
        raise RuntimeError(f"could not publish {label}: {exc}") from None

    try:
        for entry in remove_entries:
            current = destination / entry
            if current.exists():
                os.replace(current, backup / entry)

        for entry in publish_entries:
            source = staging / entry
            target = destination / entry
            if source.exists():
                os.replace(source, target)

        shutil.rmtree(staging)
    except OSError as exc:
        try:
            restore_component_entries(destination, backup, entries)
        except OSError as restore_exc:
            raise RuntimeError(f"could not publish {label}: {exc}; rollback failed: {restore_exc}") from None
        raise RuntimeError(f"could not publish {label}: {exc}") from None
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def restore_component_entries(destination: Path, backup: Path, entries: list[str]) -> None:
    for entry in entries:
        current = destination / entry
        if current.exists():
            remove_path(current)
        saved = backup / entry
        if saved.exists():
            os.replace(saved, destination / entry)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def ensure_dir(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare {label}: {exc}") from None


def cleanup_staging_dir(staging_dir: Path | None) -> None:
    if staging_dir is not None:
        shutil.rmtree(staging_dir, ignore_errors=True)


def sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return sha256_stream(source)


def sha256_stream(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


REVISION_PREFIX = "sha256:"


def content_revision(digests: Mapping[str, str]) -> str:
    """The content revision of a set of named contents: sha256 over the sorted names and per-name sha256.

    The same set gives the same revision wherever it is computed, and any changed byte, added or
    removed name, or renamed entry changes it. It stands where a Git commit id used to, for every
    live-root writer that makes no commit (memory, knowledge, the secret store).
    """
    digest = hashlib.sha256()
    for name in sorted(digests):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(digests[name].encode("ascii"))
        digest.update(b"\n")
    return f"{REVISION_PREFIX}{digest.hexdigest()}"


def files_revision(root: Path, relatives: list[str] | tuple[str, ...]) -> str:
    """:func:`content_revision` of the files `relatives` (POSIX paths below `root`), by their bytes."""
    digests: dict[str, str] = {}
    for relative in relatives:
        try:
            digests[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(f"could not read {relative}: {exc}") from None
    return content_revision(digests)
