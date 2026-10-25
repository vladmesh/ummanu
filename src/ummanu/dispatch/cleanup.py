"""Durable ownership and settlement of card and observer Git residue.

CleanupJournal is the only producer of dispatcher/cleanup/. CleanupOwner is
its replay owner; inventory is its supported reader. Board archive and close
only request settlement. They never destroy work from inside a board transaction.
The installation lock covers journal publication and ownership admission only.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import shutil
import stat as stat_mode
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Self

from ummanu import _proc
from ummanu.dispatch.cleanup_journal import (
    _FANOUT_EVENTS_KEPT as _FANOUT_EVENTS_KEPT,
    _FANOUT_KEPT as _FANOUT_KEPT,
    _HEAD_FIELDS as _HEAD_FIELDS,
    _HEAD_RUN_FIELDS as _HEAD_RUN_FIELDS,
    _HEX as _HEX,
    _LAYOUT_VERSION as _LAYOUT_VERSION,
    _MAX_GENERATED_DEPTH as _MAX_GENERATED_DEPTH,
    _RECORD_KEPT as _RECORD_KEPT,
    INTENT_FILE_LIMIT as INTENT_FILE_LIMIT,
    RETRY_COOLDOWN as RETRY_COOLDOWN,
    TERMINAL_KINDS as TERMINAL_KINDS,
    CleanupJournal as CleanupJournal,
    _attempt_due as _attempt_due,
    _begin_attempt as _begin_attempt,
    _compact_heads as _compact_heads,
    _compact_intent as _compact_intent,
    _compact_record_runs as _compact_record_runs,
    _compact_run as _compact_run,
    _fsync_directory as _fsync_directory,
    _generated_buckets as _generated_buckets,
    _generated_directory as _generated_directory,
    _generated_layout as _generated_layout,
    _intent_key as _intent_key,
    _json_names as _json_names,
    _merge_head as _merge_head,
    _next_attempt as _next_attempt,
    _project_intent as _project_intent,
    _replace_file as _replace_file,
    _terminal as _terminal,
    _valid_intent as _valid_intent,
    cleanup_record as cleanup_record,
)
from ummanu.dispatch.ownership import (
    _ALLOWANCE as _ALLOWANCE,
    _LOCK_POLL as _LOCK_POLL,
    ATTEMPT_FLOOR as ATTEMPT_FLOOR,
    EFFECT_FLOOR as EFFECT_FLOOR,
    PUBLICATION_RESERVE as PUBLICATION_RESERVE,
    REPLAY_ALLOWANCE as REPLAY_ALLOWANCE,
    Allowance as Allowance,
    Deferred as Deferred,
    _allowance as _allowance,
    _check as _check,
    _flocker as _flocker,
    _guard as _guard,
    _held as _held,
    _locks as _locks,
    _path_lock as _path_lock,
    _wait as _wait,
    bulk_lane as bulk_lane,
    capacity_serialized as capacity_serialized,
    journal_mutation as journal_mutation,
    lifecycle as lifecycle,
    owner_operation as owner_operation,
    ownership_lock as ownership_lock,
    ownership_recovery as ownership_recovery,
    reference_lock as reference_lock,
    serialized as serialized,
)
from ummanu.dispatch.tick_telemetry import tick_stage, tick_stage_entering
from ummanu.dispatch.types import HostError, OwnershipChanged
from ummanu.infra import git_worktree

#: The bound of one cleanup Git child outside an automatic replay, and its ceiling inside one.
GIT_TIMEOUT = 30.0


def _run_git(argv: list[str], *, input: str | None = None, env: dict[str, str] | None = None,
             pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    """One cleanup Git child, bounded by `GIT_TIMEOUT`.

    Inside an automatic replay it runs in its own process group (`_proc.run_isolated`) within what
    is left of the allowance: a child still running then gets `SIGTERM` to its whole group, so Git
    removes the lock files of an unfinished ref transaction itself and a hook it runs ends with it,
    then the group is killed and reaped. Nothing it started is left to act after the call, which
    is that replay's deferral. Every other caller keeps the direct child's bound as it was.
    """
    allowance = _ALLOWANCE.get()
    what = "Git " + (argv[3] if len(argv) > 3 else "")
    if allowance is None:
        return subprocess.run(argv, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False,
                              input=input, env=env, pass_fds=pass_fds)
    allowance.timeout(GIT_TIMEOUT, what)
    try:
        return _proc.run_isolated(argv, input=input, env=env, pass_fds=pass_fds, timeout=GIT_TIMEOUT,
                                  within=allowance.remaining)
    except subprocess.TimeoutExpired as exc:
        if getattr(exc, "killed", False):
            raise allowance.defer(what + " (it outlived SIGTERM and was killed: any lock file it held "
                                  "is left for Git's own recovery, none is removed by cleanup)") from exc
        raise allowance.defer(what + " (its process group was ended at the bound)") from exc


def _read_git(repo: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    """Every Git read of the cleanup owner: with no optional locks, `status` never refreshes the
    index, so inventory and manifest planning write nothing into a repository or worktree."""
    return _run_git(["git", "-C", str(repo), *args], env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


def _git(repo: Path, *args: str, allow: bool = False) -> str:
    result = _read_git(repo, *args)
    if result.returncode and not allow:
        raise HostError(f"cleanup Git {args[0]} failed: {result.stderr.strip()[:400]}")
    return result.stdout.strip() if not result.returncode else ""


def _canonical(value: str | Path) -> Path:
    path = Path(value).expanduser().absolute()
    if path != path.resolve():
        raise HostError(f"cleanup path is substituted or noncanonical: {path}")
    return path


def _ref_tip(repo: Path, ref: str) -> str:
    result = _read_git(repo, "rev-parse", "--verify", "--quiet", ref)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1 and not result.stderr.strip():
        return ""
    raise HostError("cleanup candidate ref evidence is unreadable")


def _registered(repo: Path) -> list[dict[str, str]]:
    result = _read_git(repo, "worktree", "list", "--porcelain", "-z")
    if result.returncode:
        raise HostError("cleanup worktree registrations are unreadable")
    return git_worktree.parse_list(result.stdout)


_LEGACY_REASON = ("no attempt ownership was recorded; nothing was admitted; "
                 "Git residue is left to the inventory")


def _empty_attempt(intent: dict[str, Any]) -> bool:
    """A legacy obligation staged with no attempt, identity or head of its own."""
    return (intent["task"].get("kind") != "observer" and not intent["record"].get("attempt_id")
            and not intent.get("identity") and not intent["heads"])


def _no_workspace_attempt(intent: dict[str, Any]) -> bool:
    """An exact attempt that recorded no workspace and never named a head (an operation card)."""
    record = intent["record"]
    return (intent["task"].get("kind") != "observer" and bool(record.get("attempt_id"))
            and record.get("workspace", None) == "" and not intent.get("identity")
            and not intent["heads"] and not any(record.get(field) for field in _HEAD_FIELDS)
            and not (record.get("launch_intent") or {}).get("head_run"))


#: Disposable Python caches a Done card's exact workspace may hold, when Git ignores their contents.
_CACHE_DIRECTORIES = frozenset({".pytest_cache", "__pycache__", ".venv"})


def _cache_parts(name: str) -> tuple[str, ...] | None:
    """The components of a relative Git path strictly inside a cache directory, or None: never the
    cache itself, a directory row, an absolute path or one with an empty, `.` or `..` component."""
    parts = tuple(name.split("/"))
    if any(part in {"", ".", ".."} for part in parts) or not any(part in _CACHE_DIRECTORIES for part in parts[:-1]):
        return None
    return parts


def _open_below(root: int, parts: tuple[str, ...]) -> int:
    """A descriptor of root/parts..., each component a real directory opened without following."""
    directory = os.dup(root)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
    except BaseException:
        os.close(directory)
        raise
    return directory


#: The one cache whose leaf symlinks are disposable (ummanu-135): a virtual environment links its
#: interpreter outside itself and `lib64` beside it. Such a link goes as the directory entry it is.
_LINKED_CACHE = ".venv"


def _disposable_leaf(parts: tuple[str, ...], mode: int) -> bool:
    """The one leaf type policy of classification and effect, for an entry reached through real
    directories only: a regular file in any cache, or a symlink strictly inside a real `.venv`
    (dangling, to a file or to a directory alike). A link's target is never stat'ed or opened."""
    return stat_mode.S_ISREG(mode) or (stat_mode.S_ISLNK(mode) and _LINKED_CACHE in parts[:-1])


class _ConfinedDirectories:
    """Parent directories below one pinned root, for one pass over Git's path-sorted rows.

    Every component is opened from its real parent with O_NOFOLLOW, exactly as `_open_below` does;
    only a directory the previous row already reached is not opened again. At most one descriptor
    per depth of the current path is held, and the pass closes them all. A held directory renamed
    away mid-pass stays the one this pass reached through real parents; no link is ever entered.
    """

    def __init__(self, root: int) -> None:
        self._root = root
        self._open: list[tuple[str, int]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._keep(0)

    def _keep(self, depth: int) -> None:
        while len(self._open) > depth:
            os.close(self._open.pop()[1])

    def parent(self, parts: tuple[str, ...]) -> int:
        """The real directory root/parts[:-1] of one entry; an OSError if any component is not one."""
        parents = parts[:-1]
        if parents == tuple(name for name, _ in self._open):
            return self._open[-1][1] if self._open else self._root
        depth = 0
        while depth < min(len(self._open), len(parents)) and self._open[depth][0] == parents[depth]:
            depth += 1
        self._keep(depth)
        for part in parents[depth:]:
            directory = self._open[-1][1] if self._open else self._root
            self._open.append((part, os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                             dir_fd=directory)))
        return self._open[-1][1] if self._open else self._root


def _cache_entry(directories: _ConfinedDirectories, name: str) -> bool:
    """A disposable leaf (see `_disposable_leaf`) strictly inside a cache directory, reached from the
    pinned root through real directories only. A symlinked cache or parent, a leaf link outside a
    real `.venv`, or a nested repository Git reports as a directory, is not one."""
    parts = _cache_parts(name)
    if parts is None:
        return False
    try:
        mode = os.stat(parts[-1], dir_fd=directories.parent(parts), follow_symlinks=False).st_mode
    except OSError:
        return False
    return _disposable_leaf(parts, mode)


def _unlink_confined(directories: _ConfinedDirectories, name: str) -> None:
    """Unlink one disposable cache entry below the pinned root; nothing on the way, and no link
    target, is followed: the type is read again without following and unlink removes the entry."""
    parts = _cache_parts(name)
    if parts is None:
        raise HostError("cleanup cache entry is not inside a cache directory: " + name)
    directory = directories.parent(parts)
    if not _disposable_leaf(parts, os.stat(parts[-1], dir_fd=directory, follow_symlinks=False).st_mode):
        raise HostError("cleanup cache entry is no longer a disposable file or venv link: " + name)
    os.unlink(parts[-1], dir_fd=directory)


def _remove_namespace(namespace: Path) -> None:
    """Remove the exactly owned environment namespace like `shutil.rmtree`: every directory opened
    without following a link, every entry unlinked relative to its parent's descriptor, and the
    current allowance checked between entries. A cut leaves a smaller namespace whose retained
    identity (`generated_environment`) the next attempt resumes from. Every other caller keeps
    `shutil.rmtree` itself."""
    if _allowance() is None:
        shutil.rmtree(namespace)
        return
    parent = os.open(namespace.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.open(namespace.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            _clear_directory(directory)
        finally:
            os.close(directory)
        os.rmdir(namespace.name, dir_fd=parent)
    finally:
        os.close(parent)


def _clear_directory(directory: int) -> None:
    """Empty one directory, the allowance checked before each entry is read and removed. Entries are
    removed as the scan yields them, never collected first; a scan that removed anything is
    repeated, since removing while reading may leave entries the scan did not return."""
    while True:
        removed = False
        with os.scandir(directory) as scan:
            for entry in scan:
                _check("environment namespace removal")
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        _clear_directory(child)
                    finally:
                        os.close(child)
                    os.rmdir(entry.name, dir_fd=directory)
                else:
                    os.unlink(entry.name, dir_fd=directory)
                removed = True
        if not removed:
            return


@contextlib.contextmanager
def _pinned_root(identity: dict[str, Any]) -> Iterator[int]:
    """The recorded workspace directory itself, opened from `/` without following any component.

    A substituted ancestor or workspace (a symlink anywhere on the path) fails to open, and a
    replaced directory fails the recorded device/inode check. Everything read or unlinked through
    the descriptor is that exact directory, whatever later happens to the path.
    """
    path = Path(identity["workspace"])
    if not path.is_absolute():
        raise HostError("cleanup workspace path is not absolute")
    try:
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            directory = _open_below(root, path.parts[1:])
        finally:
            os.close(root)
    except OSError as exc:
        raise HostError("cleanup workspace root is substituted or unreadable: " + str(path)) from exc
    try:
        stat = os.fstat(directory)
        if (stat.st_dev, stat.st_ino) != (identity.get("device"), identity.get("inode")):
            raise HostError("cleanup workspace root is not the recorded directory: " + str(path))
        yield directory
    finally:
        os.close(directory)


def _read_pinned_git(root: int, *args: str) -> subprocess.CompletedProcess[str]:
    """`_read_git` inside the pinned directory: Git runs in the descriptor's own inode."""
    return _run_git(["git", "-C", f"/proc/self/fd/{root}", *args],
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, pass_fds=(root,))


