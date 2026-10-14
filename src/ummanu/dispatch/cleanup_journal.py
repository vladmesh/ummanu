"""Durable cleanup obligations, layout and replay eligibility below the effect engine.

Board/tasks/sprints request settlement here. Only CleanupOwner in dispatch.cleanup performs it.
Compaction reads the existing typed HeadRun evidence policy, preserving malformed evidence.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ummanu.dispatch.ownership import Deferred, _allowance, _check, journal_mutation, serialized
from ummanu.dispatch.tick_telemetry import tick_count
from ummanu.dispatch.types import HostError

# Any of these naming a value means a head may have run for the attempt.
_HEAD_FIELDS = ("head", "review_head", "handle", "review_handle", "leaf", "worker_leaf", "review_leaf",
                "pid_file", "worker_pid_file", "review_pid_file",
                "head_run", "worker_head_run", "review_head_run")

def _merge_head(heads: list[dict[str, Any]], raw: dict[str, Any]) -> None:
    """Keep one entry per (run_id, scope_generation): the latest view replaces it.

    A confirmed exit is never replaced by an older, still-running view of that run.
    """
    key = (raw.get("run_id"), raw.get("scope_generation") or "")
    for index, old in enumerate(heads):
        if (old.get("run_id"), old.get("scope_generation") or "") == key:
            if old.get("lifecycle") != "exited" or raw.get("lifecycle") == "exited":
                heads[index] = copy.deepcopy(raw)
            return
    heads.append(copy.deepcopy(raw))


def _compact_heads(heads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for raw in heads:
        _merge_head(compact, raw)
    return compact

#: An open obligation (pending, or preserved without a terminal outcome) is attempted at most once
#: per this many seconds, by every entrypoint, across restarts and concurrent owners.
RETRY_COOLDOWN = 3600.0
#: Final outcomes decided from current facts: dead ends or a proved transfer of the old obligation.
TERMINAL_KINDS = ("workspace-disappeared", "registration-without-directory", "project-unregistered", "follows",
                  "attempt-replaced")


def _terminal(intent: dict[str, Any]) -> dict[str, Any] | None:
    """The intent's terminal outcome, if it has a well-formed one; anything else is no outcome."""
    terminal = intent["progress"].get("terminal")
    if (intent["status"] == "preserved" and isinstance(terminal, dict)
            and terminal.get("kind") in TERMINAL_KINDS):
        return terminal
    return None


def _next_attempt(intent: dict[str, Any]) -> float | None:
    """The stored due time, or None when there is none this clock could have written."""
    retry = intent.get("retry")
    due = retry.get("next_attempt_at") if isinstance(retry, dict) else None
    if isinstance(due, bool) or not isinstance(due, (int, float)):
        return None
    try:
        # Total on any JSON number: an integer beyond float range is malformed, not a crash.
        value = float(due)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _attempt_due(intent: dict[str, Any], now: float) -> bool:
    """The one replay eligibility policy, for automatic replay, replay_one and targeted replay.

    Only an open obligation is due: owned, completed and terminal intents never are. With no
    stored due time (a legacy intent, or a malformed value) the first attempt is now. A due time
    more than one cooldown ahead of `now` cannot have been written by this clock (it moved back, or
    the value is hostile): it is distrusted and the obligation is due, so one early attempt
    re-anchors it and nothing is hidden forever.
    """
    if intent["status"] not in {"pending", "preserved"} or _terminal(intent) is not None:
        return False
    due = _next_attempt(intent)
    return due is None or due <= now or due > now + RETRY_COOLDOWN


def _begin_attempt(intent: dict[str, Any]) -> None:
    """Current refusal revokes admission's settlement signal before any effect of an attempt."""
    intent["status"] = "pending"
    intent["reason"] = ""
    # Retained receipts survive, but current refusal must revoke admission's
    # settlement signal rather than exposing a prior successful observation.
    intent["progress"]["heads_stopped"] = False
    intent["progress"]["preservation_verified"] = False
    intent["progress"].pop("awaits_cards", None)
    intent["progress"].pop("terminal", None)

def _json_names(directory: Path, what: str) -> list[str]:
    """The stems of `directory`'s `*.json` files, sorted, read entry by entry from the native
    iterator with the current allowance checked before each. A scan cut short raises `Deferred`:
    no caller ever takes a partial listing for the whole directory. A missing directory has none."""
    names: list[str] = []
    try:
        scan = os.scandir(directory)
    except FileNotFoundError:
        return names
    with scan:
        for entry in scan:
            _check(what)
            if entry.name.endswith(".json"):
                names.append(entry.name[:-len(".json")])
    return sorted(names)

