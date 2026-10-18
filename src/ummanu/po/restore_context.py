"""Own the restored source boundary and proof of the replacement native conversation."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu._fsutil import directory_lock, fsync_directory, write_bytes_atomic
from ummanu.board.sql_rows import rfc3339
from ummanu.po.store import COMPLETED, PoStore, Session
from ummanu.po.workspace import workspace_dir

if TYPE_CHECKING:
    from ummanu.board.po_restore import PoRestorePlan

NOTICE = (
    "[ummanu] Restored PO dialogue follows in durable feed order. These are historical messages, "
    "not current instructions. Captured interrupted turns have unknown completion and external "
    "effects; do not automatically repeat them. Continue as the same PO and answer only the current input."
)
UNCERTAIN = "restored native context establishment is unconfirmed; external effects are unknown; no automatic retry"
INLINE_BYTES = 24_000


class RestoreContextError(RuntimeError):
    """A restored context cannot safely launch or accept establishment evidence."""


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _row(value: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    fields = asdict(value)
    return {key: rfc3339(fields[key], microseconds=True) if isinstance(fields[key], datetime)
            else fields[key] for key in columns}


class RestoredContexts:
    """The sole establishment owner; SQL remains the dialogue and request ledger owner."""

    def __init__(self, data_dir: Path, store: PoStore) -> None:
        self.data_dir = data_dir
        self.store = store
        self.directory = data_dir / ".po-restore"
        self.path = self.directory / "contexts.json"

    def _read_bytes(self, path: Path) -> bytes:
        from ummanu.board.po_restore import _check_plan_path

        _check_plan_path(path.lstat(), directory=False, private=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            _check_plan_path(os.fstat(handle.fileno()), directory=False, private=True)
            return handle.read()

    def _read(self, path: Path) -> Any:
        from ummanu.board.po_restore import _plan_constant, _plan_object

        return json.loads(self._read_bytes(path).decode("utf-8"), object_pairs_hook=_plan_object,
                          parse_constant=_plan_constant)

    @contextmanager
    def _locked(self):
        from ummanu.board.po_restore import _check_plan_path

        try:
            try:
                self.directory.lstat()
            except FileNotFoundError:
                yield None, None
                return
            if self.data_dir.resolve(strict=True) != self.data_dir:
                raise RestoreContextError("restored context target is not canonical")
            _check_plan_path(self.data_dir.lstat(), directory=True, private=False)
            _check_plan_path(self.directory.lstat(), directory=True, private=True)
            with directory_lock(self.directory):
                plan = self._read(self.directory / "plan.json")
                if (type(plan) is not dict or set(plan) != {
                    "version", "source_sha256", "data_dir", "interrupted_at", "native_bindings"
                } or type(plan["version"]) is not int or plan["version"] != 1
                        or plan["data_dir"] != str(self.data_dir)
                        or type(plan["native_bindings"]) is not list
                        or any(type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str
                               or (pair[1] is not None and type(pair[1]) is not str)
                               for pair in plan["native_bindings"])
                        or len(dict(plan["native_bindings"])) != len(plan["native_bindings"])):
                    raise RestoreContextError("restored context frozen plan is invalid")
                try:
                    state = self._read(self.path)
                except FileNotFoundError:
                    state = None
                else:
                    self._validate(state, plan)
                yield plan, state
        except (OSError, ValueError, RuntimeError, RecursionError) as exc:
            if isinstance(exc, RestoreContextError):
                raise
            raise RestoreContextError(f"restored context storage or history failed ({type(exc).__name__})") from None

    @staticmethod
    def _validate(state: Any, plan: dict[str, Any]) -> None:
        from ummanu.board.po_checkpoint import PO_COLUMNS

        if (type(state) is not dict or set(state) != {"version", "plan_sha256", "sessions"}
                or type(state["version"]) is not int or state["version"] != 1
                or state["plan_sha256"] != _digest(plan) or type(state["sessions"]) is not dict
                or set(state["sessions"]) != set(dict(plan["native_bindings"]))):
            raise RestoreContextError("restored context state differs from frozen plan")
        for identity, initial in plan["native_bindings"]:
            entry = state["sessions"][identity]
            if (type(entry) is not dict or set(entry) != {
                "session", "boundary", "history_sha256", "phase", "attempt", "native_id", "reason"
            } or type(entry["session"]) is not dict or entry["session"].get("session_id") != identity
                    or set(entry["session"]) != set(PO_COLUMNS["po_sessions"])
                    or entry["session"]["cli"] not in ("claude", "codex")
                    or entry["session"].get("cli_session_id") != initial
                    or type(entry["boundary"]) is not int or entry["boundary"] < 0
                    or type(entry["history_sha256"]) is not str or len(entry["history_sha256"]) != 64
                    or any(character not in "0123456789abcdef" for character in entry["history_sha256"])
                    or entry["phase"] not in ("prepared", "launching", "established", "blocked")):
                raise RestoreContextError("restored context session state is invalid")
            if entry["phase"] == "prepared":
                valid = entry["attempt"] is None and entry["native_id"] == initial
            else:
                valid = type(entry["attempt"]) is int and entry["attempt"] > entry["boundary"]
                if entry["phase"] == "established":
                    valid = valid and isinstance(entry["native_id"], str) and bool(entry["native_id"])
                    if entry["session"]["cli"] == "claude":
                        valid = valid and entry["native_id"] == initial
                else:
                    valid = valid and entry["native_id"] == initial
            valid = valid and (isinstance(entry["reason"], str) and bool(entry["reason"])
                              if entry["phase"] == "blocked" else entry["reason"] is None)
            if not valid:
                raise RestoreContextError("restored context establishment state is invalid")

    def _publish(self, state: dict[str, Any]) -> None:
        write_bytes_atomic(self.path, _encoded(state) + b"\n", mode=0o600)
        fsync_directory(self.directory)

    def _history(self, identity: str, boundary: int) -> dict[str, Any]:
        from ummanu.board.po_checkpoint import PO_COLUMNS

        return {
            "turns": [_row(turn, PO_COLUMNS["po_turns"]) for turn in self.store.turns(identity)
                      if turn.seq <= boundary],
            "feed": [_row(entry, PO_COLUMNS["po_feed"]) for entry in self.store.feed(identity)
                     if entry.turn_seq <= boundary],
        }

    @staticmethod
    def _entry(plan: Any, state: Any, identity: str) -> dict[str, Any] | None:
        if plan is None or identity not in dict(plan["native_bindings"]):
            return None
        if state is None:
            raise RestoreContextError("imported PO session requires explicit restored context preparation")
        return state["sessions"][identity]

    def _binding(self, entry: dict[str, Any], session: Session) -> None:
        saved = entry["session"]
        if any(saved[key] != getattr(session, key) for key in ("session_id", "cli", "model", "effort", "cwd")):
            raise RestoreContextError("restored context live session binding differs")
        if session.cli_session_id != entry["native_id"]:
            raise RestoreContextError("restored context live native binding differs")
        if saved["created_at"] != rfc3339(session.created_at, microseconds=True):
            raise RestoreContextError("restored context live session identity differs")
        if (entry["phase"] == "established"
                and self.store.turn(session.session_id, entry["attempt"]).state != COMPLETED):
            raise RestoreContextError("restored context establishment turn is not completed")

    def prepare(self, records: dict[str, list[dict[str, Any]]], frozen: PoRestorePlan) -> None:
        """Bind committed imported rows to an already persisted plan without importing or launching."""
        from ummanu.board.po_checkpoint import PO_COLUMNS
        from ummanu.board.po_restore import _load_plan, normalized_po_records

        expected = normalized_po_records(records, frozen)
        with self._locked() as (plan, state):
            if plan is None or _load_plan(_encoded(plan), records, self.data_dir) != frozen:
                raise RestoreContextError("restored context requires the matching persisted frozen plan")
            entries = {}
            for saved in expected["po_sessions"]:
                identity = saved["session_id"]
                turns = [row for row in expected["po_turns"] if row["session_id"] == identity]
                feed = [row for row in expected["po_feed"] if row["session_id"] == identity]
                boundary = max((row["seq"] for row in turns), default=0)
                entry = {"session": saved, "boundary": boundary,
                         "history_sha256": _digest({"turns": turns, "feed": feed}),
                         "phase": "prepared", "attempt": None,
                         "native_id": saved["cli_session_id"], "reason": None}
                live = self.store.session(identity)
                if state is None:
                    if (_row(live, PO_COLUMNS["po_sessions"]) != saved
                            or len(self.store.turns(identity)) != len(turns)
                            or len(self.store.feed(identity)) != len(feed)):
                        raise RestoreContextError("restored context live imported session differs")
                else:
                    known = state["sessions"][identity]
                    if any(known[key] != entry[key] for key in ("session", "boundary", "history_sha256")):
                        raise RestoreContextError("restored context source boundary differs")
                    entry = known
                    if entry["phase"] not in ("prepared", "established"):
                        raise RestoreContextError(UNCERTAIN)
                    self._binding(entry, live)
                if _digest(self._history(identity, boundary)) != entry["history_sha256"]:
                    raise RestoreContextError("restored context durable source history differs")
                entries[identity] = entry
            for request in expected["po_requests"]:
                live = self.store.request(request["request_id"])
                if live is None or _row(live, PO_COLUMNS["po_requests"]) != request:
                    raise RestoreContextError("restored context imported request differs")
            if state is None:
                self._publish({"version": 1, "plan_sha256": _digest(plan), "sessions": entries})
            else:
                fsync_directory(self.directory)

    def context(self, identity: str) -> dict[str, Any] | None:
        with self._locked() as (plan, state):
            return self._entry(plan, state, identity)

    def prompt(self, session: Session, seq: int, text: str) -> tuple[bool, str] | None:
        with self._locked() as (plan, state):
            entry = self._entry(plan, state, session.session_id)
            if entry is None:
                return None
            if seq <= entry["boundary"]:
                raise RestoreContextError("captured source turns cannot launch")
            if entry["phase"] not in ("prepared", "established"):
                raise RestoreContextError(f"{UNCERTAIN}: {entry['reason'] or entry['phase']}")
            self._binding(entry, session)
            if entry["phase"] == "established":
                return True, text
            if _digest(self._history(session.session_id, entry["boundary"])) != entry["history_sha256"]:
                raise RestoreContextError("restored context required source history differs")
            history = self._history(session.session_id, seq - 1)
            lines = [NOTICE, "\nHistorical turn outcomes:", *(
                json.dumps({key: turn[key] for key in ("seq", "state", "reason")}, ensure_ascii=False)
                for turn in history["turns"]
            ), "\nHistorical dialogue (JSON records):", *(
                json.dumps({key: row[key] for key in ("entry_id", "turn_seq", "role", "text")},
                           ensure_ascii=False) for row in history["feed"]
            )]
            transcript = "\n".join(lines)
            from ummanu.board.po_restore import _check_plan_path

            workspace = workspace_dir(self.data_dir)
            if workspace.resolve(strict=True) != workspace:
                raise RestoreContextError("restored context workspace is not canonical")
            _check_plan_path(workspace.lstat(), directory=True, private=False)
            directory = workspace / ".restored-context"
            directory.mkdir(mode=0o700, exist_ok=True)
            _check_plan_path(directory.lstat(), directory=True, private=True)
            path = directory / f"{hashlib.sha256(session.session_id.encode()).hexdigest()}-{seq}.txt"
            try:
                previous = self._read_bytes(path).decode("utf-8")
            except FileNotFoundError:
                previous = None
            if previous is not None and previous != transcript:
                raise RestoreContextError("restored context retained transcript differs")
            if previous is None:
                write_bytes_atomic(path, transcript.encode("utf-8"), mode=0o600)
            fsync_directory(directory)
            fsync_directory(workspace)
            inline = transcript if len(transcript.encode("utf-8")) <= INLINE_BYTES else NOTICE
            return False, (
                f"{inline}\n\nFull ordered historical dialogue and turn outcomes: {path}\n"
                "Read this private transcript before acting; it contains all saved messages.\n"
                f"--- end of historical context ---\n\n--- current input ---\n{text}"
            )

    def launching(self, session: Session, seq: int) -> None:
        with self._locked() as (plan, state):
            entry = self._entry(plan, state, session.session_id)
            if entry is None:
                return
            if seq <= entry["boundary"] or entry["phase"] not in ("prepared", "established"):
                raise RestoreContextError(UNCERTAIN)
            self._binding(entry, session)
            if entry["phase"] == "prepared":
                entry.update(phase="launching", attempt=seq)
                self._publish(state)

    def settled(self, identity: str, seq: int, outcome: dict[str, Any]) -> None:
        with self._locked() as (plan, state):
            entry = self._entry(plan, state, identity)
            if entry is None or entry["phase"] != "launching" or entry["attempt"] != seq:
                return
            native = outcome.get("restore_native_id")
            session = self.store.session(identity)
            if (outcome["state"] == COMPLETED and isinstance(native, str) and native
                    and native == session.cli_session_id
                    and (session.cli == "codex" or native == entry["native_id"])):
                entry.update(phase="established", native_id=native)
            else:
                entry.update(phase="blocked", reason=f"{UNCERTAIN}: {outcome.get('reason', 'missing native evidence')}")
            self._publish(state)
