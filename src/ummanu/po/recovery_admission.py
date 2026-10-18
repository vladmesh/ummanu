"""Serialize PO ingress with durable recovery admission on one target."""

from __future__ import annotations

import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu._fsutil import directory_lock, fsync_directory, write_bytes_atomic
from ummanu.po.restore_context import RestoreContextError, RestoredContexts, _encoded

if TYPE_CHECKING:
    from ummanu.board.po_restore import PoRestorePlan
    from ummanu.po.store import PoStore

_HELD = threading.local()


class RecoveryAdmissionError(RestoreContextError):
    """A recovery phase or damaged authority refuses mutation before its SQL claim."""

    def __init__(self, message: str, *, code: str = "recovery_hold") -> None:
        super().__init__(message)
        self.code = code
        self.retryable = code != "recovery_invalid"


def _invalid() -> RecoveryAdmissionError:
    return RecoveryAdmissionError(
        "PO recovery authority is incomplete or invalid; repair recovery authority before retrying",
        code="recovery_invalid",
    )


@contextmanager
def recovery_target_lock(data_dir: Path):
    """Hold the native target lock reentrantly before any PO runner or context lock."""
    from ummanu.board.po_restore import _check_plan_path

    key = (os.getpid(), str(data_dir.absolute()))
    held = vars(_HELD).setdefault("targets", set())
    if key in held:
        yield
        return
    with ExitStack() as stack:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            _check_plan_path(data_dir.lstat(), directory=True, private=False)
            stack.enter_context(directory_lock(data_dir))
        except (OSError, ValueError, RuntimeError):
            raise _invalid() from None
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


class PoRecoveryAdmission:
    """Own begin/finish and every PO mutation's cross-process admission boundary."""

    def __init__(self, data_dir: Path, store: PoStore) -> None:
        self.data_dir = data_dir
        self.path = data_dir / ".po-recovery-admission.json"
        self.store = store
        self.contexts = RestoredContexts(data_dir, store)

    def locked(self):
        """Serialize a coordinator or already admitted transition without reopening admission."""
        return recovery_target_lock(self.data_dir)

    def _state(self) -> dict[str, Any] | None:
        try:
            try:
                self.path.lstat()
            except FileNotFoundError:
                try:
                    self.contexts.directory.lstat()
                except FileNotFoundError:
                    return None
                raise _invalid()
            if not self.data_dir.is_absolute() or self.data_dir.resolve(strict=True) != self.data_dir:
                raise _invalid()
            state = self.contexts._read(self.path)
            if (type(state) is not dict or set(state) != {
                "version", "source_sha256", "data_dir", "phase", "plan_sha256"
            } or type(state["version"]) is not int or state["version"] != 1
                    or state["data_dir"] != str(self.data_dir)
                    or state["phase"] not in ("holding", "finished")):
                raise _invalid()
            digests = [state["source_sha256"]]
            if state["phase"] == "holding":
                if state["plan_sha256"] is not None:
                    raise _invalid()
            else:
                digests.append(state["plan_sha256"])
            if any(type(value) is not str or len(value) != 64
                   or any(char not in "0123456789abcdef" for char in value) for value in digests):
                raise _invalid()
            if state["phase"] == "finished":
                self.contexts.authority(state["plan_sha256"], state["source_sha256"])
            return state
        except (OSError, ValueError, RuntimeError, RecursionError):
            raise _invalid() from None

    def _publish(self, state: dict[str, Any]) -> None:
        try:
            write_bytes_atomic(self.path, _encoded(state) + b"\n", mode=0o600)
            fsync_directory(self.data_dir)
        except (OSError, RuntimeError):
            raise _invalid() from None

    @contextmanager
    def mutation(self):
        """Keep the admission check and all writes/spawn in one serialized boundary."""
        with self.locked():
            state = self._state()
            if state is not None and state["phase"] == "holding":
                raise RecoveryAdmissionError(
                    "PO recovery is holding mutations; confirm import commit/parity, prepare the full "
                    "context cohort and finish recovery admission, then retry the same request"
                )
            yield

    def begin(self, records: dict[str, list[dict[str, Any]]]) -> str:
        """Close admission before plan/import; retry only the same captured source and target."""
        from ummanu.board.po_checkpoint import validate_po_records
        from ummanu.board.po_restore import _digest

        validate_po_records(records)
        with self.locked():
            if not self.data_dir.is_absolute() or self.data_dir.resolve(strict=True) != self.data_dir:
                raise _invalid()
            state = self._state()
            if state is not None and state["source_sha256"] != _digest(records):
                raise _invalid()
            if (state is None or state["phase"] == "holding") and self.store.running_turns():
                raise RecoveryAdmissionError(
                    "PO recovery cannot begin while turns are running; drain existing work and retry",
                    code="recovery_busy",
                )
            if state is not None:
                fsync_directory(self.data_dir)
                return state["phase"]
            self._publish({"version": 1, "source_sha256": _digest(records),
                           "data_dir": str(self.data_dir), "phase": "holding", "plan_sha256": None})
            return "holding"

    def status(self) -> dict[str, Any] | None:
        """Describe the recovery hold without exposing source records or target paths."""
        try:
            with self.mutation():
                return None
        except RecoveryAdmissionError as exc:
            return {"reason": exc.code, "detail": str(exc), "retryable": exc.retryable}

    def finish(self, records: dict[str, list[dict[str, Any]]], plan: PoRestorePlan,
               *, import_committed: bool, parity_verified: bool) -> None:
        """Open only after caller commit/parity confirmation and full live cohort preparation proof."""
        from ummanu.board.po_restore import _digest, normalized_po_records

        with self.locked():
            state = self._state()
            if (state is None or state["source_sha256"] != _digest(records)
                    or plan.data_dir != str(self.data_dir) or plan.source_sha256 != state["source_sha256"]):
                raise _invalid()
            normalized_po_records(records, plan)
            plan_sha256 = _digest({"version": 1, **asdict(plan)})
            if import_committed is not True or parity_verified is not True:
                raise RecoveryAdmissionError("PO recovery finish requires caller-confirmed SQL import commit and parity")
            if state["phase"] == "finished":
                if state["plan_sha256"] != plan_sha256:
                    raise _invalid()
                fsync_directory(self.data_dir)
                return
            if self.store.running_turns():
                raise RecoveryAdmissionError("PO recovery finish requires no running turns")
            self.contexts.prepare(records, plan, require_prepared=True)
            self._publish({**state, "phase": "finished", "plan_sha256": plan_sha256})