#: Every active file of the journal (intents, generated buckets, meta) stays under this bound,
#: checked at the one publication seam; the archived v1 document does not.
INTENT_FILE_LIMIT = 1_000_000
_LAYOUT_VERSION = 2
_HEX = "0123456789abcdef"
_MAX_GENERATED_DEPTH = 64
# The fan-out attestation a cleanup head keeps: its verdict, the binding a HeadRun read checks,
# and the first events. The provider source (with its session baseline), progress source and
# prompt identity are launch telemetry that no stop, fence or settlement reads.
_FANOUT_KEPT = ("version", "state", "terminal_state", "reason", "run_id", "role", "model", "binary_path",
                "binary_digest", "cli_version", "tool_schema_digest", "provider_schema_verdict", "event_count")
_FANOUT_EVENTS_KEPT = 8
_HEAD_RUN_FIELDS = ("head_run", "worker_head_run", "review_head_run")
# The dispatcher record fields CleanupOwner, admission, stop and settlement read: attempt and
# workspace ownership, every head field that proves a head may have run, the launch intent's run
# and the observer generation/launch order. Everything else in a record is its own telemetry.
_RECORD_KEPT = ("attempt_id", "worker", "workspace", "sprint", "generation", "launches", "launched_at",
                "head_possible", "launch_intent", *_HEAD_FIELDS)


def _compact_run(raw: Any) -> Any:
    """A cleanup copy of one persisted HeadRun: identity, lifecycle and stop receipt intact."""
    if not isinstance(raw, dict) or not isinstance(raw.get("fanout_policy"), dict):
        return raw
    from ummanu.runtime.head import HeadRun, HeadRunError, TaskRefError
    try:
        # The verdict as a HeadRun read sees it, so dropping the source can never upgrade it.
        policy = HeadRun.from_json(raw).fanout_policy
    except (HeadRunError, TaskRefError):
        policy = raw["fanout_policy"]
    compact = {name: policy[name] for name in _FANOUT_KEPT if name in policy}
    if isinstance(compact.get("reason"), str):
        compact["reason"] = compact["reason"][:500]
    events = policy.get("events")
    if isinstance(events, list):
        # A non-empty log stays non-empty, so a read never upgrades it to a clean policy.
        compact["events"] = events[:_FANOUT_EVENTS_KEPT]
        if len(events) > _FANOUT_EVENTS_KEPT:
            compact["event_count"] = len(events)
    elif "events" in policy:
        compact["events"] = events
    return {**raw, "fanout_policy": compact}


def _compact_record_runs(record: dict[str, Any]) -> dict[str, Any]:
    record = dict(record)
    for field in _HEAD_RUN_FIELDS:
        if isinstance(record.get(field), dict):
            record[field] = _compact_run(record[field])
    launch = record.get("launch_intent")
    if isinstance(launch, dict) and isinstance(launch.get("head_run"), dict):
        record["launch_intent"] = {**launch, "head_run": _compact_run(launch["head_run"])}
    return record


def cleanup_record(record: dict[str, Any]) -> dict[str, Any]:
    """The projection of a dispatcher record an obligation keeps (see `_RECORD_KEPT`)."""
    kept = {field: record[field] for field in _RECORD_KEPT if field in record}
    launch = kept.get("launch_intent")
    if isinstance(launch, dict):
        kept["launch_intent"] = {"head_run": launch["head_run"]} if "head_run" in launch else {}
    # Compacted before the copy, so the dropped launch telemetry is never copied.
    return copy.deepcopy(_compact_record_runs(kept))


def _compact_intent(intent: dict[str, Any]) -> None:
    """In place, so the caller's copy and the stored one compare equal afterwards."""
    # Compacts lists that grew before heads were keyed by run and generation.
    intent["heads"] = _compact_heads([_compact_run(head) for head in intent["heads"]])
    intent["record"] = _compact_record_runs(intent["record"])


def _generated_directory(root: Path, depth: int) -> Path:
    return root / ("generated" if depth == 1 else f"generated-{depth}")