def _identity(repo: Path, workspace: str, branch: str) -> dict[str, Any]:
    repo = _canonical(repo)
    if _git(repo, "rev-parse", "--show-toplevel") != str(repo):
        raise HostError("cleanup catalog repository Git root differs")
    common = _canonical(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    ref = "refs/heads/" + branch if branch else ""
    tip = _git(repo, "rev-parse", "--verify", ref) if ref else ""
    result: dict[str, Any] = {"repo": str(repo), "common": str(common), "branch": ref, "tip": tip}
    if not workspace:
        return result
    path = _canonical(workspace)
    listed = [row for row in _registered(repo) if row.get("worktree") == str(path)]
    if len(listed) != 1 or not path.is_dir() or path == repo:
        raise HostError("cleanup workspace is not an exact registered linked worktree")
    row = listed[0]
    if (row.get("branch", "") != ref or (ref and row.get("HEAD") != tip)
            or "locked" in row):
        raise HostError("cleanup workspace ref, tip or registration differs")
    actual_common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    admin = _canonical(_git(path, "rev-parse", "--absolute-git-dir"))
    if actual_common != str(common) or not admin.is_relative_to(common / "worktrees"):
        raise HostError("cleanup workspace common directory differs")
    if _git(path, "rev-parse", "--show-toplevel") != str(path):
        raise HostError("cleanup workspace Git root differs")
    if not (path / ".git").is_file() or (path / ".git").is_symlink():
        raise HostError("cleanup workspace Git registration is substituted")
    stat = path.stat()
    admin_stat = admin.stat()
    result.update(workspace=str(path), device=stat.st_dev, inode=stat.st_ino,
                  admin=str(admin), gitfile=(path / ".git").read_text(),
                  admin_device=admin_stat.st_dev, admin_inode=admin_stat.st_ino,
                  admin_gitdir=(admin / "gitdir").read_text(),
                  admin_commondir=(admin / "commondir").read_text(),
                  admin_head=(admin / "HEAD").read_text(),
                  tip=row["HEAD"])
    return result


class CleanupOwner:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.data_dir = Path(runtime.data_dir)
        self.journal = CleanupJournal(self.data_dir)
        self._operations = threading.RLock()
        # While a manifest is planned, every effect site appends here instead of acting.
        self._planned: list[dict[str, Any]] | None = None
        self._plan_removed: set[str] = set()
        # While a targeted replay runs, the manifest entry its operator reviewed.
        self._reviewed: dict[str, Any] | None = None
        self._admitted: dict[str, Any] | None = None
        # The cleanup projection each record flush last published, by card ref (see remember_record).
        self._remembered: dict[str, dict[str, Any]] = {}
        # The wall clock every attempt reservation and due check reads (stdlib unless injected).
        self.clock: Callable[[], float] = getattr(runtime, "cleanup_clock", None) or time.time
        # The elapsed clock of an automatic replay's allowance (stdlib monotonic unless injected).
        self.monotonic: Callable[[], float] = getattr(runtime, "cleanup_monotonic", None) or time.monotonic
        # Whether this thread's last replay_one reserved and ran an attempt.
        self._local = threading.local()
        # What the last `replay` invocation selected, attempted, skipped and deferred (see `replay`).
        self.last_replay: dict[str, Any] = {}

    def _workspace_identity(self, project: str, reference: str, record: Any) -> dict[str, Any] | None:
        binding = self.runtime.catalog.binding(project)
        identity = None
        if record.workspace and Path(record.workspace).exists():
            expected = self.data_dir / "workspaces" / project / record.worker
            if Path(record.workspace).absolute() == expected.absolute():
                try:
                    identity = _identity(Path(binding["repo"]), record.workspace, "pipeline/" + reference)
                except HostError:
                    # Retain unknown/legacy records too, before reconciliation
                    # can drop them. Absence of proof authorizes no effects.
                    pass
        return identity

    def remember(self, task: dict[str, Any], record: Any) -> str:
        identity = self._workspace_identity(task["project"], task["ref"], record)
        return self.journal.remember(task, record.to_json(), identity=identity)

    def remember_record(self, reference: str, record: Any, card: Callable[[], dict[str, Any]]) -> str:
        """`remember` for the dispatcher's record flush: no journal read or write while unchanged.

        The projection is what the journal would store (the cleanup record and the workspace
        identity) under its intent key. It is published through `remember`, with all of its
        ownership checks, whenever it differs from the last one this owner published, the attempt
        changed, or the intent file was replaced by anyone since (a stat, no bytes read).
        """
        raw = record.to_json()
        key = _intent_key(reference, str(raw.get("attempt_id") or ""))
        cached = self._remembered.get(reference)
        task = None
        if cached is None or cached["key"] != key:
            task = card()
            project = str(task["project"])
        else:
            project = cached["project"]
        with tick_stage("flush_identity"):
            identity = self._workspace_identity(project, reference, record)
        digest = hashlib.sha256(json.dumps([cleanup_record(raw), identity], sort_keys=True,
                                           default=str).encode()).hexdigest()
        state = self.journal.intent_state(key)
        if (cached is not None and state is not None
                and (cached["key"], cached["digest"], cached["state"]) == (key, digest, state)):
            return key
        if task is None:
            task = card()
        with tick_stage_entering("flush_lock", ownership_lock(self.data_dir)), tick_stage("flush_journal"):
            self.journal.remember(task, raw, identity=identity)
            state = self.journal.intent_state(key)
        self._remembered[reference] = {"key": key, "project": str(task["project"]), "digest": digest,
                                       "state": state}
        return key

    def cleanup(self, task: dict[str, Any], record: Any, disposition: str) -> dict[str, Any]:
        key = self.remember(task, record)
        self.journal.request(task, disposition, record.to_json())
        return self.replay_one(key)

    def _state(self) -> dict[str, Any]:
        path = self.data_dir / "dispatcher" / "production-state.json"
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return {"records": {}, "observers": {}}
        except (OSError, ValueError) as exc:
            raise HostError("current dispatcher ownership is unreadable") from exc
        if not isinstance(value, dict) or not isinstance(value.get("records", {}), dict):
            raise HostError("current dispatcher ownership is malformed")
        if value.get("phase") == "unavailable":
            raise HostError("current dispatcher ownership was unavailable")
        return value

    def cleanup_observer(self, record: Any) -> dict[str, Any]:
        from ummanu.observer_root import observer_root_repo
        sprint = self.runtime.sprints.show(record.sprint, include_cards=False)
        task = {"id": sprint["id"], "ref": record.sprint, "sprint": record.sprint,
                "project": "observers", "kind": "observer", "claim": {}}
        raw = record.to_json()
        raw.update(attempt_id=record.generation + ":" + str(record.launches), worker=record.generation)
        identity = None
        # Classify replacement before any workspace read: a replaced launch's
        # workspace pathname now names its successor's checkout.
        replaced = self._observer_successor({"task": task, "record": raw})
        if record.workspace:
            if record.workspace != self.runtime.host.observer_workspace(record.sprint):
                raise HostError("observer workspace is not the exact sprint workspace")
            if not replaced and Path(record.workspace).exists():
                try:
                    identity = _identity(observer_root_repo(self.data_dir), record.workspace, "")
                except HostError:
                    pass  # Stop the exact head, but preserve unproven Git placement.
        disposition = "observer-close" if sprint["status"] == "closed" else "observer-stop"
        key = self.journal.remember(task, raw, identity=identity, disposition=disposition)
        return self.replay_one(key)

    def _validate_owner(self, intent: dict[str, Any],
                        current: dict[str, Any] | None = None) -> dict[str, Any]:
        task = intent["task"]
        if task.get("kind") == "observer":
            if current is None:
                current = self.runtime.sprints.show(task["ref"], include_cards=False)
            if current["id"] != task["id"]:
                raise HostError("cleanup observer sprint identity changed")
            if intent["disposition"] == "observer-close" and current["status"] != "closed":
                raise HostError("observer closeout has no completed close handoff")
            # A replaced generation may settle only its own recorded runs; the
            # workspace belongs to its successor from now on.
            return {**current, "claim": {}, "successor": self._observer_successor(intent)}
        if current is None:
            current = self.runtime.reader.show(task["ref"])
        if (current["id"] != task["id"] or current.get("ref") != task["ref"]
                or current["project"] != task["project"]):
            raise HostError("cleanup card identity changed")
        state = self._state()
        successor = self._card_successor(intent, current, state.get("records", {}))
        if successor:
            # This admits only the predecessor's recorded head obligation. The
            # current board state and claim continue to belong to the successor.
            return {**current, "successor": successor}
        if (current.get("state") in {"in_progress", "validate", "ci", "review", "assessment", "ready"}
                and not current.get("closed")
                and (intent["disposition"] != "done" or current.get("state") != "assessment")):
            raise HostError("cleanup card is still admitted for work")
        record = intent["record"]
        claim = current.get("claim") or {}
        if claim.get("worker") and claim.get("worker") != record.get("worker"):
            raise HostError("cleanup claim is foreign or unknown")
        if claim.get("claimed_at") and claim != task.get("claim"):
            raise HostError("cleanup claim changed")
        for ref, other in state.get("records", {}).items():
            if not isinstance(other, dict):
                raise HostError("current dispatcher record is unreadable")
            same_target = (other.get("workspace") and other.get("workspace") == record.get("workspace"))
            if ref == task["ref"] or same_target:
                if (ref != task["ref"] or other.get("attempt_id") != record.get("attempt_id")
                        or other.get("worker") != record.get("worker")):
                    raise HostError("cleanup target has another active owner")
                for field in ("worker_head_run", "review_head_run"):
                    head = other.get(field)
                    if head and not any(head.get("run_id") == raw.get("run_id") and
                                        head.get("scope_generation") == raw.get("scope_generation")
                                        for raw in intent["heads"]):
                        raise HostError("cleanup target has a newer head")
                launch_head = (other.get("launch_intent") or {}).get("head_run")
                if launch_head and not any(launch_head.get("run_id") == raw.get("run_id") and
                                           launch_head.get("scope_generation") == raw.get("scope_generation")
                                           for raw in intent["heads"]):
                    raise HostError("cleanup target has a newer launch intent")
        return current

    def _card_successor(self, intent: dict[str, Any], card: dict[str, Any],
                        records: dict[str, Any]) -> str:
        """Prove a live replacement using the dispatcher's record and owned journal identity.

        Called only inside ownership admission, before any workspace or claim effect.
        No workspace is inspected: its recorded ownership belongs to the successor.
        """
        from ummanu.runtime.head import HeadRun
        task, record = intent["task"], intent["record"]
        current = records.get(task["ref"])
        if not current:
            return ""
        if not isinstance(current, dict):
            raise HostError("current dispatcher record is unreadable")
        if current.get("attempt_id") == record.get("attempt_id"):
            return ""
        attempt, successor = record.get("attempt_id"), current.get("attempt_id")
        if not attempt or not successor:
            # No replacement classification: the ordinary active-owner guards
            # still refuse this record, including an empty legacy obligation.
            return ""
        if (not isinstance(attempt, str) or not attempt or not isinstance(successor, str) or not successor
                or not all(task.get(field) for field in ("id", "ref", "project"))
                or not record.get("worker") or not record.get("workspace")
                or any(current.get(field) != record[field] for field in ("worker", "workspace"))):
            raise HostError("cleanup replacement attempt ownership is unproven")
        other = self.journal.load_intent(_intent_key(task["ref"], successor))
        if (other is None or other["status"] != "owned" or other["disposition"] != "owned"
                or other["task"].get("kind") == "observer"
                or any(other["task"].get(field) != task[field] for field in ("id", "ref", "project"))
                or other["record"] != cleanup_record(current)
                or card.get("claim") != other["task"].get("claim")
                or (card.get("claim") or {}).get("worker") not in (None, record["worker"])):
            raise HostError("cleanup replacement has no matching live attempt identity")
        identity, replacement = intent.get("identity"), other.get("identity")
        fields = ("repo", "common", "workspace", "branch", "device", "inode", "admin", "gitfile",
                  "admin_device", "admin_inode", "admin_gitdir", "admin_commondir", "admin_head")
        expected = self.data_dir / "workspaces" / task["project"] / record["worker"]
        binding = self.runtime.catalog.binding(task["project"])
        if (not isinstance(identity, dict) or not isinstance(replacement, dict)
                or any(field not in identity or field not in replacement
                       or identity[field] != replacement[field] for field in fields)
                or any(not isinstance(identity[field], str) or not identity[field]
                       for field in fields if field not in {"device", "inode", "admin_device", "admin_inode"})
                or any(type(identity[field]) is not int or identity[field] < 0
                       for field in ("device", "inode", "admin_device", "admin_inode"))
                or identity.get("workspace") != str(expected.absolute())
                or identity.get("workspace") != record["workspace"]
                or identity.get("repo") != str(Path(binding["repo"]).absolute())
                or identity.get("branch") != "refs/heads/pipeline/" + task["ref"]):
            raise HostError("cleanup replacement workspace identity is unproven")
        for ref, raw in records.items():
            if not isinstance(raw, dict):
                raise HostError("current dispatcher record is unreadable")
            if ref != task["ref"] and raw.get("workspace") == record["workspace"]:
                raise HostError("cleanup target has another active owner")
        old_runs, new_runs = [], []
        for obligation, runs in ((intent, old_runs), (other, new_runs)):
            if not obligation["heads"]:
                raise HostError("cleanup replacement head ownership is missing")
            for raw in obligation["heads"]:
                run = HeadRun.from_json(raw)
                if (run.workspace != record["workspace"] or run.task_ref.kind != "card"
                        or run.task_ref.ref != task["ref"] or run.role not in {"worker", "review", "reviewer"}):
                    raise HostError("cleanup replacement head identity is foreign")
                runs.append(run)
            raw_record = obligation["record"]
            for head in (*(raw_record.get(field) for field in _HEAD_RUN_FIELDS),
                         (raw_record.get("launch_intent") or {}).get("head_run")):
                if head and not any(head.get("run_id") == run.run_id
                                    and head.get("scope_generation", "") == run.scope_generation for run in runs):
                    raise HostError("cleanup replacement has an unrecorded head")
        if any(old.run_id == new.run_id or (old.scope_generation
                                           and old.scope_generation == new.scope_generation)
               for old in old_runs for new in new_runs):
            raise HostError("cleanup replacement shares a recorded head identity")
        return successor

    @contextlib.contextmanager
    def admission(self, task: dict[str, Any], *, intent: dict[str, Any] | None = None,
                  launch: bool = False) -> Iterator[dict[str, Any]]:
        """The one cleanup/launch ownership admission and commit barrier.

        Effects are ordered against board state/claim mutations by the per-card
        fence. Only the live key/claim check holds cleanup.lock; host work does not.
        The same barrier is used again after unlocked proofs, before destruction.
        """
        reader = (self.runtime.sprints if task.get("kind") == "observer"
                  else self.runtime.reader)
        client = getattr(reader, "client", None)
        transaction = getattr(client, "transaction", None)
        with reference_lock(self.data_dir, task["ref"]):
            with transaction() if transaction is not None else contextlib.nullcontext():
                current = self._admission_check(task, reader, client, intent=intent, launch=launch)
            # Only the effect fence spans disposal or Popen. The short row
            # transaction above is committed before any subprocess or unlink.
            yield current

    def _admission_check(self, task: dict[str, Any], reader: Any, client: Any, *,
                         intent: dict[str, Any] | None, launch: bool) -> dict[str, Any]:
        """Key revalidation inside the caller's short SQL transaction and effect fence."""
        transaction = getattr(client, "transaction", None)
        if transaction is not None and not client.call(
            "lockOwnershipReference", reference=task["ref"], observer=task.get("kind") == "observer"
        ):
            raise OwnershipChanged("cleanup/launch primary key no longer exists")
        # The SQL row fence keeps this key read stable through this short
        # check. Network reads and row contention precede the flock.
        current = (reader.show(task["ref"], include_cards=False)
                   if task.get("kind") == "observer" else reader.show(task["ref"]))
        with ownership_lock(self.data_dir):
            if launch:
                if (current["id"] != task["id"] or current.get("closed")
                        or (task.get("kind") == "observer" and current["status"] != task["status"])
                        or (task.get("kind") != "observer" and
                            (current.get("state") != task.get("state")
                             or current.get("project") != task.get("project")
                             or current.get("claim") != task.get("claim")))):
                    raise OwnershipChanged("launch ownership changed since admission")
                refusal = self.journal.admission_refusal(task["ref"])
                if refusal:
                    raise HostError(refusal)
            else:
                assert intent is not None
                if self._planned is None:
                    key = _intent_key(task["ref"], str(intent["record"].get("attempt_id") or ""))
                    fresh = self.journal.load_intent(key)
                    if fresh is None or any(fresh.get(field) != intent.get(field)
                                            for field in ("identity", "record", "disposition")):
                        raise HostError("cleanup intent changed since admission; workspace retained")
                current = self._validate_owner(intent, current=current)
                observed = {field: copy.deepcopy(current.get(field))
                            for field in ("id", "state", "status", "closed", "claim", "successor")}
                if self._admitted is not None and self._admitted != observed:
                    raise HostError("cleanup ownership changed since admission; workspace retained")
                self._admitted = observed
        return current

    def _observer_successor(self, intent: dict[str, Any]) -> str:
        """The generation:launch that replaced this observer intent, or "" while it is current."""
        observers = self._state().get("observers", {})
        if not isinstance(observers, dict):
            raise HostError("observer ownership unreadable")
        record = intent["record"]
        current = observers.get(intent["task"]["ref"])
        if current:
            if (current.get("generation"), current.get("launches")) == (record.get("generation"),
                                                                         record.get("launches")):
                return ""
            return str(current.get("generation", "")) + ":" + str(current.get("launches", 0))
        # The current record is gone; a later recorded launch still owns the workspace.
        mine = (float(record.get("launched_at") or 0), int(record.get("launches") or 0))
        for other in self.journal.read()["intents"].values():
            raw = other["record"]
            if (other["task"].get("kind") == "observer" and other["task"]["id"] == intent["task"]["id"]
                    and raw.get("attempt_id") != record.get("attempt_id")
                    and (float(raw.get("launched_at") or 0), int(raw.get("launches") or 0)) > mine):
                return str(raw.get("attempt_id"))
        return ""

    def _unsettled_cards(self, sprint: str) -> list[str]:
        # A terminal outcome is settled for this dependency only with its own stop and claim
        # proof: no replay will ever change it, and its residue stays visible in the inventory.
        return sorted(key for key, other in self.journal.read()["intents"].items()
                      if other["task"].get("sprint") == sprint and other["task"].get("kind") != "observer"
                      and not (other["status"] == "completed" or
                               (other["status"] == "preserved"
                                and (other["progress"].get("preservation_verified") or _terminal(other))
                                and other["progress"].get("heads_stopped")
                                and other["progress"].get("claim_settled"))))

    def _attempt_owners(self, intent: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """The card's attempt owners as one journal read found them, by key in order."""
        return {key: other for key, other in sorted(self.journal.read()["intents"].items())
                if other["task"]["id"] == intent["task"]["id"] and other["task"].get("kind") != "observer"
                and other["record"].get("attempt_id")}

    def _follow(self, intent: dict[str, Any], intents: dict[str, dict[str, Any]]) -> None:
        """A duplicate empty-attempt obligation takes its attempt owner's settlement; no effects."""
        owners = list(intents)
        states = [intents[key] for key in owners]
        intent["progress"]["settled_by"] = owners
        for flag in ("heads_stopped", "claim_settled", "preservation_verified"):
            intent["progress"][flag] = all(bool(other["progress"].get(flag)) for other in states)
        unsettled = [key for key, other in zip(owners, states) if other["status"] not in {"completed", "preserved"}]
        intent["progress"].pop("terminal", None)
        if unsettled:
            intent["status"] = "pending"
            intent["reason"] = ("follows attempt owner " + ", ".join(unsettled) + ": "
                                + "; ".join(intents[key]["reason"] for key in unsettled))[:1000]
        elif all(other["status"] == "completed" for other in states):
            intent["status"] = "completed"
            intent["reason"] = "; ".join(other["reason"] for other in states if other["reason"])
        else:
            intent["status"] = "preserved"
            intent["reason"] = "; ".join(other["reason"] for other in states if other["status"] == "preserved")
            terminal = [key for key, other in zip(owners, states) if _terminal(other) is not None]
            # Ends with its owners only when each of them ended: completed, or terminal itself.
            if terminal and all(other["status"] == "completed" or _terminal(other) is not None
                                for other in states):
                intent["progress"]["terminal"] = {"kind": "follows", "owners": terminal,
                                                  "reason": intent["reason"][:1000]}

    def _terminal_card(self, current: dict[str, Any]) -> None:
        if not current.get("closed") and current.get("state") != "done":
            raise HostError("cleanup without workspace ownership awaits a closed or Done card")

    def _no_current_record(self, intent: dict[str, Any]) -> None:
        """Neither the card nor its workspace has a current dispatcher record."""
        task = intent["task"]
        root = self.data_dir / "workspaces" / str(task.get("project", ""))
        for ref, other in self._state().get("records", {}).items():
            if not isinstance(other, dict):
                raise HostError("current dispatcher record is unreadable")
            workspace = Path(str(other.get("workspace") or ""))
            if (ref == task["ref"] or str(other.get("worker") or "").startswith(task["ref"] + "-")
                    or (workspace.parent == root and workspace.name.startswith(task["ref"] + "-"))):
                raise HostError("cleanup without workspace ownership awaits release of current record " + ref)

    def _settle_without_identity(self, intent: dict[str, Any], current: dict[str, Any]) -> None:
        """Return only when an exact no-workspace attempt may complete; otherwise raise."""
        if _empty_attempt(intent):
            self._terminal_card(current)
            self._no_current_record(intent)
            raise Preserved(_LEGACY_REASON, verified=True)
        if not _no_workspace_attempt(intent):
            # An attempt whose identity was never captured: the identity-specific refusal must not
            # hide a project that is no longer registered (after the owner and head fences above).
            if intent["task"].get("kind") != "observer":
                self._registration(str(intent["task"]["project"]))
            raise Preserved("missing exact workspace/attempt ownership proof")
        self._terminal_card(current)
        self._no_current_record(intent)
        repo = _canonical(self._project_binding(intent["task"]["project"])["repo"])
        ref = "refs/heads/pipeline/" + intent["task"]["ref"]
        tip = _ref_tip(repo, ref)
        if tip:
            raise Preserved("attempt recorded no workspace but " + ref + " exists at " + tip, verified=True)

    def _scope_fence(self, intent: dict[str, Any], *, replaced: bool = False) -> None:
        if not replaced and (intent["disposition"] == "catch-up" or _empty_attempt(intent)
                             or _no_workspace_attempt(intent)):
            from ummanu.dispatch.watchdog import pid_file_path
            from ummanu.runtime.head.identity import head_process_status
            for role in ("worker", "review"):
                path = Path(pid_file_path(role, intent["task"]["ref"]))
                if path.exists():
                    status = head_process_status(str(path))
                    if status.get("state") != "dead":
                        if intent["disposition"] == "catch-up":
                            raise HostError("archived branch still has live or unknown head identity")
                        raise HostError(f"{role} pid file {path} names a live or unknown process")
        fence = getattr(self.runtime.host, "fence_cleanup_scopes", None)
        if callable(fence):
            from ummanu.runtime.head import HeadRun, TaskRef
            task = intent["task"]
            reference = (TaskRef.sprint(task["ref"]) if task.get("kind") == "observer"
                         else TaskRef.card(task["ref"]))
            runs = [HeadRun.from_json(raw) for raw in intent["heads"]]
            allowance = _allowance()
            # Inside an automatic replay a terminal scope's native disappearance proof is cut to it.
            bound = {} if allowance is None else {"remaining": allowance.remaining}
            if replaced:
                # Only the recorded runs' own scope owners and bindings; the
                # workspace-wide inspection would reach the successor's scopes.
                fence(intent["record"].get("workspace", ""), reference, runs, recorded_only=True, **bound)
            else:
                fence(intent["record"].get("workspace", ""), reference, runs, **bound)

    def _project_binding(self, project: str) -> dict[str, Any]:
        """The card project's binding; a project this installation does not register is terminal."""
        self._registration(project)
        return self.runtime.catalog.binding(project)

    def _registration(self, project: str) -> None:
        """Raise Terminal when the catalog's readable registration table does not name the project.

        Decided from the table, never from a refusal's text: a registered but disabled project, or a
        catalog with no readable table, is still the binding's own (retryable) refusal. No
        repository is searched for, read or changed for an unregistered one.
        """
        catalog = self.runtime.catalog
        registered = getattr(catalog, "registered_bindings", None)
        if registered is None:
            registered = getattr(catalog, "bindings", None)
        if isinstance(registered, dict) and not any(
                name in registered for name in (project, project.replace("_", "-"))):
            raise Terminal("project-unregistered", "project " + project + " is not registered on this "
                           "installation; no repository was read or changed, and any Git residue of the "
                           "attempt is left as it is with its recorded identity")

    def _binding(self, intent: dict[str, Any]) -> tuple[Path, str]:
        if intent["task"].get("kind") == "observer":
            from ummanu.observer_root import observer_root_repo
            repo = _canonical(observer_root_repo(self.data_dir))
            if (intent["identity"]["repo"] != str(repo) or intent["identity"]["common"] !=
                    _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")):
                raise HostError("cleanup observer repository changed")
            return repo, ""
        binding = self._project_binding(intent["task"]["project"])
        repo = _canonical(binding["repo"])
        base = (intent["task"].get("workspace") or {}).get("base_branch")
        allowed = [binding.get("default_branch") or "main", *(binding.get("integration_bases") or [])]
        base = base or allowed[0]
        if base not in allowed or base.startswith("pipeline/"):
            raise HostError("cleanup integration branch is not registered")
        identity = intent["identity"]
        if identity["repo"] != str(repo) or identity["common"] != _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise HostError("cleanup repository registration changed")
        return repo, base

    def _stop(self, intent: dict[str, Any], *, workspace_owned: bool = True) -> None:
        from ummanu.runtime.head import HeadRun, StopInitiator
        from ummanu.runtime.local_pty_head import head_run_pid_file
        self._provenance("cleanup-before-stop")
        environment = getattr(self.runtime.host, "_decide_workspace_environment_ownership", None)
        if workspace_owned and intent["record"].get("workspace") and callable(environment):
            self._environment_owner(intent, Path(intent["record"]["workspace"]))
        # A malformed/unknown head is never absence evidence. Keep the original
        # identities after a stop; its receipt can be retried after a crash.
        record = intent["record"]
        if (intent["task"].get("kind") == "observer" and not record.get("head_run")
                and (record.get("handle") or record.get("pid_file") or record.get("head_possible"))):
            raise HostError("cleanup observer head ownership is missing")
        roles = () if intent["task"].get("kind") == "observer" else (("worker", "worker_head_run"), ("review", "review_head_run"))
        for role, field in roles:
            if (record.get("handle" if role == "worker" else "review_handle")
                    or record.get(role + "_pid_file")) and not record.get(field):
                raise HostError("cleanup head ownership is missing")
        latest = {(raw["run_id"], raw.get("scope_generation", "")): raw for raw in intent["heads"]}
        runs = []
        for raw in latest.values():
            run = HeadRun.from_json(raw)
            if self._unlaunched_placeholder(intent, run):
                continue  # Never launched by this attempt: there is no head to stop.
            expected_kind = "sprint" if intent["task"].get("kind") == "observer" else "card"
            if (run.workspace != record.get("workspace") or run.task_ref.ref != intent["task"]["ref"]
                    or run.task_ref.kind != expected_kind):
                raise HostError("cleanup head belongs to another workspace or card")
            guard = getattr(self.runtime.host, "_guard_head_run", None)
            # A replaced attempt's pid file now names its successor: never read it. A scoped run
            # is fenced by its own run directory's heartbeat, as its stop below is addressed.
            if callable(guard) and workspace_owned:
                pid_file = (str(head_run_pid_file(self._heads_root(), run.run_id)) if run.scope_generation
                            else run.pid_file)
                guard(run, run.role, pid_file=pid_file, leaf=run.leaf,
                      task=run.task_ref.ref if run.task_ref.kind == "sprint" else "card:" + run.task_ref.ref)
            if not run.scope_generation and run.settled:
                continue  # Deployed unscoped runs retain their confirmed stop receipt.
            runs.append(run)
        # Fence every recorded identity before stopping the first head. A
        # foreign reviewer must not cause us to stop a worker and only then refuse.
        for run in runs:
            # A role's shared pid file names only its latest generation, and a replaced attempt's
            # names its successor. A scoped run, and every run of a replaced attempt, is addressed
            # by its own run directory instead; the runtime still proves its own identity and scope.
            target = run if workspace_owned and not run.scope_generation else replace(run, pid_file="")
            if self._planned is not None:
                self._planned.append({"effect": "stop-head", "run_id": run.run_id, "role": run.role,
                                      "scope_generation": run.scope_generation})
                continue
            allowance = _allowance()
            # Inside an automatic replay the runtime's own waits (supervisor exchange, scope stop,
            # exit confirmation) are cut to what is left; an unconfirmed stop is a pending receipt.
            bound = {} if allowance is None else {"remaining": allowance.remaining}
            if allowance is not None:
                allowance.timeout(GIT_TIMEOUT, "head stop")
            receipt = self.runtime.host.head_runtime_for(target).stop(
                target, StopInitiator(actor="ummanu-dispatcher", reason="owned residue cleanup"), **bound)
            if not receipt.ok and allowance is not None and allowance.remaining() <= 0:
                raise allowance.defer("head stop (" + str(receipt.reason)[:200] + ")")
            if not receipt.ok:
                raise HostError("cleanup head stop pending: " + receipt.reason)
            settled = getattr(receipt, "run", None)
            if (not isinstance(settled, HeadRun) or not settled.same_run(run) or not settled.settled
                    or settled.scope_generation != run.scope_generation
                    or settled.spec != run.spec or settled.workspace != run.workspace
                    or settled.task_ref != run.task_ref or settled.role != run.role):
                raise HostError("cleanup stop receipt does not settle the recorded run")
            # Retain the recorded identity, including the pid file the run was launched with.
            settled = replace(settled, pid_file=run.pid_file)
            if settled.to_json() not in intent["heads"]:
                _merge_head(intent["heads"], settled.to_json())
                self._checkpoint_intent(intent)

    def _heads_root(self) -> Path:
        root = getattr(self.runtime.host, "_local_pty_root", None)
        return Path(root()) if callable(root) else self.data_dir / "heads"

    def _unlaunched_placeholder(self, intent: dict[str, Any], run: Any) -> bool:
        """An identity a re-stop of a settled head once minted and never launched (secretary-1918).

        The boundary is evidence that the run was never launched, and all of it must hold:
        no scope_generation; role ''; a card task_ref naming this attempt's worker id, which is
        not the card ref; the intent's workspace; no run directory (lexists) for the run_id; and
        a pid file that is absent, dead or names another run.

        The entry's own lifecycle is neither a qualifier nor a disqualifier: a stop that addressed
        no run directory recorded `exited` without any head behind it. Anything else is still a
        foreign head and keeps the refusal.
        """
        from ummanu.runtime.head.identity import head_process_status
        from ummanu.runtime.local_pty_head import head_run_directory
        record = intent["record"]
        worker = str(record.get("worker") or "")
        if (run.scope_generation or run.role or run.task_ref.kind != "card" or not worker
                or run.task_ref.ref != worker or run.task_ref.ref == intent["task"]["ref"]
                or not run.workspace or run.workspace != record.get("workspace")):
            return False
        try:
            run_dir = head_run_directory(self._heads_root(), run.run_id)
        except ValueError:
            return False
        if os.path.lexists(run_dir):
            return False
        if run.pid_file and os.path.lexists(run.pid_file):
            status = head_process_status(run.pid_file)
            named = (status.get("record") or {}).get("run_id")
            if status.get("state") != "dead" and named in (None, run.run_id):
                return False
        return True

    def _provenance(self, boundary: str) -> None:
        require = getattr(self.runtime.host, "_require_production_runtime", None)
        if not callable(require):
            return
        allowance = _allowance()
        if allowance is None:
            require(boundary)
            return
        # The provenance probe is a child process: inside an automatic replay it runs within the
        # allowance, and one cut short is the allowance's refusal, not a provenance verdict.
        allowance.timeout(GIT_TIMEOUT, "runtime provenance " + boundary)
        try:
            require(boundary, allowance.remaining)
        except HostError as exc:
            if allowance.remaining() <= 0:
                raise allowance.defer("runtime provenance " + boundary) from exc
            raise

    def _settle_claim(self, intent: dict[str, Any]) -> None:
        with self.admission(intent["task"], intent=intent) as current:
            self._settle_claim_admitted(intent, current)

    def _settle_claim_admitted(self, intent: dict[str, Any], current: dict[str, Any]) -> None:
        if intent["task"].get("kind") != "observer" and current.get("successor"):
            # Only the old attempt's duty is complete. Never release or wait on
            # the successor's claim, even when the board is Blocked or Validate.
            intent["progress"]["claim_settled"] = True
            intent["progress"]["handoff"] = {"attempt_id": intent["record"]["attempt_id"],
                                               "successor_attempt_id": current["successor"]}
            return
        claim = current.get("claim") or {}
        if claim.get("worker"):
            if not current.get("closed") and current.get("state") != "done":
                raise HostError("cleanup awaits terminal board claim settlement")
            if self._planned is None:
                self.runtime.writer.settle_cleanup_claim(current, intent["record"].get("worker", ""))
        if self._planned is not None:
            self._planned.append({"effect": "settle-claim", "worker": claim.get("worker") or "",
                                  "board_write": bool(claim.get("worker"))})
        intent["progress"]["claim_settled"] = True

    def _dirty(self, intent: dict[str, Any], path: Path,
               root: int | None = None) -> tuple[list[str], list[str]]:
        """The one dirty predicate of planning, inventory and execution: dirty rows, disposable caches.

        Include ignored files. Only bytes written by the prompt producer, the existing exact
        environment namespace contract and, for the exact workspace of a card this replay's
        admission saw Done, Git-ignored regular files inside its Python caches and leaf links inside
        its real `.venv` (see `_cache_entry`) can be disposable. Every later admission must see that same state. Status and cache
        classification read the recorded directory through its pinned descriptor (`root`, or one
        pinned here), never through a path that could be substituted. It reads only.
        """
        if root is None:
            with _pinned_root(intent["identity"]) as pinned:
                return self._dirty(intent, path, pinned)
        result = _read_pinned_git(root, "status", "--porcelain=v1", "--ignored", "--untracked-files=all", "-z")
        if result.returncode:
            raise HostError("cleanup workspace status is unreadable")
        status = result.stdout
        dirty: list[str] = []
        caches: list[str] = []
        generated = self.journal.generated_digests()
        environment = self._environment_owner(intent, path)
        done = (intent["task"].get("kind") != "observer"
                and (self._admitted or {}).get("state") == "done")
        prefix = str(path) + "/"
        with _ConfinedDirectories(root) as directories:
            for row in status.split("\0"):
                if not row:
                    continue
                _check("workspace status classification")
                name = row[3:]
                if row[:2] in {"??", "!!"}:
                    if name.startswith(".ummanu-task-env/") and environment == "dispatcher":
                        continue
                    # `str(path / name)` for Git's normalized names, without a Path per row.
                    expected = generated.get(prefix + name.rstrip("/"))
                    if expected:
                        file = path / name
                        if not file.is_symlink() and file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == expected:
                            continue
                    if row[:2] == "!!" and done and _cache_entry(directories, name):
                        caches.append(name)
                        continue
                dirty.append(row)
        return dirty, caches

    def _environment_owner(self, intent: dict[str, Any], path: Path) -> str:
        try:
            return self.runtime.host._decide_workspace_environment_ownership(str(path))
        except HostError:
            namespace = path / ".ummanu-task-env"
            saved = intent.get("generated_environment")
            if saved and intent["progress"].get("environment_removal_started"):
                namespace = _canonical(namespace)
                if namespace.exists():
                    stat = namespace.stat()
                    if (stat.st_dev, stat.st_ino) == (saved["device"], saved["inode"]):
                        return "dispatcher"
            raise

    def _checkpoint_intent(self, intent: dict[str, Any]) -> None:
        if self._planned is not None:
            return
        key = _intent_key(intent["task"]["ref"], str(intent["record"].get("attempt_id") or ""))
        self.journal.commit_intent(key, intent)

    def _shared_removal_proof(self, intent: dict[str, Any]) -> str:
        """An attempt can reuse a directory whose later owner settled its Git effects."""
        identity = intent["identity"]
        fields = ("repo", "common", "workspace", "device", "inode", "admin", "gitfile", "branch")
        for key, other in self.journal.read()["intents"].items():
            if (other["task"]["id"] == intent["task"]["id"] and other["progress"].get("workspace_removed")
                    and other.get("identity") and
                    all(other["identity"].get(field) == identity.get(field) for field in fields)):
                return key
        return ""

    def _admitted_registration(self, intent: dict[str, Any], repo: Path) -> None:
        """The retained admin entry can finish a previously admitted Git effect."""
        path = _canonical(intent["identity"]["workspace"])
        if not intent["progress"].get("removal_started") or path.exists() or path.is_symlink():
            raise HostError("cleanup missing directory has no admitted removal proof")
        self._retained_registration(intent, repo)

    def _retained_registration(self, intent: dict[str, Any], repo: Path) -> None:
        """The missing directory's registration, admin entry and ref are still exactly the recorded ones."""
        identity = intent["identity"]
        path = _canonical(identity["workspace"])
        common = _canonical(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        admin = _canonical(identity["admin"])
        if (str(common) != identity["common"] or admin.parent != common / "worktrees"
                or not admin.is_dir()):
            raise HostError("cleanup retained admin/common directory changed")
        stat = admin.stat()
        if (stat.st_dev, stat.st_ino) != (identity.get("admin_device"), identity.get("admin_inode")):
            raise HostError("cleanup retained admin identity changed")
        for name in ("gitdir", "commondir", "HEAD"):
            file = admin / name
            if (not file.is_file() or file.is_symlink()
                    or file.read_text() != identity.get("admin_" + name.lower())):
                raise HostError("cleanup retained admin registration changed")
        if ((admin / "gitdir").read_text().strip() != str(path / ".git")
                or (admin / (admin / "commondir").read_text().strip()).resolve() != common):
            raise HostError("cleanup retained admin path mapping changed")
        rows = [row for row in _registered(repo) if row.get("worktree") == str(path)]
        if (len(rows) != 1 or rows[0].get("branch", "") != identity["branch"]
                or rows[0].get("HEAD") != identity["tip"] or "locked" in rows[0]):
            raise HostError("cleanup retained worktree registration or HEAD changed")
        if identity["branch"] and _ref_tip(repo, identity["branch"]) != identity["tip"]:
            raise HostError("cleanup retained worktree ref changed")

    def _verify_commits(self, intent: dict[str, Any], repo: Path, *,
                        preservation_verified: bool = True) -> None:
        """Every exact HEAD needs a persistent publication/retention witness."""
        identity = intent["identity"]
        tip = identity["tip"]
        if self._published(repo, tip):
            refs = _git(repo, "for-each-ref", "--format=%(refname)", "--contains=" + tip, "refs/remotes/")
            if not refs:
                raise HostError("cleanup commit retention witness disappeared")
            intent["commit_proof"] = {"tip": tip, "publication": "remote-tracking", "refs": refs.splitlines()}
            return
        if self._observer_root(intent, repo, tip):
            return
        if not preservation_verified:
            raise HostError("cleanup interrupted removal awaits commit retention proof")
        raise Preserved("unpublished commits; workspace and candidate ref retained", verified=True)

    def _observer_root(self, intent: dict[str, Any], repo: Path, tip: str) -> bool:
        """Record the owned observer root as the tip's retention witness, when it is one."""
        if intent["task"].get("kind") != "observer":
            return False
        # The existing observer producer cuts detached worktrees from this
        # empty, parentless root commit. Its named branch already retains
        # it. No user commit or arbitrary local ref gains this authority.
        from ummanu.dispatch.host_runtime.observer_io import OBSERVER_REPO_BRANCH
        ref = "refs/heads/" + OBSERVER_REPO_BRANCH
        if (_ref_tip(repo, ref) == tip and _git(repo, "rev-list", "--parents", "-n", "1", tip) == tip
                and not _git(repo, "ls-tree", "-r", tip)):
            intent["commit_proof"] = {"tip": tip, "publication": "owned observer root", "refs": [ref]}
            return True
        return False

    def _retention_witness(self, intent: dict[str, Any], repo: Path) -> str:
        """A ref that retains the recorded tip, before a registered Git attempt may end terminally.

        A remote-tracking ref containing the tip, the owned observer root, or the attempt's own
        candidate ref still containing it. Recorded in `commit_proof` and returned for the reason.
        Without one the work behind the tip may be lost: that stays a retryable refusal, with the
        obligation and the claim kept. A head stop receipt is no such witness.
        """
        identity = intent["identity"]
        tip = str(identity.get("tip") or "")
        if not tip:
            raise HostError("cleanup terminal outcome needs a recorded tip; obligation and claim retained")
        remote = _git(repo, "for-each-ref", "--format=%(refname)", "--contains=" + tip, "refs/remotes/")
        if remote:
            intent["commit_proof"] = {"tip": tip, "publication": "remote-tracking", "refs": remote.splitlines()}
            return ", ".join(remote.splitlines())
        if self._observer_root(intent, repo, tip):
            return intent["commit_proof"]["refs"][0]
        branch = str(identity.get("branch") or "")
        current = _ref_tip(repo, branch) if branch else ""
        if current:
            contained = _read_git(repo, "merge-base", "--is-ancestor", tip, current)
            if contained.returncode == 0:
                intent["commit_proof"] = {"tip": tip, "publication": "candidate ref", "refs": [branch]}
                return branch
            if contained.returncode != 1:
                raise HostError("cleanup commit retention evidence is unreadable")
        raise HostError("cleanup found no remote-tracking or candidate ref retaining recorded tip " + tip
                        + "; the obligation and the claim are retained")

    def _remove_workspace(self, intent: dict[str, Any], repo: Path) -> None:
        self._provenance("cleanup-before-worktree-remove")
        identity = intent["identity"]
        workspace = identity.get("workspace")
        if not workspace:
            return
        path = _canonical(workspace)
        rows = [row for row in _registered(repo) if row.get("worktree") == workspace]
        if not path.exists() and not rows:
            admin = _canonical(identity["admin"])
            if admin.exists():
                raise HostError("cleanup retained admin registration changed its workspace mapping")
            # Interrupted Git removal is resumable only from an effect already
            # admitted against this exact identity and durably recorded.
            if not intent["progress"].get("removal_started"):
                shared = self._shared_removal_proof(intent)
                if not shared:
                    # Current facts, just read: no directory, no registration, no admin entry, and a
                    # ref that still retains the recorded tip.
                    witness = self._retention_witness(intent, repo)
                    raise Terminal("workspace-disappeared", "workspace directory, Git registration and "
                                   "admin entry are gone without removal evidence; nothing was removed by "
                                   "cleanup; recorded tip " + identity["tip"] + " is retained by " + witness)
                intent["progress"]["workspace_disposed_by"] = shared
            self._verify_commits(intent, repo)
            return
        missing = not path.exists() and not path.is_symlink()
        caches: list[str] = []
        if missing:
            if not intent["progress"].get("removal_started"):
                # Only the exact recorded registration may end this way; a changed one is a refusal.
                self._retained_registration(intent, repo)
                witness = self._retention_witness(intent, repo)
                raise Terminal("registration-without-directory", "workspace directory is missing while its "
                               "exact Git registration remains, with no admitted removal proof; the "
                               "registration and admin entry are retained untouched; recorded tip "
                               + identity["tip"] + " is retained by " + witness)
            self._admitted_registration(intent, repo)
            self._verify_commits(intent, repo, preservation_verified=False)
        else:
            fresh = _identity(repo, workspace, identity["branch"].removeprefix("refs/heads/") if identity["branch"] else "")
            if fresh != identity:
                raise HostError("cleanup workspace, registration or HEAD changed")
            dirty, caches = self._dirty(intent, path)
            if dirty:
                raise Preserved("dirty tracked, untracked or ignored work: " + "; ".join(dirty[:8]), verified=True)
            self._verify_commits(intent, repo)
        if self._planned is not None:
            self._planned.append({
                "effect": "remove-worktree", "path": workspace, "identity": identity,
                "dirty": "missing directory; admitted removal resumes" if missing else "clean",
                "ignored_caches": len(caches),
                "commit_proof": intent.get("commit_proof"),
                "environment": "absent" if missing else self._environment_owner(intent, path)})
            self._plan_removed.add(workspace)
            return
        allowance = _allowance()
        if allowance is not None:
            # No new destructive stage starts in the allowance's last moments.
            allowance.require(EFFECT_FLOOR, "workspace removal admission")
        with self.admission(intent["task"], intent=intent):
            if not missing:
                # Board and journal admission does not cover the filesystem: the exact recorded
                # workspace is revalidated and pinned, and the same predicate runs again inside it.
                # Only its Git-ignored cache files and venv links go, unlinked through that descriptor.
                if _identity(repo, workspace, identity["branch"].removeprefix("refs/heads/")
                             if identity["branch"] else "") != identity:
                    raise HostError("cleanup workspace, registration or HEAD changed before its cache "
                                    "effects; nothing was removed")
                with _pinned_root(identity) as root:
                    dirty, caches = self._dirty(intent, path, root)
                    if dirty:
                        raise HostError("cleanup workspace changed since its dirty check; workspace retained")
                    with _ConfinedDirectories(root) as directories:
                        for name in caches:
                            # Each entry is disposable by itself: a cut leaves the rest for the next
                            # attempt's full proof, never a partial unproven state.
                            _check("ignored cache removal")
                            _unlink_confined(directories, name)
            # No forced removal: first delete only exact generated bytes whose
            # ownership was validated above. Git independently refuses dirty work.
            generated = self.journal.generated_digests()
            for name, digest in generated.items():
                _check("generated file removal")
                file = Path(name)
                # Nested generated files, such as an editable install's metadata, qualify only through
                # real directories of this workspace: a symlinked parent never leads the unlink outside.
                if (file.is_relative_to(path) and file != path and file.parent.resolve() == file.parent
                        and file.is_file() and not file.is_symlink()
                        and hashlib.sha256(file.read_bytes()).hexdigest() == digest):
                    file.unlink()
            if not missing and self._environment_owner(intent, path) == "dispatcher":
                namespace = _canonical(path / ".ummanu-task-env")
                stat = namespace.stat()
                intent["generated_environment"] = {"device": stat.st_dev, "inode": stat.st_ino,
                                                   "owner": "ummanu-dispatcher", "workspace": workspace,
                                                   "schema_version": 1}
                intent["progress"]["environment_removal_started"] = True
                self._checkpoint_intent(intent)
                _remove_namespace(namespace)
            if allowance is not None:
                allowance.require(EFFECT_FLOOR, "Git worktree removal admission")
            # Admit Git only after exact identity, author-work and retention proof.
            intent["progress"]["removal_started"] = True
            self._checkpoint_intent(intent)
            def run_git(args, cwd):
                argv = ["git", "-C", str(cwd), *args]
                capture = getattr(self.runtime.host, "run_capture", None)
                if allowance is not None or not callable(capture):
                    # An automatic replay's removal runs within its allowance (`_run_git`).
                    return _run_git(argv)
                return capture(argv, "owned cleanup Git")
            if missing:
                removed = git_worktree.remove(run_git, repo, path,
                                             admitted_missing=lambda: self._admitted_registration(intent, repo))
            else:
                removed = git_worktree.remove(run_git, repo, path)
            if not removed:
                raise HostError("cleanup worktree removal failed; directory or registration remains")

    def _published(self, repo: Path, tip: str) -> bool:
        return bool(_git(repo, "for-each-ref", "--format=%(refname)", "--contains=" + tip,
                         "refs/remotes/", allow=False))

    def _delete_branch(self, intent: dict[str, Any], repo: Path, base: str) -> None:
        self._provenance("cleanup-before-ref-delete")
        identity = intent["identity"]
        ref, tip = identity["branch"], identity["tip"]
        if not ref:
            return
        if ref != "refs/heads/pipeline/" + intent["task"]["ref"]:
            raise Preserved("foreign branch namespace")
        current = _ref_tip(repo, ref)
        if not current:
            if intent["progress"].get("ref_delete_admitted"):
                return
            if self._shared_removal_proof(intent):
                main = _git(repo, "rev-parse", "--verify", "refs/heads/" + base)
                merged = _read_git(repo, "merge-base", "--is-ancestor", tip, main)
                if merged.returncode == 0 and self._published(repo, tip):
                    return
            raise Preserved("candidate ref missing without settlement evidence")
        if current != tip:
            raise Preserved("candidate ref changed; retained current tip " + current, verified=True)
        if any(row.get("branch") == ref and row.get("worktree") not in self._plan_removed
               for row in _registered(repo)):
            raise Preserved("branch still used by a registered worktree")
        main = _git(repo, "rev-parse", "--verify", "refs/heads/" + base)
        if self._reviewed is not None and self._planned is None:
            # A targeted replay deletes only the reviewed tip against the reviewed base tip.
            reviewed = [effect for effect in self._reviewed["effects"] if effect["effect"] == "delete-ref"]
            if [(e["ref"], e["tip"], e["base"], e["base_tip"]) for e in reviewed] != [
                    (ref, tip, "refs/heads/" + base, main)]:
                raise HostError("cleanup ref evidence differs from the reviewed manifest; nothing was deleted")
        result = _read_git(repo, "merge-base", "--is-ancestor", tip, main)
        if result.returncode == 1:
            raise Preserved("unmerged candidate ref retained at " + tip, verified=True)
        if result.returncode:
            raise HostError("cleanup merge proof unavailable")
        if not self._published(repo, tip):
            raise Preserved("unpublished candidate commits retained at " + tip, verified=True)
        if self._planned is not None:
            self._planned.append({"effect": "delete-ref", "ref": ref, "tip": tip,
                                  "base": "refs/heads/" + base, "base_tip": main,
                                  "merged": True, "published": True})
            return
        allowance = _allowance()
        if allowance is not None:
            allowance.require(EFFECT_FLOOR, "ref deletion admission")
        with self.admission(intent["task"], intent=intent):
            # The integration witness and candidate tip are locked and verified in
            # one native Git ref transaction. Never use branch -D after a stale probe.
            command = f"start\nverify refs/heads/{base} {main}\ndelete {ref} {tip}\nprepare\ncommit\n"
            intent["progress"]["ref_delete_admitted"] = {"ref": ref, "tip": tip, "integration_tip": main}
            self._checkpoint_intent(intent)
            result = _run_git(["git", "-C", str(repo), "update-ref", "--stdin"], input=command)
            if result.returncode:
                # Git names a lock it found held; cleanup never removes one, whoever left it.
                raise HostError("cleanup ref transaction refused a changed or locked tip: "
                                + result.stderr.strip()[:400])

    def _save(self, value: dict[str, Any]) -> None:
        if self._planned is None:
            targets = self.journal._targets.get()
            if targets is None:
                raise HostError("cleanup replay has no admitted intent target")
            for key in targets:
                self.journal.commit_intent(key, value["intents"][key])

    @owner_operation
    def replay_one(self, key: str) -> dict[str, Any]:
        """Attempt one intent when the eligibility policy says it is due; else return it untouched.

        Every entrypoint (automatic replay, targeted replay, close, archive and teardown) reaches its
        effects only through here, so none can bypass the cooldown.
        """
        intent, self._local.attempted = self._attempt(key)
        return intent

    def _attempt(self, key: str) -> tuple[dict[str, Any], bool]:
        """The intent after this call, and whether an attempt was reserved and run."""
        # Replay plans against the stored (migrated) form, which its checkpoints compare with.
        self.journal.migrate()
        task = self.journal.intent(key)["task"]
        # An automatic replay never queues behind another owner's lifecycle operation on the card:
        # a busy lane is that card's turn elsewhere, skipped before any reservation (`Deferred`).
        with reference_lock(self.data_dir, task["ref"], lane="lifecycle", blocking=_allowance() is None), \
                self.journal.targeted({key}):
            self._admitted = None
            # Reserved before any Git, host or board effect; not due means nothing is read or written.
            reserved = self.journal.reserve_attempt(key, self.clock())
            if reserved is None:
                return self.journal.intent(key), False
            # Only this intent: whole-journal proofs (owners, shared removal, observer order) read
            # the journal themselves, and every checkpoint writes back only this key.
            value = {"intents": {key: reserved}}
            try:
                return self._replay(value, key), True
            except HostError as exc:
                # A checkpoint/final publication can lose a same-intent race.
                # It is a retryable obligation, not a failure of the whole tick.
                try:
                    return self.journal.defer_intent(key, value["intents"][key], str(exc)), True
                except HostError as publication:
                    pending = copy.deepcopy(value["intents"][key])
                    pending.update(status="pending", reason=(str(exc) + "; " + str(publication))[:1000])
                    return pending, True
            finally:
                self._admitted = None

    def _replay(self, value: dict[str, Any], key: str) -> dict[str, Any]:
        intent = value["intents"][key]
        if intent["status"] in {"owned", "completed"} or _terminal(intent) is not None:
            return intent
        # The reservation already published this begun state; planning saves nothing.
        _begin_attempt(intent)
        if _empty_attempt(intent):
            owners = self._attempt_owners(intent)
            if owners:
                self._follow(intent, owners)
                self._save(value)
                return intent
        successor = ""
        try:
            with self.admission(intent["task"], intent=intent) as current:
                successor = current.get("successor", "")
            self._scope_fence(intent, replaced=bool(successor))
            with self.admission(intent["task"], intent=intent) if successor else contextlib.nullcontext():
                self._stop(intent, workspace_owned=not successor)
            if successor:
                # Revalidate the specific successor after the unlocked stop proof,
                # and checkpoint only the old head obligation under the effect fence.
                with self.admission(intent["task"], intent=intent):
                    intent["progress"]["heads_stopped"] = True
                    self._checkpoint_intent(intent)
                kind = "observer" if intent["task"].get("kind") == "observer" else "card attempt"
                raise Preserved(kind + " " + str(intent["record"].get("attempt_id")) + " was replaced by "
                                + successor + "; its own runs are settled and the workspace is handed "
                                "to the successor", verified=True)
            # Each stop receipt is already durable (`_stop`). The flag is published with the first
            # checkpoint that follows: before any workspace or ref effect, or with the outcome.
            intent["progress"]["heads_stopped"] = True
            if intent["disposition"] == "observer-close":
                # The head is down; only workspace removal and completion wait for the cards.
                waiting = self._unsettled_cards(intent["task"]["ref"])
                if waiting:
                    intent["progress"]["awaits_cards"] = waiting
                    raise HostError("observer closeout stopped its head; workspace removal waits for "
                                    "card cleanup: " + ", ".join(waiting))
            if not intent.get("identity"):
                self._settle_without_identity(intent, current)
            else:
                # Durable before the first Git read of the attempt and any removal it admits.
                self._save(value)
                repo, base = self._binding(intent)
                if not intent["identity"].get("workspace"):
                    self._verify_commits(intent, repo)
                self._remove_workspace(intent, repo)
                intent["progress"]["workspace_removed"] = True
                self._save(value)
                self._validate_owner(intent)
                intent["progress"]["ref_started"] = True
                self._save(value)
                self._delete_branch(intent, repo, base)
                intent["progress"]["ref_removed"] = True
            self._settle_claim(intent)
            intent["status"] = "completed"
        except Preserved as exc:
            intent["status"] = "preserved"
            intent["reason"] = str(exc)
            if intent["progress"].get("heads_stopped"):
                try:
                    with self.admission(intent["task"], intent=intent) as current:
                        self._settle_claim_admitted(intent, current)
                        intent["progress"]["preservation_verified"] = exc.verified
                        if successor and intent["task"].get("kind") != "observer":
                            # Ownership was handed off permanently. A later disappearance
                            # of the successor record can never return its workspace to us.
                            intent["progress"]["terminal"] = {"kind": "attempt-replaced",
                                                                "reason": str(exc)[:1000]}
                        elif isinstance(exc, Terminal):
                            # Ends the obligation only with its heads' stop and claim proof in hand.
                            intent["progress"]["terminal"] = {"kind": exc.kind, "reason": str(exc)[:1000]}
                        self._save(value)
                    return intent
                except Exception as settlement:  # noqa: BLE001 - a failed obligation is retained as pending evidence
                    intent["status"] = "pending"
                    intent["reason"] += "; " + str(settlement)
                    if successor:
                        intent["progress"]["heads_stopped"] = False
                        intent["progress"]["preservation_verified"] = False
        except Exception as exc:  # noqa: BLE001 - a failed obligation is retained as pending evidence
            intent["status"] = "pending"
            intent["reason"] = str(exc)[:1000]
            if successor:
                intent["progress"]["heads_stopped"] = False
                intent["progress"]["preservation_verified"] = False
        self._save(value)
        return intent

    def replay(self, *, limit: int = 20, allowance: float | None = None) -> list[dict[str, Any]]:
        """Attempt up to `limit` due intents, rotating from the stored cursor.

        With `allowance` (the production tick's automatic replay) the whole invocation, selection
        and final cursor publication included, runs on one monotonic allowance: every lock wait, Git
        child, head stop, board row lock and traversal it admits is cut to what is left, a due intent
        is reserved only while `ATTEMPT_FLOOR` remains, and one cut short stays pending with its
        durable progress. Every other caller passes none and keeps its waits as they were.
        `last_replay` describes the invocation for the tick's telemetry.
        """
        if not 1 <= limit <= 100:
            raise HostError("cleanup replay limit must be between 1 and 100")
        budget = _allowance()
        if budget is None and allowance is not None:
            budget = Allowance(allowance, clock=self.monotonic)
        writes = dict(self.journal.writes)
        report: dict[str, Any] = {"due": 0, "attempted": 0, "deferred": 0, "skipped": 0, "busy": 0, "lost": 0,
                                  "unread": 0, "cursor": "unchanged"}
        token = _ALLOWANCE.set(budget)
        try:
            with contextlib.ExitStack() as bounded:
                if budget is not None:
                    # Every board read and write of this invocation, standalone or in a transaction,
                    # waits only for what is left of the same work allowance.
                    for client in self._board_clients():
                        bounded.enter_context(client.within(
                            budget.remaining, lambda what, budget=budget: budget.defer(what)))
                return self._replay_batch(limit, budget, report)
        finally:
            _ALLOWANCE.reset(token)
            if budget is not None:
                report.update(allowance_ms=round(budget.seconds * 1000.0, 3),
                              spent_ms=round(budget.spent() * 1000.0, 3),
                              deferred_at=budget.deferred_at[:200])
            report["writes"] = {name: self.journal.writes[name] - writes[name] for name in writes}
            self.last_replay = report

    def _board_clients(self) -> list[Any]:
        """The distinct board store clients the owner's readers and writer use, that take a deadline."""
        clients: dict[int, Any] = {}
        for name in ("reader", "writer", "sprints"):
            client = getattr(getattr(self.runtime, name, None), "client", None)
            if callable(getattr(client, "within", None)):
                clients[id(client)] = client
        return list(clients.values())

    def _replay_batch(self, limit: int, budget: Allowance | None,
                      report: dict[str, Any]) -> list[dict[str, Any]]:
        read: list[str] = []
        try:
            self.journal.migrate()
            cursor = self.journal.replay_cursor()
            self.journal.selection_size = 0
            # Only due intents are yielded: cooling and terminal ones are neither replayed nor written.
            selection = self.journal.due_keys(self.clock(), cursor, read)
            due: list[str] = []
            result: list[dict[str, Any]] = []
            attempted: list[str] = []
            lost: set[str] = set()
            # A due key is not a reservation: only an attempt this owner reserved takes a slot. One
            # lost to a concurrent owner is skipped, and the next due one is tried. One skipped before
            # its reservation (no allowance left, or its card's lifecycle lane busy) consumes no
            # cooldown and stays ahead of the cursor for the next invocation.
            for key in selection:
                due.append(key)
                if budget is not None and budget.remaining() < ATTEMPT_FLOOR:
                    budget.defer("admission of the next due intent")
                    report["skipped"] += 1
                    break
                self._local.attempted = False
                deferrals = budget.deferrals if budget is not None else 0
                try:
                    intent = self.replay_one(key)
                except Deferred:
                    report["busy" if budget is not None and budget.remaining() > 0 else "skipped"] += 1
                    continue
                if not getattr(self._local, "attempted", False):
                    report["lost"] += 1
                    lost.add(key)
                    continue
                result.append(intent)
                attempted.append(key)
                if (budget is not None and intent["status"] == "pending"
                        and (budget.deferrals > deferrals or budget.remaining() <= 0)):
                    report["deferred"] += 1
                if len(attempted) == limit:
                    break
        except Deferred:
            return []
        report.update(due=len(due), attempted=len(attempted),
                      unread=max(0, self.journal.selection_size - len(read)))
        # The cursor moves past everything this invocation observed: attempted, lost, skipped at
        # the floor, refused by a busy lane or owner, or read as not due. A due intent left this way
        # was never reserved (its due time is untouched) and is reached again on the next rotation;
        # a slow first read can therefore never hold every later intent back. When the selection
        # read the whole journal every due intent was observed, and the cursor rests on the last
        # attempt as before. Written once, and only when there was due work or the selection
        # stopped short, so an idle invocation writes nothing. `cursor` says whether it advanced.
        frontier = read[-1] if read else cursor
        if attempted and not report["unread"]:
            frontier = attempted[-1]
        if not (due or report["unread"]) or frontier == cursor:
            report["cursor"] = "unchanged"
        else:
            try:
                self.journal.set_replay_cursor(frontier)
                report["cursor"] = "advanced"
            except Deferred:
                report["cursor"] = "unwritten"
        return result

    def _bindings(self, project: str | None = None) -> dict[str, dict[str, Any]]:
        bindings = getattr(self.runtime.catalog, "registered_bindings", None)
        if bindings is None:
            bindings = self.runtime.catalog.bindings
        if project is None:
            return dict(bindings)
        if project not in bindings:
            raise UnknownProject("cleanup project is not registered: " + project)
        return {project: bindings[project]}

    def _settlement_request(self, task: dict[str, Any]) -> str:
        """The disposition the inventory requests for an owned attempt of a terminal card."""
        if task.get("state") != "done" and not task.get("closed"):
            raise Preserved("card is still active")
        return "archive" if task.get("closed") else "done"

    @owner_operation
    def inventory(self, *, project: str | None = None) -> dict[str, Any]:
        """Read actual registered Git residue, including archived cards with no record.

        Old workspaces lacking exact runtime identity remain visible and preserved,
        rather than guessed from a glob.

        It writes nothing and carries the effect manifest: for every row and journaled
        intent, its target id with the effects a replay would perform, in order, or its
        refusal, and a digest the targeted replay must match. A named project is read
        alone; an unregistered one is refused before any read.
        """
        bindings = self._bindings(project)
        rows = []
        value = self.journal.read()
        recorded = value["intents"]
        manifest = []
        for name, binding in sorted(bindings.items()):
            try:
                repo = _canonical(binding["repo"])
                worktrees = _registered(repo)
                refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/pipeline/")
                for line in refs.splitlines():
                    ref, tip = line.split(" ", 1)
                    row, admitted = self._residue_row(name, binding, repo, worktrees, ref, tip, recorded)
                    if "recorded_owners" not in row:
                        row["target"] = ref + "@" + tip
                        manifest.append(self._branch_manifest(name, row, admitted, value))
                    elif "cleanup_ids" not in row:
                        # Conflicting recorded owners: a scoped refusal, never an admitted target.
                        row["target"] = ref + "@" + tip
                        manifest.append(_conflict_entry(name, row))
                    rows.append(row)
                for worktree in worktrees:
                    if not worktree.get("branch", "").startswith("refs/heads/pipeline/") and worktree.get("worktree") != str(repo):
                        row = {"project": name, "repo": str(repo), "worktree": worktree,
                               "status": "preserved", "reason": "foreign, detached or legacy workspace"}
                        row["target"] = "worktree:" + str(worktree.get("worktree", ""))
                        manifest.append(_manifest_entry(row["target"], name, "preserved", row["reason"],
                                                        [], {"worktree": worktree}))
                        rows.append(row)
            except Exception as exc:  # noqa: BLE001 - a failed obligation is retained as pending evidence
                rows.append({"project": name, "status": "pending", "reason": str(exc)[:500]})
        result: dict[str, Any] = {"intents": self.journal.summary(project=project or ""), "residue": rows}
        for key in sorted(recorded):
            if project is None or _project_intent(recorded[key], project):
                manifest.append(self._intent_manifest(key, value))
        result["manifest"] = manifest
        return result

    def _residue_row(self, project: str, binding: dict[str, Any], repo: Path, worktrees: list[dict[str, str]],
                     ref: str, tip: str, recorded: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """One branch row, and its card when branch-only ownership is admitted."""
        card_ref = ref.removeprefix("refs/heads/pipeline/")
        row = {"project": project, "repo": str(repo), "ref": ref, "tip": tip,
               "status": "preserved", "reason": "ownership not proven",
               "worktrees": [w for w in worktrees if w.get("branch") == ref]}
        base = binding.get("default_branch") or "main"
        merge = _read_git(repo, "merge-base", "--is-ancestor", tip, "refs/heads/" + base)
        row["merged"] = merge.returncode == 0 if merge.returncode in (0, 1) else None
        row["published"] = self._published(repo, tip)
        try:
            owners = {key: intent for key, intent in recorded.items()
                      if ((intent.get("identity") or {}).get("repo") == str(repo)
                          and (intent.get("identity") or {}).get("branch") == ref)
                      or (intent["task"]["ref"] == card_ref
                          and intent["task"].get("project") == project)}
            if owners:
                row["recorded_owners"] = [
                    {"cleanup_id": key, "attempt_id": intent["record"].get("attempt_id"),
                     "identity": intent.get("identity"), "status": intent["status"]}
                    for key, intent in owners.items()]
                # Recorded residue is targeted by this project's journaled intents, never by the ref.
                row["targets"] = [key for key, intent in owners.items() if _project_intent(intent, project)]
                if any(not intent.get("identity") or
                       intent["identity"]["repo"] != str(repo) or
                       intent["identity"]["branch"] != ref or
                       intent["identity"]["tip"] != tip or
                       intent["task"]["ref"] != card_ref or
                       intent["task"].get("project") != project
                       for intent in owners.values()):
                    raise Preserved("recorded ownership conflicts with current ref; retained current tip " + tip)
                row["reason"] = "retained exact owner; replay existing cleanup obligations"
                row["cleanup_ids"] = list(owners)
                if any(intent["status"] == "completed" for intent in owners.values()):
                    row["reason"] = "ref present after completed cleanup; retained recorded provenance"
                return row, None
            task = self.runtime.reader.show(card_ref)
            if task["project"] != project:
                # Another project's card: its audit is never read from this binding.
                raise Preserved("foreign or project-mismatch residue: card " + card_ref
                                + " belongs to project " + str(task["project"]))
            if not _dispatcher_claimed(self.runtime.audit.events(card_ref)):
                raise Preserved("card/project or audited claim proof missing")
            if task.get("state") != "done" and not task.get("closed"):
                raise Preserved("card is still active")
            if row["worktrees"]:
                raise Preserved("historical worktree needs exact attempt and head ownership evidence")
            row["reason"] = "owned branch-only residue; eligible for exact-tip replay"
            return row, task
        except Exception as exc:  # noqa: BLE001 - a failed obligation is retained as pending evidence
            row["reason"] = str(exc)[:500]
        return row, None

    def _adopt_branch(self, task: dict[str, Any], repo: Path, ref: str, tip: str,
                      reviewed: dict[str, Any] | None = None) -> str:
        """Journal branch-only residue; a targeted replay adopts exactly the reviewed manifest inputs."""
        identity = _identity(repo, "", ref.removeprefix("refs/heads/"))
        if reviewed is not None:
            deletion = [effect for effect in reviewed["effects"] if effect["effect"] == "delete-ref"]
            if (identity != reviewed["inputs"]["identity"] or len(deletion) != 1
                    or _ref_tip(repo, deletion[0]["base"]) != deletion[0]["base_tip"]):
                raise Refused("branch evidence changed since the manifest was read; nothing was adopted")
            identity = reviewed["inputs"]["identity"]
        return self.journal.remember(task, _archived_record(tip), identity=identity, disposition="catch-up")

    def _plan(self, value: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """Run replay's own proofs against a private copy with every effect and save disabled."""
        self._planned, self._plan_removed = [], set()
        try:
            self._replay(value, key)
            return self._planned
        finally:
            self._planned, self._plan_removed = None, set()
            self._admitted = None

    def _branch_manifest(self, project: str, row: dict[str, Any], task: dict[str, Any] | None,
                         value: dict[str, Any]) -> dict[str, Any]:
        target = row["ref"] + "@" + row["tip"]
        if task is None:
            return _manifest_entry(target, project, "preserved", row["reason"], [],
                                   {"ref": row["ref"], "tip": row["tip"]})
        try:
            identity = _identity(Path(row["repo"]), "", row["ref"].removeprefix("refs/heads/"))
        except HostError as exc:
            return _manifest_entry(target, project, "pending", str(exc)[:1000], [],
                                   {"ref": row["ref"], "tip": row["tip"]})
        value = copy.deepcopy(value)
        key, _ = self.journal.remember_into(value, task, _archived_record(row["tip"]), identity=identity,
                                            disposition="catch-up")
        return self._intent_manifest(key, value, target=target)

    def _intent_manifest(self, key: str, value: dict[str, Any], *, target: str = "") -> dict[str, Any]:
        value = copy.deepcopy(value)
        intent = value["intents"][key]
        task = intent["task"]
        inputs = {"task": {field: task.get(field) for field in ("id", "ref", "project", "kind")},
                  "attempt_id": intent["record"].get("attempt_id"), "disposition": intent["disposition"],
                  "status": intent["status"], "identity": intent.get("identity"),
                  # The stored form: the same digest before and after the v1 journal is migrated.
                  "heads": _compact_heads([_compact_run(head) for head in intent["heads"]])}
        effects: list[dict[str, Any]] = []
        try:
            if intent["status"] == "completed":
                return _manifest_entry(target or key, task.get("project", ""), "completed",
                                       intent["reason"] or "cleanup already completed", [], inputs)
            if intent["status"] == "owned":
                if task.get("kind") == "observer":
                    raise Preserved("observer obligation was never requested")
                disposition = self._settlement_request(self.runtime.reader.show(task["ref"]))
                effects.append({"effect": "request-settlement", "disposition": disposition})
                intent["disposition"], intent["status"] = disposition, "pending"
            effects += self._plan(value, key)
            outcome = "eligible" if intent["status"] == "completed" else intent["status"]
            reason = intent["reason"]
        except Exception as exc:  # noqa: BLE001 - any unreadable evidence is this target's refusal.
            outcome = "preserved" if isinstance(exc, Preserved) else "pending"
            reason = str(exc)[:1000]
        return _manifest_entry(target or key, task.get("project", ""), outcome, reason, effects, inputs)

    def _target(self, project: str, binding: dict[str, Any],
                target: str) -> tuple[dict[str, Any], Any, str] | None:
        """Recompute one target's manifest, with its admission and journal key, or None if unknown."""
        value = self.journal.read()
        intent = value["intents"].get(target)
        if intent is not None:
            if not _project_intent(intent, project):
                return None
            entry = self._intent_manifest(target, value)
            if entry["outcome"] == "completed":
                return entry, None, target
            if intent["status"] == "owned":
                if not entry["effects"] or entry["effects"][0]["effect"] != "request-settlement":
                    return entry, None, target  # Still active: nothing is requested.
                def admit() -> str:
                    task = self.runtime.reader.show(intent["task"]["ref"])
                    self.journal.request(task, self._settlement_request(task), intent["record"])
                    return target
                return entry, admit, target
            return entry, lambda: target, target
        ref, _, tip = target.rpartition("@")
        if not ref.startswith("refs/heads/pipeline/") or not tip:
            return None
        repo = _canonical(binding["repo"])
        if _ref_tip(repo, ref) != tip:
            return None
        row, task = self._residue_row(project, binding, repo, _registered(repo), ref, tip, value["intents"])
        key = _intent_key(ref.removeprefix("refs/heads/pipeline/"), _archived_record(tip)["attempt_id"])
        if "recorded_owners" in row:
            return (_conflict_entry(project, row), None, key) if "cleanup_ids" not in row else None
        entry = self._branch_manifest(project, row, task, value)
        if task is None or entry["outcome"] != "eligible":
            return entry, None, key  # Not admitted: nothing is adopted or written.
        return entry, lambda: self._adopt_branch(task, repo, ref, tip, reviewed=entry), key

    @owner_operation
    def replay_targets(self, project: str, targets: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """Replay only the named targets of one registered project, each at the manifest read.

        Before its effects, each target's manifest is recomputed; a differing digest, an
        unknown target or another project's target is refused with nothing written. Other
        intents and the replay cursor are never touched.
        """
        binding = self._bindings(project)[project]
        self.journal.migrate()
        names = [target for target, _ in targets]
        if not 1 <= len(names) <= MAX_REPLAY_TARGETS:
            raise HostError(f"cleanup replay takes 1..{MAX_REPLAY_TARGETS} explicit targets")
        if len(set(names)) != len(names):
            raise HostError("cleanup replay targets are not distinct")
        results = []
        for target, digest in targets:
            outcome: dict[str, Any] = {"target": target, "replayed": False}
            try:
                planned = self._target(project, binding, target)
            except Exception as exc:  # noqa: BLE001 - refused before any effect; other targets go on.
                results.append({**outcome, "status": "pending", "reason": str(exc)[:500]})
                continue
            if planned is None:
                results.append({**outcome, "status": "refused",
                                "reason": "unknown target or not a cleanup target of project " + project})
                continue
            entry, admit, key = planned
            if entry["digest"] != digest:
                results.append({**outcome, "status": "refused", "digest": entry["digest"],
                                "reason": "manifest digest differs; read the inventory again"})
                continue
            if admit is None:
                results.append({**outcome, "status": entry["outcome"], "reason": entry["reason"]})
                continue
            # Saves write back only this target's intent; the rest of the journal stays as stored.
            with self.journal.targeted({key}):
                try:
                    if admit() != key:
                        raise HostError("cleanup target key differs from its admission")
                except Refused as exc:
                    results.append({**outcome, "status": "refused", "reason": str(exc)})
                    continue
                except HostError as exc:
                    results.append({**outcome, "status": "pending", "reason": str(exc)[:500]})
                    continue
                self._reviewed = entry
                self._local.attempted = False
                try:
                    intent = self.replay_one(key)
                finally:
                    self._reviewed = None
            if not getattr(self._local, "attempted", False):
                terminal = _terminal(intent)
                results.append({**outcome, "cleanup_id": key, "status": intent["status"],
                                "reason": ("terminal " + terminal["kind"] + ": " + intent["reason"] if terminal
                                           else "not due: next attempt at " + str(_next_attempt(intent))
                                           if intent["status"] in {"pending", "preserved"}
                                           else intent["reason"]),
                                "next_attempt_at": None if terminal else _next_attempt(intent),
                                "progress": intent["progress"]})
                continue
            results.append({**outcome, "replayed": True, "cleanup_id": key, "status": intent["status"],
                            "reason": intent["reason"], "progress": intent["progress"]})
        return results


MAX_REPLAY_TARGETS = 20


class UnknownProject(HostError):
    """A residue command named a project that is not registered; nothing was read."""


class Refused(HostError):
    """A replay target's evidence changed since its manifest was read; nothing was written."""


def _conflict_entry(project: str, row: dict[str, Any]) -> dict[str, Any]:
    return _manifest_entry(row["ref"] + "@" + row["tip"], project, "preserved", row["reason"], [],
                           {"ref": row["ref"], "tip": row["tip"]})


def _archived_record(tip: str) -> dict[str, Any]:
    return {"attempt_id": "archived-branch:" + tip, "worker": "", "workspace": ""}


def _dispatcher_claimed(events: list[dict[str, Any]]) -> bool:
    """An audited dispatcher claim: the board's `card.started`, or an older form."""
    from ummanu.board.models import EventKind
    from ummanu.board.roles import Role
    for event in events:
        actor = event.get("actor") or {}
        if event.get("kind") == "claimed":
            return True
        if actor.get("role") == Role.DISPATCHER.value and (
                event.get("kind") == EventKind.CARD_STARTED.value
                or (event.get("payload") or {}).get("to") == "in_progress"):
            return True
    return False


def _manifest_entry(target: str, project: str, outcome: str, reason: str,
                    effects: list[dict[str, Any]], inputs: dict[str, Any]) -> dict[str, Any]:
    entry = {"target": target, "project": project, "outcome": outcome, "reason": reason,
             "effects": effects, "inputs": inputs}
    entry["digest"] = hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()
    return entry


class Preserved(HostError):
    """Owned work deliberately retained; distinct from a retryable failed effect."""

    def __init__(self, reason: str, *, verified: bool = False):
        super().__init__(reason)
        self.verified = verified


class Terminal(Preserved):
    """A dead end decided from current facts: the obligation ends without removal or retention proof.

    Never verified retention and never a completed removal; its residue stays retained and visible.
    """

    def __init__(self, kind: str, reason: str):
        super().__init__(reason, verified=False)
        assert kind in TERMINAL_KINDS
        self.kind = kind
