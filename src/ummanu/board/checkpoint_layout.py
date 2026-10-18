"""The committed board checkpoint's on-disk layout, and the one reader of it.

Contract: docs/RECOVERY.md, section "Layout". The local export in the data directory keeps its flat
files (`cards.ndjson`, `sprints.ndjson`, `events.ndjson`, `audit.ndjson`, `export.json`); only what
the checkpoint writer publishes into `state/board` of the instance repository changes shape, so a
tick's Git cost follows what changed rather than the size of the board.

Two layouts are readable:

- **flat** (every checkpoint before secretary-1656): each logical file is one file in the directory.
- **split**, marked by `layout.json`: `cards.ndjson` and `sprints.ndjson` are stored one record per
  file (`cards/0000/00000000.json`, ...), so changing one card rewrites one small blob; the
  append-only `audit.ndjson` and `events.ndjson` are stored as immutable segments
  (`audit/0000/00000000.ndjson`, ...), and a checkpoint that appended lines adds one segment.
  `export.json` and the analytics manifest stay single files.

In both layouts a logical file's bytes are the concatenation of its parts in index order, so every
consumer of a committed checkpoint reads through :func:`open_checkpoint_board` and sees the same
bytes the flat export held. Git itself is never asked here; this module only reads and writes the
working tree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

LAYOUT_MARKER = "layout.json"
LAYOUT_SCHEMA = "ummanu.board.checkpoint-layout"
LAYOUT_VERSION = 2
FLAT = "flat"
SPLIT = "split"

#: The logical files of a board checkpoint, in either layout.
LOGICAL_FILES = (
    "cards.ndjson", "sprints.ndjson", "events.ndjson", "audit.ndjson", "export.json", "origin_returns.ndjson",
    "po_sql.json",
)
#: Logical files stored one record per file in the split layout: records change in place.
RECORD_FILES = {"cards.ndjson": ("cards", ".json"), "sprints.ndjson": ("sprints", ".json")}
#: Logical files stored as immutable segments in the split layout: they only ever grow.
SEGMENT_FILES = {"audit.ndjson": ("audit", ".ndjson"), "events.ndjson": ("events", ".ndjson")}
#: Logical files that stay single files in the split layout.
SINGLE_FILES = ("export.json", "origin_returns.ndjson", "po_sql.json")

# Parts are grouped a thousand to a directory so no single Git tree grows with the board.
_PER_DIRECTORY = 1000
#: Largest segment written, kept well under the 100 MB file limit of the instance remote.
MAX_SEGMENT_BYTES = 32 * 1024 * 1024
_CHUNK = re.compile(r"[0-9]{4}")


class CheckpointLayoutError(ValueError):
    """A committed board checkpoint whose layout cannot be read."""


def part_name(index: int, suffix: str) -> Path:
    return Path(f"{index // _PER_DIRECTORY:04d}") / f"{index:08d}{suffix}"


def layout_marker_text() -> str:
    return json.dumps({"schema": LAYOUT_SCHEMA, "version": LAYOUT_VERSION}, sort_keys=True) + "\n"


def _parts(root: Path, suffix: str) -> list[Path]:
    """The ordered parts under one split directory; a gap or a stray file is a broken layout."""
    if not root.exists():
        return []
    if not root.is_dir() or root.is_symlink():
        raise CheckpointLayoutError(f"{root}: split checkpoint part directory is not a directory")
    parts: list[Path] = []
    try:
        chunks = sorted(root.iterdir())
        for chunk in chunks:
            if not _CHUNK.fullmatch(chunk.name) or not chunk.is_dir() or chunk.is_symlink():
                raise CheckpointLayoutError(f"{chunk}: unexpected entry in split checkpoint directory")
            for part in sorted(chunk.iterdir()):
                index = _part_index(part, suffix)
                if index is None or not part.is_file() or part.is_symlink():
                    raise CheckpointLayoutError(f"{part}: unexpected entry in split checkpoint directory")
                if part.relative_to(root) != part_name(len(parts), suffix):
                    raise CheckpointLayoutError(f"{part}: split checkpoint part is out of sequence")
                parts.append(part)
    except OSError as exc:
        raise CheckpointLayoutError(f"{root}: could not list split checkpoint parts: {exc}") from None
    return parts


def _part_index(part: Path, suffix: str) -> int | None:
    if not part.name.endswith(suffix):
        return None
    stem = part.name[: -len(suffix)]
    return int(stem) if re.fullmatch(r"[0-9]{8}", stem) else None


@dataclass(frozen=True)
class CheckpointBoard:
    """One committed `state/board` directory, read the same way whichever layout wrote it."""

    directory: Path
    layout: str

    def path(self, name: str) -> Path:
        """Where a logical file lives, for diagnostics; a split record file names its directory."""
        if self.layout == SPLIT and name in RECORD_FILES:
            return self.directory / RECORD_FILES[name][0]
        if self.layout == SPLIT and name in SEGMENT_FILES:
            return self.directory / SEGMENT_FILES[name][0]
        return self.directory / name

    def has(self, name: str) -> bool:
        """Whether the logical file exists; split record and log files may have no parts."""
        self._check_name(name)
        if self.layout == SPLIT and name not in SINGLE_FILES:
            return True
        path = self.directory / name
        if path.is_symlink():
            raise CheckpointLayoutError(f"{path}: checkpoint file is a symlink")
        return path.is_file()

    def read_bytes(self, name: str) -> bytes:
        """The logical file's bytes. A logical file the checkpoint lacks raises FileNotFoundError."""
        self._check_name(name)
        try:
            if self.layout == SPLIT and name in RECORD_FILES:
                directory, suffix = RECORD_FILES[name]
                return b"".join(part.read_bytes() for part in _parts(self.directory / directory, suffix))
            if self.layout == SPLIT and name in SEGMENT_FILES:
                directory, suffix = SEGMENT_FILES[name]
                return b"".join(part.read_bytes() for part in _parts(self.directory / directory, suffix))
            path = self.directory / name
            if path.is_symlink():
                raise CheckpointLayoutError(f"{path}: checkpoint file is a symlink")
            return path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise CheckpointLayoutError(f"{self.path(name)}: could not read checkpoint file: {exc}") from None

    def read_text(self, name: str) -> str:
        try:
            return self.read_bytes(name).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CheckpointLayoutError(
                f"{self.path(name)}: could not decode checkpoint file: {exc}"
            ) from None

    def physical_entries(self) -> frozenset[str]:
        """The top-level names this layout may hold, besides the analytics seal and `.gitignore`."""
        if self.layout == FLAT:
            return frozenset(LOGICAL_FILES)
        return frozenset(
            {
                LAYOUT_MARKER,
                *SINGLE_FILES,
                *(directory for directory, _ in RECORD_FILES.values()),
                *(directory for directory, _ in SEGMENT_FILES.values()),
            }
        )

    @staticmethod
    def _check_name(name: str) -> None:
        if name not in LOGICAL_FILES:
            raise AssertionError(f"undeclared board checkpoint file {name}")