def _generated_buckets(generated: dict[str, str], depth: int) -> dict[str, bytes]:
    """Every path -> digest entry, in buckets named by the first `depth` hex digits of its path's hash."""
    buckets: dict[str, dict[str, str]] = {}
    for name, digest in generated.items():
        buckets.setdefault(hashlib.sha256(name.encode()).hexdigest()[:depth], {})[name] = digest
    return {prefix: json.dumps(bucket, sort_keys=True).encode() for prefix, bucket in buckets.items()}


def _generated_layout(generated: dict[str, str], depth: int) -> tuple[int, dict[str, bytes]]:
    """The shallowest depth from `depth` at which every bucket fits the file bound, with its buckets.

    Every entry is kept: a map that cannot fit at any depth is refused before anything is written.
    """
    while depth <= _MAX_GENERATED_DEPTH:
        buckets = _generated_buckets(generated, depth)
        if all(len(body) <= INTENT_FILE_LIMIT for body in buckets.values()):
            return depth, buckets
        depth += 1
    raise HostError("cleanup generated digests cannot be stored under the "
                    f"{INTENT_FILE_LIMIT}-byte file bound; nothing was published")


def _replace_file(path: Path, body: bytes) -> None:
    """Atomic replace with the file and its directory entry durable before return."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".cleanup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _fsync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _valid_intent(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("task"), dict)
            and isinstance(value.get("record"), dict) and isinstance(value.get("heads"), list)
            and isinstance(value.get("progress"), dict) and isinstance(value.get("status"), str)
            and isinstance(value.get("disposition"), str))


class CleanupJournal:
    """One file per obligation under dispatcher/cleanup/, each atomically replaced with fsync.

    `cleanup/meta.json` holds the replay cursor and the generated depth d; the generated-file digests
    live in buckets named by the first d hex digits of each path's hash (`cleanup/generated/` at
    d=1, `cleanup/generated-<d>/` deeper); `cleanup/intents/<key>.json` holds one intent. A bucket
    that would outgrow the file bound deepens the whole map: it is rebuilt at the next depth, the
    meta switch publishes it and only then is the old depth removed. A mutation reads and replaces only
    its own intent file; inventory and replay walk the directory. The directory is published by one
    rename, so it exists only complete. The released v1 `cleanup.json` is migrated into it once,
    under the ownership lock, and then archived beside it; the archive is never read again.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        root = self.data_dir / "dispatcher"
        self.path = root / "cleanup"
        self.legacy = root / "cleanup.json"
        self.archive = root / "cleanup.v1-archive.json"
        self._staging = root / ".cleanup-migrating"
        self._targets: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
            "cleanup_targets", default=None)
        # Successful replacements made through this instance, by file class, and the bytes they wrote.
        self.writes: dict[str, int] = {"intent": 0, "meta": 0, "generated": 0, "bytes": 0}
        # What `due_keys` last read of each intent file, by the file's stat identity, and how many
        # intent files its last scan found.
        self._due: dict[str, tuple[tuple[int, ...], dict[str, Any]]] = {}
        self.selection_size = 0

    # Layout ------------------------------------------------------------------------------------

    def _intent_path(self, key: str, root: Path | None = None) -> Path:
        if len(key) != 64 or any(character not in _HEX for character in key):
            raise HostError("cleanup intent key is malformed")
        return (root or self.path) / "intents" / (key + ".json")

    def _generated_depth(self) -> int:
        depth = self._meta().get("generated_depth", 1)
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= _MAX_GENERATED_DEPTH:
            raise HostError("cleanup evidence has an unsupported shape: generated depth")
        return depth

    def _published(self) -> bool:
        if self.path.is_dir():
            return True
        if os.path.lexists(self.path):
            raise HostError("cleanup evidence unreadable: " + str(self.path) + " is not a journal directory")
        return False

    @staticmethod
    def _load(path: Path) -> Any:
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise HostError(f"cleanup evidence unreadable: {exc}") from exc

    def _read_legacy(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.legacy.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise HostError(f"cleanup evidence unreadable: {exc}") from exc
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("intents"), dict)
                or not isinstance(value.get("generated"), dict)):
            raise HostError("cleanup evidence has an unsupported shape")
        return value

    def _meta(self) -> dict[str, Any]:
        meta = self._load(self.path / "meta.json")
        if not isinstance(meta, dict) or meta.get("version") != _LAYOUT_VERSION:
            raise HostError("cleanup evidence has an unsupported shape")
        return meta

    # Reads -------------------------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """The whole journal as one value, for inventory, replay selection and whole-journal proofs."""
        if not self._published():
            legacy = self._read_legacy()
            if legacy is not None:
                return legacy
            # A concurrent migration may have published the layout and archived the file meanwhile.
            if not self._published():
                return {"version": _LAYOUT_VERSION, "intents": {}, "generated": {}, "replay_cursor": ""}
        meta = self._meta()
        intents = {}
        for name in _json_names(self.path / "intents", "cleanup journal read"):
            _check("cleanup journal read")
            file = self.path / "intents" / (name + ".json")
            intent = self._load(file)
            if not _valid_intent(intent):
                raise HostError("cleanup evidence has an unsupported shape: " + file.name)
            intents[file.stem] = intent
        return {"version": _LAYOUT_VERSION, "intents": intents, "generated": self.generated_digests(),
                "replay_cursor": str(meta.get("replay_cursor") or "")}

    def load_intent(self, key: str) -> dict[str, Any] | None:
        """One intent, reading only its own file (or the unmigrated v1 document)."""
        if not self._published():
            legacy = self._read_legacy()
            if legacy is not None or not self._published():
                return copy.deepcopy((legacy or {"intents": {}})["intents"].get(key))
        path = self._intent_path(key)
        if not path.exists():
            return None
        intent = self._load(path)
        if not _valid_intent(intent):
            raise HostError("cleanup evidence has an unsupported shape: " + path.name)
        return intent

    def intent(self, key: str) -> dict[str, Any]:
        intent = self.load_intent(key)
        if intent is None:
            raise KeyError(key)
        return intent

    def due_keys(self, now: float, cursor: str = "", read: list[str] | None = None) -> Iterator[str]:
        """The automatic replay's selection: the keys `_attempt_due` admits at `now`, in rotation
        order from `cursor`, yielded as they are read, every key read appended to `read`.

        The caller pulls only as many as it can attempt, and the allowance is checked before each
        read: what was read is always a prefix of the full rotation, and the next invocation goes on
        from where this one got (`replay`), so slow reads never starve the attempts. Only a file
        replaced since this instance last read it (any producer's replacement changes its stat
        identity, as `intent_state`) is read again, and only its eligibility fields are kept.
        """
        read = [] if read is None else read
        if not self._published():
            keys = sorted(key for key, intent in self.read()["intents"].items() if _attempt_due(intent, now))
            for key in [key for key in keys if key > cursor] + [key for key in keys if key <= cursor]:
                read.append(key)
                yield key
            return
        names = _json_names(self.path / "intents", "intent selection")
        self.selection_size = len(names)
        for key in [key for key in names if key > cursor] + [key for key in names if key <= cursor]:
            try:
                _check("intent selection")
            except Deferred:
                return
            try:
                stat = (self.path / "intents" / (key + ".json")).stat()
            except FileNotFoundError:
                read.append(key)
                continue  # never published, or replaced by a rename while this scan read the directory
            state = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            cached = self._due.get(key)
            if cached is None or cached[0] != state:
                intent = self._load(self.path / "intents" / (key + ".json"))
                if not _valid_intent(intent):
                    raise HostError("cleanup evidence has an unsupported shape: " + key + ".json")
                cached = (state, {"status": intent["status"], "retry": intent.get("retry"),
                                  "progress": {"terminal": intent["progress"].get("terminal")}})
                self._due[key] = cached
            read.append(key)
            if _attempt_due(cached[1], now):
                yield key

    def replay_cursor(self) -> str:
        if not self._published():
            return str((self._read_legacy() or {}).get("replay_cursor") or "")
        return str(self._meta().get("replay_cursor") or "")

    def intent_state(self, key: str) -> tuple[int, ...] | None:
        """The stored file's identity: any replacement by any producer changes it. No bytes are read."""
        try:
            stat = self._intent_path(key).stat()
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size

    def generated_digests(self) -> dict[str, str]:
        """The generated-file digests alone, without reading any intent file.

        Unlocked readers see the depth the meta names; a deepening published meanwhile is read again.
        """
        if not self._published():
            return self.read()["generated"]
        for _ in range(3):
            depth = self._generated_depth()
            generated: dict[str, str] = {}
            try:
                directory = _generated_directory(self.path, depth)
                for name in _json_names(directory, "generated digest read"):
                    _check("generated digest read")
                    file = directory / (name + ".json")
                    bucket = self._load(file)
                    if not isinstance(bucket, dict):
                        raise HostError("cleanup evidence has an unsupported shape: generated/" + file.name)
                    generated.update(bucket)
            except HostError:
                if self._generated_depth() == depth:
                    raise
                continue
            if self._generated_depth() == depth:
                return generated
        raise HostError("cleanup generated digests kept changing depth while being read")

    # Writes ------------------------------------------------------------------------------------

    def _replace(self, path: Path, body: bytes) -> None:
        """The one publication seam of every journal file, migration staging included.

        The file bound is checked before the replace, and only a completed replace is counted, by
        what the file is: an intent written by a migration is an intent write.
        """
        if len(body) > INTENT_FILE_LIMIT:
            raise HostError(f"cleanup journal file {path.name} would be {len(body)} bytes, over the "
                            f"{INTENT_FILE_LIMIT}-byte bound; nothing was published")
        _replace_file(path, body)
        kind = ("intent" if path.parent.name == "intents" else "meta" if path.name == "meta.json"
                else "generated")
        self.writes[kind] += 1
        self.writes["bytes"] += len(body)
        if kind == "intent":
            tick_count("cleanup_intent_writes")
        tick_count("cleanup_bytes_written", len(body))

    def _encode_intent(self, key: str, intent: dict[str, Any]) -> bytes:
        _compact_intent(intent)
        body = json.dumps(intent, sort_keys=True).encode()
        if len(body) > INTENT_FILE_LIMIT:
            # Fields of the record no cleanup path reads go first; ownership evidence never does.
            intent["record"] = cleanup_record(intent["record"])
            body = json.dumps(intent, sort_keys=True).encode()
        if len(body) > INTENT_FILE_LIMIT:
            raise HostError(f"cleanup intent {key} needs {len(body)} bytes after compaction, over the "
                            f"{INTENT_FILE_LIMIT}-byte bound; its stored obligation is unchanged")
        return body

    def _write_intent(self, key: str, intent: dict[str, Any]) -> None:
        body = self._encode_intent(key, intent)
        path = self._intent_path(key)
        try:
            if path.read_bytes() == body:
                return
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise HostError(f"cleanup evidence unreadable: {exc}") from exc
        self._replace(path, body)

    def _write_meta(self, **changes: Any) -> None:
        meta = {**self._meta(), **changes}
        self._replace(self.path / "meta.json", json.dumps(meta, sort_keys=True).encode())

    def _ensure(self) -> None:
        """Under ownership_lock: publish the layout, migrating the v1 journal once if there is one."""
        if self._published():
            if os.path.lexists(self.legacy):
                # Publication finished before a crash; the legacy file is already fully migrated.
                self._archive_legacy()
            return
        legacy = self._read_legacy()
        self._stage(legacy or {"version": 1, "intents": {}, "generated": {}})
        self._publish()
        if legacy is not None:
            self._archive_legacy()

    def _stage(self, value: dict[str, Any]) -> None:
        """Build the complete new layout beside the journal. An earlier unpublished copy is discarded:
        until publication the v1 document stays the only evidence."""
        if os.path.lexists(self._staging):
            shutil.rmtree(self._staging)
        root = self._staging
        files: list[tuple[Path, bytes]] = []
        for key, intent in sorted(value["intents"].items()):
            if not _valid_intent(intent):
                raise HostError("cleanup evidence has an unsupported shape: intent " + str(key))
            # Freshly parsed and owned here: compaction replaces record and heads, nothing else changes.
            migrated = {**intent, "record": cleanup_record(intent["record"])}
            files.append((self._intent_path(key, root), self._encode_intent(key, migrated)))
        depth, buckets = _generated_layout(value["generated"], 1)
        generated = _generated_directory(root, depth)
        files += [(generated / (prefix + ".json"), body) for prefix, body in sorted(buckets.items())]
        meta = {"version": _LAYOUT_VERSION, "replay_cursor": str(value.get("replay_cursor") or ""),
                "generated_depth": depth}
        files.append((root / "meta.json", json.dumps(meta, sort_keys=True).encode()))
        (root / "intents").mkdir(parents=True)
        generated.mkdir()
        for path, body in files:
            self._replace(path, body)
        _fsync_directory(generated)
        _fsync_directory(root)

    def _publish(self) -> None:
        os.rename(self._staging, self.path)
        _fsync_directory(self.path.parent)

    def _archive_legacy(self) -> None:
        """Keep the v1 document under a name nothing reads; never delete it."""
        target, index = self.archive, 1
        while os.path.lexists(target):
            target = self.archive.with_name(f"cleanup.v1-archive.{index}.json")
            index += 1
        os.rename(self.legacy, target)
        _fsync_directory(self.legacy.parent)

    @serialized
    def migrate(self) -> bool:
        """Migrate a v1 journal now, if there is one; True when this call migrated it.

        With no v1 journal nothing is written: the layout is created by the first mutation.
        """
        if not os.path.lexists(self.legacy):
            return False
        allowance = _allowance()
        if allowance is not None:
            # A one-time whole-document migration is no automatic replay's work: the dispatcher's
            # ordinary journal writers migrate it, and the replay waits for them.
            raise allowance.defer("v1 cleanup journal migration")
        pending = not self._published()
        self._ensure()
        return pending

    @contextlib.contextmanager
    def targeted(self, keys: set[str]) -> Iterator[None]:
        """Saves inside write back only these intents; every other stored value stays as it was read."""
        token = self._targets.set(set(keys))
        try:
            yield
        finally:
            self._targets.reset(token)

    @serialized
    def save(self, value: dict[str, Any], *, generated: bool = False) -> None:
        """Fsync each changed intent file and its publication before allowing effects.

        Only intents named by the value (or the current targets) are written, each only if its bytes
        differ; every other intent file is left untouched.
        """
        self._ensure()
        targets = self._targets.get()
        selected = [key for key in (value["intents"] if targets is None else sorted(targets))
                    if key in value["intents"]]
        for key in selected:
            self._write_intent(key, value["intents"][key])
        if targets is None and "replay_cursor" in value and \
                str(value["replay_cursor"] or "") != str(self._meta().get("replay_cursor") or ""):
            self._write_meta(replay_cursor=str(value["replay_cursor"] or ""))
        if generated:
            for name, digest in value["generated"].items():
                self._record_generated(name, digest)

    def _record_generated(self, name: str, digest: str) -> None:
        depth = self._generated_depth()
        directory = _generated_directory(self.path, depth)
        path = directory / (hashlib.sha256(name.encode()).hexdigest()[:depth] + ".json")
        bucket = self._load(path) if path.exists() else {}
        if not isinstance(bucket, dict):
            raise HostError("cleanup evidence has an unsupported shape: generated/" + path.name)
        if bucket.get(name) == digest:
            return
        bucket[name] = digest
        body = json.dumps(bucket, sort_keys=True).encode()
        if len(body) <= INTENT_FILE_LIMIT:
            self._replace(path, body)
            return
        self._deepen_generated(depth, {**self.generated_digests(), name: digest})

    def _deepen_generated(self, depth: int, generated: dict[str, str]) -> None:
        """Rebuild the whole map one or more levels deeper, publish it by the meta switch, then drop
        the old depth. Until the switch the old depth stays authoritative and complete."""
        deeper, buckets = _generated_layout(generated, depth + 1)
        directory = _generated_directory(self.path, deeper)
        if os.path.lexists(directory):
            shutil.rmtree(directory)  # an interrupted deepening; never named by the meta
        directory.mkdir()
        for prefix, body in sorted(buckets.items()):
            self._replace(directory / (prefix + ".json"), body)
        _fsync_directory(directory)
        self._write_meta(generated_depth=deeper)
        for stale in self.path.glob("generated*"):
            if stale != directory and stale.is_dir():
                shutil.rmtree(stale)
        _fsync_directory(self.path)

    @journal_mutation
    def generated(self, path: Path, body: str | bytes) -> None:
        data = body if isinstance(body, bytes) else body.encode()
        self._record_generated(str(path.absolute()), hashlib.sha256(data).hexdigest())

    @journal_mutation
    def set_replay_cursor(self, cursor: str) -> None:
        if str(self._meta().get("replay_cursor") or "") != cursor:
            self._write_meta(replay_cursor=cursor)

    def _single(self, key: str) -> dict[str, Any]:
        """A journal value holding just this intent, as read under the caller's lock."""
        intent = self.load_intent(key)
        return {"intents": {} if intent is None else {key: intent}}

    @journal_mutation
    def remember(self, task: dict[str, Any], record: dict[str, Any], *,
                 identity: dict[str, Any] | None = None, disposition: str = "owned") -> str:
        key = _intent_key(str(task["ref"]), str(record.get("attempt_id") or ""))
        value = self._single(key)
        key, changed = self.remember_into(value, task, record, identity=identity, disposition=disposition)
        if changed:
            self.save(value)
        return key

    @staticmethod
    def remember_into(value: dict[str, Any], task: dict[str, Any], record: dict[str, Any], *,
                      identity: dict[str, Any] | None = None,
                      disposition: str = "owned") -> tuple[str, bool]:
        """Stage the obligation in `value` only; the effect manifest plans against this same copy."""
        key = _intent_key(str(task["ref"]), str(record.get("attempt_id") or ""))
        # Only what cleanup reads is retained, so record telemetry (cursors, timestamps, evidence of
        # other subsystems) never changes the obligation or causes a write.
        record = cleanup_record(record)
        previous = value["intents"].get(key)
        if previous and previous.get("disposition") != "owned":
            if not previous.get("identity") and identity and not previous["progress"].get("removal_started"):
                previous["identity"] = identity
                return key, True
            return key, False
        # Each save fsyncs this intent's file: report a change only when this intent differs.
        before = copy.deepcopy(previous)
        intent = previous or {"task": copy.deepcopy(task), "record": copy.deepcopy(record),
                              "identity": identity, "heads": [], "progress": {},
                              "status": "owned", "reason": "", "disposition": "owned"}
        if identity is not None:
            if intent.get("identity"):
                old = intent["identity"]
                for field in ("repo", "common", "workspace", "device", "inode", "admin", "gitfile", "branch",
                              "admin_device", "admin_inode", "admin_gitdir", "admin_commondir"):
                    if old.get(field) != identity.get(field):
                        raise HostError("cleanup ownership changed within the recorded attempt")
            intent["identity"] = identity
        intent["record"] = record
        launch = record.get("launch_intent") or {}
        for head in (*(record.get(field) for field in ("worker_head_run", "review_head_run", "head_run")),
                     launch.get("head_run")):
            if isinstance(head, dict) and head.get("run_id"):
                # Keep all generations, including heads replaced during this attempt.
                _merge_head(intent["heads"], head)
        intent["disposition"] = disposition
        if disposition != "owned":
            intent["status"] = "pending"
        value["intents"][key] = intent
        return key, intent != before

    @journal_mutation
    def commit_intent(self, key: str, intent: dict[str, Any]) -> None:
        """Commit one replay result into the latest journal, retaining other producers.

        Identity acquired by a concurrent remember is new admission evidence. A
        replay planned without it cannot overwrite it or publish stale settlement.
        """
        value = self._single(key)
        previous = value["intents"].get(key)
        # A direct exact-run stop can checkpoint a receipt without a cleanup
        # obligation. Never resurrect a missing admitted cleanup obligation.
        if (previous is None and "disposition" in intent) or (previous is not None and any(
                previous.get(field) != intent.get(field)
                for field in ("identity", "record", "disposition"))):
            raise HostError("cleanup intent ownership changed while work was unlocked")
        merged = copy.deepcopy(intent)
        for head in (previous or {}).get("heads", []):
            _merge_head(merged["heads"], head)
        value["intents"][key] = merged
        with self.targeted({key}):
            self.save(value)

    @journal_mutation
    def defer_intent(self, key: str, attempted: dict[str, Any], reason: str) -> dict[str, Any]:
        """Retain a failed replay against fresh evidence without reverting its owner."""
        value = self._single(key)
        current = value["intents"].get(key)
        if current is None:
            raise HostError("cleanup intent disappeared while recording replay refusal")
        if all(current.get(field) == attempted.get(field)
               for field in ("identity", "record", "disposition")):
            pending = copy.deepcopy(attempted)
            for head in current["heads"]:
                _merge_head(pending["heads"], head)
        else:
            pending = copy.deepcopy(current)
            # Exact stop receipts remain true even if a producer updated the
            # obligation. Never carry destructive progress to a changed owner.
            for head in attempted["heads"]:
                if head.get("lifecycle") == "exited" and any(
                    all(head.get(field) == old.get(field) for field in
                        ("run_id", "scope_generation", "spec", "workspace", "task_ref", "role"))
                    for old in pending["heads"]
                ):
                    _merge_head(pending["heads"], head)
        pending["status"] = "pending"
        pending["reason"] = reason[:1000]
        value["intents"][key] = pending
        with self.targeted({key}):
            self.save(value)
        return pending

    @journal_mutation
    def reserve_attempt(self, key: str, now: float) -> dict[str, Any] | None:
        """Reserve this intent's next attempt, or None when it is not due (nothing is written then).

        Under the ownership lock, so of concurrent owners only one reserves a due attempt. The due
        time is durable before any effect: a crash after it waits out the cooldown like a refusal.
        The same write begins the attempt, so no extra publication precedes the effects.
        """
        intent = self.load_intent(key)
        if intent is None or not _attempt_due(intent, now):
            return None
        # A spent allowance reserves nothing: the intent keeps its due time (`Deferred`).
        _check("attempt reservation")
        intent["retry"] = {"last_attempt_at": now, "next_attempt_at": now + RETRY_COOLDOWN}
        _begin_attempt(intent)
        self._write_intent(key, intent)
        return intent

    @journal_mutation
    def request(self, task: dict[str, Any], disposition: str,
                record: dict[str, Any] | None = None) -> str:
        if record is None:
            state = self.data_dir / "dispatcher" / "production-state.json"
            try:
                payload = json.loads(state.read_text())
                records = payload.get("records", {})
                if not isinstance(records, dict):
                    raise TypeError("invalid records")
                record = records.get(task["ref"], {})
            except FileNotFoundError:
                record = {}
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise HostError("cleanup cannot read dispatcher ownership") from exc
        if not record:
            # Attach to the retained obligation whatever its disposition: a completed or
            # preserved intent keeps its settlement; only a live owned one is requested.
            value = self.read()
            matches = [key for key, intent in value["intents"].items()
                       if intent["task"]["id"] == task["id"] and intent["task"].get("kind") != "observer"]
            owned = [key for key in matches if value["intents"][key]["disposition"] == "owned"]
            for key in owned:
                value["intents"][key]["disposition"] = disposition
                value["intents"][key]["status"] = "pending"
            if owned:
                self.save({"intents": {key: value["intents"][key] for key in owned}})
            if matches:
                return (owned or matches)[-1]
        return self.remember(task, record or {}, disposition=disposition)

    @journal_mutation
    def observer_handoff(self, sprint: dict[str, Any]) -> str | None:
        """Stage the external observer obligation in the existing close transaction."""
        try:
            payload = json.loads((self.data_dir / "dispatcher" / "production-state.json").read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise HostError("observer close handoff cannot read current ownership") from exc
        observers = payload.get("observers", {})
        if not isinstance(observers, dict):
            raise HostError("observer close handoff has unreadable ownership")
        record = observers.get(sprint["ref"])
        if not record:
            return None
        raw = copy.deepcopy(record)
        raw.update(attempt_id=str(record.get("generation", "")) + ":" + str(record.get("launches", 0)),
                   worker=record.get("generation", ""))
        task = {"id": sprint["id"], "ref": sprint["ref"], "sprint": sprint["ref"],
                "project": "observers", "kind": "observer", "claim": {}}
        return self.remember(task, raw, disposition="observer-close")

    def summary(self, *, sprint: str = "", project: str = "") -> list[dict[str, Any]]:
        return [{"id": key, "ref": intent["task"]["ref"], "status": intent["status"],
                 "disposition": intent["disposition"], "reason": intent["reason"],
                 "identity": intent.get("identity"), "commit_proof": intent.get("commit_proof"),
                 "progress": intent["progress"],
                 # A terminal outcome is retained residue no replay will act on, named as such.
                 "terminal": (_terminal(intent) or {}).get("kind", ""),
                 "next_attempt_at": (_next_attempt(intent) if intent["status"] in {"pending", "preserved"}
                                     and _terminal(intent) is None else None)}
                for key, intent in sorted(self.read()["intents"].items())
                if (not sprint or intent["task"].get("sprint") == sprint)
                and (not project or _project_intent(intent, project))]

    def admission_refusal(self, reference: str) -> str:
        for intent in self.read()["intents"].values():
            if (intent["task"]["ref"] == reference and intent["status"] in {"pending", "preserved"}
                    and not intent["progress"].get("heads_stopped")):
                return "previous cleanup has not verified head settlement for " + reference
        return ""

def _intent_key(ref: str, attempt: str) -> str:
    return hashlib.sha256((ref + ":" + attempt).encode()).hexdigest()

def _project_intent(intent: dict[str, Any], project: str) -> bool:
    return intent["task"].get("project") == project and intent["task"].get("kind") != "observer"

