"""The PO service's durable input queue: messages not yet taken as turns, under ``<data_dir>/po-queue/``.

One file per input, ``<time_ns>-<counter>.json`` holding ``{session_id, text, request_id, source,
queued_at}`` and, for a dispatcher input, the ``card`` facts it carries beside its text (and, for an
operation card, the service's production rights ``note`` its prompt ends with), written to a temporary
name, fsynced, renamed into place and the directory fsynced, all before the submitter hears an
acknowledgement. The names sort in submission order, so FIFO per session
is "the oldest file naming that session".

The queue holds only what no turn row stands for yet. An input leaves it only after its turn exists in
the board store (`PoStore.claim_turn` under the input's request id), so a crash between the claim and
the removal leaves an input whose claim answers the same turn again, and the second hand-over removes it
without creating anything. An input that can never become a turn (its session is gone or closed, its
request id belongs to something else) is moved to ``refused/`` with the reason, never silently dropped.
Every write takes the installation's PO recovery target lock, the boundary a checkpoint capture holds
while it reads the PO SQL cut and this directory.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu._fsutil import BeneathRefused, fsync_directory, open_beneath
from ummanu.board.checkpoint_layout import unique_json_fields

QUEUE_DIR_NAME = "po-queue"
REFUSED_DIR_NAME = "refused"
SUFFIX = ".json"
# Who may submit an input (`PoService.submit`): the owner through the web, and the dispatcher handing
# a sprint's PO session a decision or operation card (`ummanu.dispatch.po_cards`).
SOURCES = ("web", "dispatcher")
DISPATCHER_SOURCE = "dispatcher"
# The service itself: the seeding message of a session its resolver opened (`PoService.sprint_session`).
SERVICE_SOURCE = "po-service"
PENDING = "pending"
REFUSED = "refused"
_DOCUMENT_FIELDS = ("session_id", "text", "request_id", "source", "queued_at")
_OPTIONAL_FIELDS = {"card": dict, "note": str, "metadata": dict}


class QueueError(RuntimeError):
    """The queue directory could not be read or written."""


def queue_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / QUEUE_DIR_NAME


def is_input_name(name: object) -> bool:
    """A name `pending` and `find_refused` read as an input; dot names are in-flight temporaries."""
    return (type(name) is str and name.endswith(SUFFIX) and not name.startswith(".")
            and "/" not in name and "\x00" not in name)


def validate_input_document(document: object, section: str) -> None:
    """Exactly the fields `put` writes, plus the `reason` `refuse` adds; metadata and card stay opaque."""
    refused = section == REFUSED
    if section not in (PENDING, REFUSED) or type(document) is not dict:
        raise QueueError("PO input is not a JSON object of a known section")
    allowed = {*_DOCUMENT_FIELDS, *_OPTIONAL_FIELDS, *(["reason"] if refused else [])}
    if not {*_DOCUMENT_FIELDS, *(["reason"] if refused else [])} <= set(document) <= allowed:
        raise QueueError("PO input fields are not the queue's")
    if any(type(document[field]) is not str for field in (*_DOCUMENT_FIELDS, *(["reason"] if refused else []))):
        raise QueueError("PO input text fields are not text")
    if document["source"] not in (*SOURCES, SERVICE_SOURCE):
        raise QueueError("PO input source is not one the queue accepts")
    if any(field in document and type(document[field]) is not kind for field, kind in _OPTIONAL_FIELDS.items()):
        raise QueueError("PO input card, note or metadata has the wrong type")


@dataclass(frozen=True)
class CapturedInput:
    """One input file as its owner wrote it: section, file name (its FIFO key) and whole document."""

    section: str
    name: str
    document: dict[str, Any]


@dataclass(frozen=True)
class QueueCapture:
    """Pending inputs then refused ones, each in name order; `present` is False for no queue directory."""

    present: bool
    inputs: tuple[CapturedInput, ...]


@dataclass(frozen=True)
class QueuedInput:
    name: str
    session_id: str
    text: str
    request_id: str
    source: str
    queued_at: str
    # The card facts of a dispatcher input (`PoService.submit`); part of what its request id binds.
    card: dict[str, Any] | None = None
    # Frozen service additions: selected new sprint comments and production rights.
    # The service's own, so not part of what the request id binds.
    note: str | None = None
    # Native display source/summary and accepted sprint comment position, transferred at claim.
    metadata: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        document = {
            "session_id": self.session_id,
            "text": self.text,
            "request_id": self.request_id,
            "source": self.source,
            "queued_at": self.queued_at,
        }
        if self.card is not None:
            document["card"] = self.card
        if self.note is not None:
            document["note"] = self.note
        if self.metadata is not None:
            document["metadata"] = self.metadata
        return document


def write_durably(path: Path, payload: str, *, mode: int = 0o600) -> None:
    """tmp + fsync + rename + directory fsync: after return the file survives a crash of this host."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        fsync_directory(path.parent)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class PoQueue:
    """The inputs of one installation, oldest first. Safe for the threads of one process."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.directory = queue_dir(data_dir)
        self._lock = threading.Lock()
        self._counter = 0

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """The cross-process boundary of every queue write and of a capture; reentrant per thread."""
        from ummanu.po.recovery_admission import RecoveryAdmissionError, recovery_target_lock

        try:
            with recovery_target_lock(self.data_dir):
                yield
        except RecoveryAdmissionError as exc:
            raise QueueError(f"the PO queue cannot take its mutation lock: {exc}") from None

    def ensure(self) -> None:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.directory / REFUSED_DIR_NAME).mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise QueueError(f"could not create the PO queue {self.directory}: {exc}") from None

    def put(
        self,
        *,
        session_id: str,
        text: str,
        request_id: str,
        source: str,
        card: dict[str, Any] | None = None,
        note: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> QueuedInput:
        """Write one input durably and return it; the caller acknowledges only after this returns."""
        if source not in (*SOURCES, SERVICE_SOURCE):
            raise ValueError(f"a PO input comes from {' or '.join(SOURCES)}, not {source!r}")
        self.ensure()
        with self._lock:
            self._counter += 1
            name = f"{time.time_ns():020d}-{os.getpid()}-{self._counter:06d}{SUFFIX}"
        queued = QueuedInput(
            name=name,
            session_id=session_id,
            text=text,
            request_id=request_id,
            source=source,
            queued_at=datetime.now(UTC).isoformat(),
            card=dict(card) if card is not None else None,
            note=note,
            metadata=dict(metadata) if metadata is not None else None,
        )
        with self.mutation():
            try:
                write_durably(self.directory / name, json.dumps(queued.document(), ensure_ascii=False))
            except OSError as exc:
                raise QueueError(f"could not queue the PO input: {exc}") from None
        return queued

    def pending(self, session_id: str | None = None) -> list[QueuedInput]:
        """Every queued input (of one session), oldest first. A file that cannot be read is skipped."""
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(self.directory)
                if entry.is_file() and entry.name.endswith(SUFFIX) and not entry.name.startswith(".")
            )
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise QueueError(f"could not read the PO queue {self.directory}: {exc}") from None
        found: list[QueuedInput] = []
        for name in names:
            item = self._read(name)
            if item is not None and (session_id is None or item.session_id == session_id):
                found.append(item)
        return found

    def heads(self) -> list[QueuedInput]:
        """The oldest input of every session that has one, in the order they were queued."""
        seen: set[str] = set()
        heads: list[QueuedInput] = []
        for item in self.pending():
            if item.session_id not in seen:
                seen.add(item.session_id)
                heads.append(item)
        return heads

    def find(self, request_id: str) -> QueuedInput | None:
        return next((item for item in self.pending() if item.request_id == request_id), None)

    def find_refused(self, request_id: str) -> dict[str, Any] | None:
        """The set-aside input (with its `reason`) that carried `request_id`, if any."""
        try:
            entries = sorted((self.directory / REFUSED_DIR_NAME).glob(f"*{SUFFIX}"))
        except OSError as exc:
            raise QueueError(f"could not read the set-aside PO inputs: {exc}") from None
        for entry in entries:
            try:
                document = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(document, dict) and document.get("request_id") == request_id:
                return document
        return None

    def remove(self, item: QueuedInput) -> None:
        """Drop an input whose turn exists in the store. Removing one already gone is not an error."""
        with self.mutation():
            try:
                (self.directory / item.name).unlink()
                fsync_directory(self.directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise QueueError(f"could not remove {item.name} from the PO queue: {exc}") from None

    def refuse(self, item: QueuedInput, reason: str) -> None:
        """Move an input that can never become a turn to ``refused/``, with why."""
        self.ensure()
        target = self.directory / REFUSED_DIR_NAME / item.name
        with self.mutation():
            try:
                write_durably(target, json.dumps({**item.document(), "reason": reason}, ensure_ascii=False))
            except OSError as exc:
                raise QueueError(f"could not set {item.name} aside: {exc}") from None
            self.remove(item)

    def capture(self) -> QueueCapture:
        """Every pending and refused input, strictly, inside `mutation`; for a backup, not for display.

        Entries are opened beneath trusted descriptors without following links. A regular file that
        is not an input name is not an input; any other entry, or an input that does not parse as
        the queue's own document, refuses the whole capture without quoting content.
        """
        with self.mutation():
            try:
                root = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            except OSError as exc:
                raise QueueError(f"PO queue capture cannot open the data directory: {exc.strerror}") from None
            try:
                queue = _open_capture(root, QUEUE_DIR_NAME, QUEUE_DIR_NAME, directory=True)
                if queue is None:
                    return QueueCapture(present=False, inputs=())
                try:
                    inputs = _capture_section(queue, PENDING)
                    refused = _open_capture(queue, REFUSED_DIR_NAME, f"{QUEUE_DIR_NAME}/{REFUSED_DIR_NAME}",
                                            directory=True)
                    if refused is not None:
                        try:
                            inputs += _capture_section(refused, REFUSED)
                        finally:
                            os.close(refused)
                finally:
                    os.close(queue)
            finally:
                os.close(root)
        return QueueCapture(present=True, inputs=tuple(inputs))

    def _read(self, name: str) -> QueuedInput | None:
        try:
            document = json.loads((self.directory / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        try:
            return QueuedInput(
                name=name,
                session_id=str(document["session_id"]),
                text=str(document["text"]),
                request_id=str(document["request_id"]),
                source=str(document.get("source") or "web"),
                queued_at=str(document.get("queued_at") or ""),
                card=document["card"] if isinstance(document.get("card"), dict) else None,
                note=document["note"] if isinstance(document.get("note"), str) else None,
                metadata=document["metadata"] if isinstance(document.get("metadata"), dict) else None,
            )
        except KeyError:
            return None


_REFUSALS = {"symlink": "a symlink", "directory": "not a plain directory", "regular": "not a regular file"}


def _open_capture(parent: int, name: str, shown: str, *, directory: bool) -> int | None:
    """One entry beneath `parent`, never followed; None only when it does not exist."""
    try:
        return open_beneath(parent, [name], want_directory=directory, missing_ok=True)
    except BeneathRefused as exc:
        raise QueueError(f"PO queue capture refuses {shown}: {_REFUSALS.get(exc.reason, exc.reason)}") from None
    except OSError as exc:
        raise QueueError(f"PO queue capture refuses {shown}: {exc.strerror or type(exc).__name__}") from None


def _capture_section(directory: int, section: str) -> list[CapturedInput]:
    """The inputs of one queue directory in name order, which is submission order."""
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise QueueError(f"PO queue capture cannot list {section} inputs: {exc.strerror}") from None
    captured: list[CapturedInput] = []
    for position, name in enumerate(names, 1):
        if section == PENDING and name == REFUSED_DIR_NAME:
            continue
        shown = f"{section} entry {position}"
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError as exc:
            raise QueueError(f"PO queue capture refuses {shown}: {exc.strerror}") from None
        if not stat.S_ISREG(info.st_mode):
            raise QueueError(f"PO queue capture refuses {shown}: not a regular file")
        if not is_input_name(name):
            continue
        handle = _open_capture(directory, name, shown, directory=False)
        if handle is None:
            raise QueueError(f"PO queue capture refuses {shown}: it vanished while the queue was held")
        try:
            chunks: list[bytes] = []
            while chunk := os.read(handle, 1 << 20):
                chunks.append(chunk)
        except OSError as exc:
            raise QueueError(f"PO queue capture refuses {shown}: {exc.strerror}") from None
        finally:
            os.close(handle)
        try:
            document = parse_input_document(b"".join(chunks).decode("utf-8"))
            validate_input_document(document, section)
        except (UnicodeDecodeError, ValueError, RecursionError, QueueError):
            raise QueueError(f"PO queue capture refuses {shown}: it is not a PO input document") from None
        captured.append(CapturedInput(section, name, document))
    return captured


def _refuse_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def parse_input_document(text: str) -> Any:
    """Strict JSON: a duplicate field or a non-finite number is not a document `put` wrote."""
    return json.loads(text, object_pairs_hook=unique_json_fields, parse_constant=_refuse_constant)


__all__ = [
    "DISPATCHER_SOURCE",
    "PENDING",
    "QUEUE_DIR_NAME",
    "REFUSED",
    "REFUSED_DIR_NAME",
    "SERVICE_SOURCE",
    "SOURCES",
    "CapturedInput",
    "PoQueue",
    "QueueCapture",
    "QueueError",
    "QueuedInput",
    "is_input_name",
    "parse_input_document",
    "queue_dir",
    "validate_input_document",
    "write_durably",
]