def open_checkpoint_board(directory: Path) -> CheckpointBoard:
    """The single reader of a committed board checkpoint, in the flat layout or the split one."""
    root = Path(directory)
    marker = root / LAYOUT_MARKER
    # Refused before the flat fallback: a symlinked marker, even a dangling one, is untrusted input,
    # never "no marker".
    if marker.is_symlink():
        raise CheckpointLayoutError(f"{marker}: checkpoint layout marker is a symlink")
    if not marker.exists():
        return CheckpointBoard(root, FLAT)
    if not marker.is_file():
        raise CheckpointLayoutError(f"{marker}: checkpoint layout marker is not a regular file")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CheckpointLayoutError(f"{marker}: could not parse checkpoint layout marker: {exc}") from None
    if not isinstance(payload, dict) or payload != {"schema": LAYOUT_SCHEMA, "version": LAYOUT_VERSION}:
        raise CheckpointLayoutError(f"{marker}: unknown checkpoint layout {payload!r}")
    return CheckpointBoard(root, SPLIT)


def read_export_summary(board: CheckpointBoard) -> dict:
    """Require export.json before interpreting coverage, including released v1/v2 absence."""
    try:
        summary = json.loads(board.read_text("export.json"))
    except (OSError, UnicodeError, ValueError):
        raise CheckpointLayoutError("board export.json is required and must be readable JSON") from None
    if not isinstance(summary, dict) or type(summary.get("version", 1)) is not int or summary.get("version", 1) not in (1, 2):
        raise CheckpointLayoutError("unsupported board export version")
    return summary


