"""Shared ownership fences and the single bounded cleanup replay allowance.

Board writes, recovery, journal publication and runtime effects use the same locks and context.
This owner has no dependency on the cleanup effect engine.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import functools
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ummanu.dispatch.types import HostError

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_held = threading.local()

#: The automatic replay's one deadline (ummanu-145), from before its selection until it returns:
#: admission, every wait and effect, and the outcome and cursor publications, below the sprint's 5 s
#: phase bound with room to return and report.
REPLAY_ALLOWANCE = 4.0
#: The last part of that deadline, kept for the journal publications that record what happened;
#: no work, wait or effect is admitted into it, and nothing extends the deadline itself.
PUBLICATION_RESERVE = 0.5
#: A due intent is reserved only while this much of the work allowance is left.
ATTEMPT_FLOOR = 1.0
#: A destructive workspace stage (cache, environment, Git removal, ref deletion) starts only with this
#: much left, so a stage cut short is the rare case its durable progress recovers, not the routine one.
EFFECT_FLOOR = 0.5
_LOCK_POLL = 0.01


class Deferred(HostError):
    """The automatic replay's allowance ran out: nothing past this point was attempted or proved."""


class Allowance:
    """One automatic replay's deadline on the stdlib monotonic clock.

    `remaining()` is what is left for work (selection, admission, waits, effects): the deadline less
    `reserve`. Journal publications read `remaining(publication=True)`, up to the deadline itself.
    Nothing nested restarts or extends either.
    """

    def __init__(self, seconds: float, *, reserve: float = PUBLICATION_RESERVE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds, self.reserve, self.clock = seconds, reserve, clock
        self.started = clock()
        self.deadline = self.started + seconds
        # Where the allowance first refused work, and how many times it did.
        self.deferred_at = ""
        self.deferrals = 0

    def remaining(self, *, publication: bool = False) -> float:
        return max(0.0, self.deadline - (0.0 if publication else self.reserve) - self.clock())

    def spent(self) -> float:
        return self.clock() - self.started

    def defer(self, what: str) -> Deferred:
        self.deferrals += 1
        self.deferred_at = self.deferred_at or what
        return Deferred("cleanup allowance exhausted at " + what + "; the intent stays pending and "
                        "its durable progress is replayed by a later attempt")

    def require(self, seconds: float, what: str) -> None:
        if self.remaining() < seconds:
            raise self.defer(what)

    def timeout(self, default: float, what: str) -> float:
        """The bound of one wait: the caller's own default, cut to what is left."""
        left = self.remaining()
        if left <= 0:
            raise self.defer(what)
        return min(default, left)


_ALLOWANCE: contextvars.ContextVar[Allowance | None] = contextvars.ContextVar("cleanup_allowance", default=None)


def _allowance() -> Allowance | None:
    """The allowance of the automatic replay this thread is serving; None for every other caller."""
    return _ALLOWANCE.get()


def _check(what: str) -> None:
    """Refuse to go on once the current allowance is spent; nothing outside an automatic replay."""
    allowance = _ALLOWANCE.get()
    if allowance is not None and allowance.remaining() <= 0:
        raise allowance.defer(what)


def _wait(what: str, acquire: Callable[[bool], bool], *, blocking: bool = True,
          publication: bool = False) -> None:
    """Acquire through `acquire(block)`: unbounded outside an automatic replay, and inside one polled
    for at most what is left of its allowance (none at all when not `blocking`)."""
    allowance = _ALLOWANCE.get()
    if allowance is None and blocking:
        acquire(True)
        return
    while not acquire(False):
        if not blocking:
            # Another owner's turn, not a spent allowance: nothing is marked deferred.
            raise Deferred("cleanup " + what + " is busy")
        assert allowance is not None
        left = allowance.remaining(publication=publication)
        if left <= 0:
            raise allowance.defer(what)
        time.sleep(min(_LOCK_POLL, left))


def _flocker(handle: Any, mode: int) -> Callable[[bool], bool]:
    def acquire(block: bool) -> bool:
        try:
            fcntl.flock(handle, mode if block else mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    return acquire


@contextlib.contextmanager
def ownership_lock(data_dir: Path) -> Iterator[None]:
    """Short journal/ownership mutations; never host work or a dispatcher tick."""
    with _path_lock(Path(data_dir).resolve() / "dispatcher" / "cleanup.lock", publication=True):
        yield


@contextlib.contextmanager
def _path_lock(target: Path, *, blocking: bool = True, publication: bool = False) -> Iterator[None]:
    """Reentrant in one thread, exclusive across threads and installed processes.

    Inside an automatic replay the wait is bounded by its allowance (see `_wait`).
    """
    path = str(target)
    with _guard:
        lock = _locks.setdefault(path, threading.RLock())
    _wait(path, lambda block: lock.acquire(blocking=block), blocking=blocking, publication=publication)
    try:
        held = getattr(_held, "paths", set())
        if path in held:
            yield
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+") as handle:
            _wait(path, _flocker(handle, fcntl.LOCK_EX), blocking=blocking, publication=publication)
            _held.paths = held | {path}
            try:
                yield
            finally:
                _held.paths = held
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        lock.release()


@contextlib.contextmanager
def bulk_lane(data_dir: Path, *, exclusive: bool = False) -> Iterator[None]:
    """One descriptor orders bulk recovery against concurrent per-card effects.

    Shared holders use independent file descriptions, so threads on different
    cards do not serialize. Nested callers reuse this thread's current mode.
    A shared holder cannot silently upgrade and authorize an unfenced bulk write.
    """
    path = str(Path(data_dir).resolve() / "dispatcher" / "board-bulk.lock")
    held = getattr(_held, "bulk_lanes", {})
    if path in held:
        if exclusive and not held[path]:
            raise HostError("bulk ownership write cannot upgrade a shared lane")
        yield
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        _wait(path, _flocker(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH))
        _held.bulk_lanes = {**held, path: exclusive}
        try:
            yield
        finally:
            _held.bulk_lanes = held
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def reference_lock(data_dir: Path, reference: str, *, lane: str = "effects",
                   blocking: bool = True) -> Iterator[None]:
    """Separate long lifecycle work from the short board/cleanup effect fence.

    Board writers never take the lifecycle lane. The effects lane orders state or
    claim changes against actual workspace disposal and the launch syscall. A caller
    that is not `blocking` takes the lane only if no one else holds it (`Deferred`).
    """
    root = Path(data_dir).resolve()
    with bulk_lane(root) if lane == "effects" else contextlib.nullcontext():
        if (lane in {"effects", "admission"}
                and getattr(_held, "bulk_lanes", {}).get(str(root / "dispatcher" / "board-bulk.lock"))):
            # The exclusive bulk owner already excludes every per-card writer.
            # Reconciliation through their normal helpers needs no extra files.
            yield
            return
        token = hashlib.sha256(reference.encode()).hexdigest()
        with _path_lock(root / "dispatcher" / lane / (token + ".lock"), blocking=blocking):
            yield


def lifecycle(method):
    """One host/cleanup operation per reference, with no board writer behind it."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        subject = args[0] if args else kwargs.get("task", kwargs.get("sprint"))
        reference = (subject.get("ref") if isinstance(subject, dict)
                     else getattr(subject, "sprint", "") or getattr(subject, "worker", ""))
        with reference_lock(self.data_dir, str(reference), lane="lifecycle"):
            return method(self, *args, **kwargs)
    return wrapped


def capacity_serialized(method):
    """Only admission/state transactions, never cleanup proofs or host calls."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        # Always take the card fence first. A slow disposal of one card must not
        # monopolize installation capacity while a transition waits for it.
        with reference_lock(self.data_dir, kwargs["reference"]), \
                reference_lock(self.data_dir, "capacity", lane="admission"), self._mutation():
            return method(self, *args, **kwargs)
    return wrapped


def owner_operation(method):
    """Protect an owner's transient manifest context, without blocking board writers.

    Inside an automatic replay the wait for another operation of the same owner (an inventory or a
    targeted replay under its own policy) is bounded by the allowance (`_wait`); a refusal is
    `Deferred` before anything is read, reserved or written. Nested use is reentrant as before.
    """
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        _wait("cleanup owner operation", lambda block: self._operations.acquire(blocking=block))
        try:
            return method(self, *args, **kwargs)
        finally:
            self._operations.release()
    return wrapped


def ownership_recovery(method):
    """A legacy pending occurrence gets the same fences as its original write."""
    @functools.wraps(method)
    def wrapped(self, event, *args, **kwargs):
        with reference_lock(self.data_dir, str(event["ref"])), \
                reference_lock(self.data_dir, "capacity", lane="admission"), self._mutation():
            return method(self, event, *args, **kwargs)
    return wrapped


def serialized(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with ownership_lock(self.data_dir):
            return method(self, *args, **kwargs)
    return wrapped


def journal_mutation(method):
    """One producer's read/modify/publication, with thread-local replay targeting.

    The per-intent layout is published (migrating a v1 journal) before the producer reads, so a
    mutation never compares a v1 value with a migrated one.
    """
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with ownership_lock(self.data_dir):
            self._ensure()
            return method(self, *args, **kwargs)
    return wrapped