def publish_split_board(staging: Path, destination: Path) -> None:
    """Bring `destination` to the split layout of the flat logical files in `staging`.

    Only parts whose bytes differ are written, so an unchanged board touches no file and Git sees
    nothing to add. A record file is rewritten when its record changed; a log gains one new segment
    holding what was appended since the last checkpoint, and is rewritten from one segment only when
    the new log does not start with the committed one. The flat files of an older checkpoint leave
    the directory. The analytics seal is the caller's to remove first and publish last.
    """
    previous = _previous_logs(destination)
    _write_bytes_if_changed(destination / LAYOUT_MARKER, layout_marker_text().encode("utf-8"))
    for name, (directory, suffix) in RECORD_FILES.items():
        _sync_records(_read_staged(staging, name), destination / directory, suffix)
    for name, (directory, suffix) in SEGMENT_FILES.items():
        _sync_segments(_read_staged(staging, name), previous.get(name), destination / directory, suffix)
    for name in SINGLE_FILES:
        if name != "export.json" and not (staging / name).exists():
            stale = destination / name
            if stale.exists() or stale.is_symlink():
                _remove(stale)
        else:
            _write_bytes_if_changed(destination / name, _read_staged(staging, name))
    for name in (*RECORD_FILES, *SEGMENT_FILES):
        legacy = destination / name
        if legacy.exists() or legacy.is_symlink():
            _remove(legacy)


def _previous_logs(destination: Path) -> dict[str, bytes | None]:
    """The committed logs a new checkpoint may extend, read before anything is rewritten.

    A flat checkpoint's logs are extended by a first segment holding the whole file, so the
    conversion writes each log once; an unreadable split directory is rewritten from scratch.
    """
    try:
        board = open_checkpoint_board(destination)
    except CheckpointLayoutError:
        return {}
    if board.layout != SPLIT:
        return {}
    logs: dict[str, bytes | None] = {}
    for name in SEGMENT_FILES:
        try:
            logs[name] = board.read_bytes(name)
        except CheckpointLayoutError:
            logs[name] = None
    return logs


def _read_staged(staging: Path, name: str) -> bytes:
    try:
        return (staging / name).read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read staged board/{name}: {exc}") from None


def split_records(payload: bytes) -> list[bytes]:
    """Split NDJSON bytes into lines that keep their newline; joined they are `payload` again."""
    records: list[bytes] = []
    start = 0
    while start < len(payload):
        end = payload.find(b"\n", start)
        end = len(payload) if end < 0 else end + 1
        records.append(payload[start:end])
        start = end
    return records


def _sync_records(payload: bytes, root: Path, suffix: str) -> None:
    records = split_records(payload)
    for index, record in enumerate(records):
        _write_bytes_if_changed(root / part_name(index, suffix), record)
    _prune(root, suffix, keep=len(records))


def _sync_segments(payload: bytes, committed: bytes | None, root: Path, suffix: str) -> None:
    if committed is not None and payload.startswith(committed):
        count = len(_parts(root, suffix))
        appended = segment_chunks(payload[len(committed) :], MAX_SEGMENT_BYTES)
        for index, segment in enumerate(appended, start=count):
            _write_bytes_if_changed(root / part_name(index, suffix), segment)
        return
    # History that does not extend the committed log is not appended to: the log is rewritten from
    # the first segment, which costs its full blobs and is expected only after rewritten history.
    if root.exists():
        _remove(root)
    for index, segment in enumerate(segment_chunks(payload, MAX_SEGMENT_BYTES)):
        _write_bytes_if_changed(root / part_name(index, suffix), segment)


def segment_chunks(payload: bytes, limit: int) -> list[bytes]:
    """Cut NDJSON bytes at line ends into segments of at most `limit` bytes, unless one line is longer."""
    segments: list[bytes] = []
    current = bytearray()
    for record in split_records(payload):
        if current and len(current) + len(record) > limit:
            segments.append(bytes(current))
            current.clear()
        current += record
    if current:
        segments.append(bytes(current))
    return segments


def _prune(root: Path, suffix: str, *, keep: int) -> None:
    """Remove every entry under `root` that is not one of its first `keep` parts."""
    if not root.exists():
        return
    if not root.is_dir() or root.is_symlink():
        _remove(root)
        return
    wanted = {part_name(index, suffix) for index in range(keep)}
    wanted_chunks = {name.parent for name in wanted}
    try:
        for chunk in list(root.iterdir()):
            if Path(chunk.name) not in wanted_chunks or not chunk.is_dir() or chunk.is_symlink():
                _remove(chunk)
                continue
            for part in list(chunk.iterdir()):
                if part.relative_to(root) not in wanted:
                    _remove(part)
        if keep == 0:
            root.rmdir()
    except OSError as exc:
        raise RuntimeError(f"could not prune checkpoint parts under {root}: {exc}") from None


def _write_bytes_if_changed(path: Path, payload: bytes) -> None:
    try:
        if path.is_file() and not path.is_symlink() and path.read_bytes() == payload:
            return
    except OSError:
        pass
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temp_path, path)
        temp_path = None
    except OSError as exc:
        raise RuntimeError(f"could not write checkpoint file {path}: {exc}") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _remove(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        raise RuntimeError(f"could not remove stale checkpoint entry {path}: {exc}") from None
