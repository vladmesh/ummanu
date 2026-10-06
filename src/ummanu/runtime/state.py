"""Watermark and run lock shared by every triggered agent, one state dir per agent.

The watermark records how far each source was processed and advances only after the agent's
durable output is committed, so a crash re-processes rather than drops. The lock is an `flock` (a
killed run cannot keep it); the file records holder pid and start time for diagnostics. State root
is `TA_STATE` or `~/ummanu-data/automation-state`, then `/<agent>`.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

STATE_ROOT = Path(os.environ.get("TA_STATE", str(Path.home() / "ummanu-data" / "automation-state")))

# Precheck exit codes: 0 dispatches; the explicit skip/defer codes are clean; all others fail.
# Skip is not 1, Python's uncaught-exception exit code.
PRECHECK_SKIP = 100

# Board unavailability is retryable and distinct from a clean skip.
PRECHECK_BOARD_UNREACHABLE = 101


class BoardUnavailable(RuntimeError):
    """The board store is not reachable yet; nothing was read or written.

    A precheck reports it as PRECHECK_BOARD_UNREACHABLE (a deferred tick), not as its own failure.
    """


# Durable settlement is in progress; exit cleanly without racing live-head cleanup.
PRECHECK_DEFERRED = 102


def append_line_durable(path: Path, line: str) -> None:
    """Append one line to an append-only journal: `O_APPEND`, one `write()`, then `fsync`.

    Never reads or replaces the file, so concurrent appends survive. A short write raises.
    """
    if "\n" in line:
        raise ValueError("a journal line must not contain a newline")
    data = (line + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError(f"short append to {path}: {written} of {len(data)} bytes")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_state_atomic(
    writes: list[tuple[Path, str]],
    *,
    removes: list[Path] | None = None,
) -> None:
    """Publish one triggered-agent state transition, or restore every changed path on failure.

    Replacements are staged first, so a failed write leaves the prior files intact, and a failed
    replace or removal restores every affected file.
    """
    removals = removes or []
    paths = [path for path, _ in writes] + removals
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((path, Path(temporary)))
        for path, staged_file in staged:
            os.replace(staged_file, path)
        for path in removals:
            path.unlink(missing_ok=True)
    except OSError:
        for path in reversed(paths):
            _restore_state_file(path, before[path])
        raise
    finally:
        for _, staged_file in staged:
            staged_file.unlink(missing_ok=True)


def _restore_state_file(path: Path, before: bytes | None) -> None:
    if before is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(before)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class AgentState:
    """Per-agent watermark + lock under STATE_ROOT/<agent>/."""

    def __init__(self, agent: str, state_dir: Path | None = None):
        self.agent = agent
        self.dir = Path(state_dir) if state_dir is not None else STATE_ROOT / agent
        self.watermark_file = self.dir / "watermark.json"
        self.pending_file = self.dir / "pending.json"
        self.lockfile = self.dir / "lock"
        self.head_profile_file = self.dir / "head_profile.json"
        self.terminal_handle_file = self.dir / "terminal_handle.json"
        self.terminal_generation_file = self.dir / "terminal_generation.json"
        self.head_run_file = self.dir / "head_run.json"
        self.active_report_file = self.dir / "active_report.json"

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def load_watermark(self) -> dict:
        if not self.watermark_file.is_file():
            return {}
        try:
            return json.loads(self.watermark_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save_watermark(self, mark: dict) -> None:
        self.ensure_dir()
        tmp = self.watermark_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mark, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.watermark_file)

    def load_head_profile(self) -> str | None:
        """The heads.toml profile the live terminal was launched with, or None if never recorded.

        A warm terminal keeps its start profile, so idle reuse checks this, not the preferred head.
        """
        if not self.head_profile_file.is_file():
            return None
        try:
            return json.loads(self.head_profile_file.read_text(encoding="utf-8")).get("profile")
        except json.JSONDecodeError:
            return None

    def save_head_profile(self, profile: str | None) -> None:
        """Record the profile a freshly (re)spawned terminal runs on; never called on warm reuse."""
        self.ensure_dir()
        tmp = self.head_profile_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"profile": profile}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.head_profile_file)

    def load_terminal_handle(self) -> str | None:
        """The Orca terminal handle last created for this singleton agent."""
        if not self.terminal_handle_file.is_file():
            return None
        try:
            return json.loads(self.terminal_handle_file.read_text(encoding="utf-8")).get("handle")
        except json.JSONDecodeError:
            return None

    def next_terminal_generation(self) -> int:
        """Bump and return the per-agent terminal generation, stamped into a terminal's teardown trailer.

        Never reset, and kept apart from terminal_handle.json so teardown cannot roll it back: a late
        finalizer compares its trailer's generation with `load_terminal_generation` and must never
        match, and so stop, a replacement terminal.
        """
        cur = 0
        if self.terminal_generation_file.is_file():
            try:
                cur = int(
                    json.loads(self.terminal_generation_file.read_text(encoding="utf-8")).get("counter", 0)
                )
            except (json.JSONDecodeError, ValueError, TypeError):
                cur = 0
        nxt = cur + 1
        self.ensure_dir()
        tmp = self.terminal_generation_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"counter": nxt}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.terminal_generation_file)
        return nxt

    def load_terminal_generation(self) -> int | None:
        """The generation of the current terminal in terminal_handle.json, or None.

        A finalizer stops the live terminal only when this equals its own trailer's generation.
        """
        if not self.terminal_handle_file.is_file():
            return None
        try:
            return json.loads(self.terminal_handle_file.read_text(encoding="utf-8")).get("generation")
        except json.JSONDecodeError:
            return None

    def load_terminal_created_at(self) -> float | None:
        """When this agent's terminal was last actually spawned (epoch seconds), or None.

        Guards the gap before a new terminal shows in `terminal list`, so a second dispatch does not
        read "no terminal" as "never spawned" and create a duplicate.
        """
        if not self.terminal_handle_file.is_file():
            return None
        try:
            return json.loads(self.terminal_handle_file.read_text(encoding="utf-8")).get("created_at")
        except json.JSONDecodeError:
            return None

    def save_terminal_handle(
        self, handle: str | None, created_at: float | None = None, generation: int | None = None
    ) -> None:
        """Record the terminal handle from the latest spawn (Codex may retitle its tab, so titles
        alone cannot identify the singleton).

        `created_at` is passed only by an actual spawn; warm reuse drops it, since the terminal is
        then already visible. `generation` comes from `next_terminal_generation` for an ephemeral
        agent's fresh create. `handle=None` deletes the record but never the generation counter.
        """
        self.ensure_dir()
        if not handle:
            try:
                self.terminal_handle_file.unlink()
            except FileNotFoundError:
                pass
            return
        payload: dict[str, object] = {"handle": handle}
        if created_at is not None:
            payload["created_at"] = created_at
        if generation is not None:
            payload["generation"] = generation
        tmp = self.terminal_handle_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.terminal_handle_file)

    def load_head_run(self) -> dict | None:
        """The `HeadRun` this agent's last tick raised on a product-owned backend, or None.

        A `local-pty` head outlives its tick with no session store, so the next tick hands this to
        `LocalPtyHeadRuntime.start`, which then refuses rather than raising a second head. An
        unparseable record reads as none, so a corrupt file cannot fence the role off duty forever.
        """
        if not self.head_run_file.is_file():
            return None
        try:
            record = json.loads(self.head_run_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return record if isinstance(record, dict) else None

    def save_head_run(self, run: dict | None) -> None:
        """Record the head this tick raised, or forget it (`run=None`); written via temp file and replace."""
        self.ensure_dir()
        if run is None:
            try:
                self.head_run_file.unlink()
            except FileNotFoundError:
                pass
            return
        tmp = self.head_run_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.head_run_file)

    def load_active_report(self) -> dict | None:
        if not self.active_report_file.is_file():
            return None
        try:
            data = json.loads(self.active_report_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def save_active_report(self, reference: str | None, terminal_handle: str | None) -> None:
        self.ensure_dir()
        if not reference or not terminal_handle:
            self.clear_active_report(reference)
            return
        tmp = self.active_report_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"reference": reference, "terminal_handle": terminal_handle}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.active_report_file)

    def clear_active_report(self, reference: str | None = None) -> None:
        if reference is not None:
            current = self.load_active_report()
            if current and current.get("reference") != reference:
                return
        try:
            self.active_report_file.unlink()
        except FileNotFoundError:
            pass

    def log_run(self, event: str, **fields: object) -> None:
        """Append a run-telemetry line to runs.jsonl; best effort, errors are swallowed."""
        try:
            self.ensure_dir()
            rec = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
            with (self.dir / "runs.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive run lock; raises SystemExit if another run of this agent holds it.

        `flock` is the mutex, dropped by the kernel when the holder dies. The pid/start record is
        diagnostics, and still refuses a live holder of the legacy bare-pid form without `flock`.
        Logs `lock-reclaimed` on reclaiming a dead holder's file, `lock-refused` on refusal.
        """
        self.ensure_dir()
        fd = self._acquire_lockfile()
        try:
            yield
        finally:
            try:
                if _same_file(self.lockfile, fd):
                    self.lockfile.unlink()
            except FileNotFoundError:
                pass
            finally:
                os.close(fd)

    def _acquire_lockfile(self) -> int:
        for _ in range(_LOCK_ACQUIRE_ATTEMPTS):
            fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self._refuse(_read_lock_record(fd))
                # A releasing holder unlinks the path before closing; a lock on an unlinked
                # inode guards nothing, so start over on whatever the path names now.
                if not _same_file(self.lockfile, fd):
                    os.close(fd)
                    continue
                record = _read_lock_record(fd)
                if record.get("raw"):
                    if _holder_alive(record, os.fstat(fd).st_mtime):
                        self._refuse(record)
                    self.log_run(
                        "lock-reclaimed",
                        stale_pid=record.get("pid"),
                        recorded_start=record.get("start"),
                        lock_age_s=round(time.time() - os.fstat(fd).st_mtime, 3),
                        reclaimer_pid=os.getpid(),
                    )
                body = json.dumps(
                    {"pid": os.getpid(), "start": _process_start(os.getpid()), "source": _lock_source()}
                ).encode()
                os.ftruncate(fd, 0)
                os.pwrite(fd, body, 0)
                os.fsync(fd)
                return fd
            except BaseException:
                with suppress(OSError):
                    os.close(fd)
                raise
        raise SystemExit(f"{self.agent}: lock file keeps changing ({self.lockfile})")

    def _refuse(self, record: dict) -> NoReturn:
        holder = record.get("pid") if record.get("pid") is not None else (record.get("raw") or "?")
        self.log_run("lock-refused", holder_pid=holder, recorded_start=record.get("start"))
        raise SystemExit(f"{self.agent}: another run holds the lock ({self.lockfile}, pid {holder})")


_LOCK_ACQUIRE_ATTEMPTS = 8


def _same_file(path: Path, fd: int) -> bool:
    try:
        on_path = os.stat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (on_path.st_dev, on_path.st_ino) == (held.st_dev, held.st_ino)


def _read_lock_record(fd: int) -> dict:
    """Parse a lock record: JSON `{"pid", "start", "source"}` or the legacy bare decimal pid."""
    try:
        raw = os.pread(fd, 4096, 0).decode("utf-8", errors="replace").strip()
    except OSError:
        raw = ""
    record: dict = {"raw": raw}
    if raw.isdigit():
        record["pid"] = int(raw)
        return record
    try:
        parsed = json.loads(raw)
    except ValueError:
        return record
    if isinstance(parsed, dict) and isinstance(parsed.get("pid"), int):
        record["pid"] = parsed["pid"]
        if isinstance(parsed.get("start"), int):
            record["start"] = parsed["start"]
    return record


def _holder_alive(record: dict, written_at: float) -> bool:
    """Whether the recorded holder still runs: the pid exists and is the same process.

    A recorded start time must match; for the legacy form, a process started after the lock file was
    written is a reused pid.
    """
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    start = _process_start(pid)
    if "start" in record:
        return start is None or start == record["start"]
    started_at = _process_started_at(start)
    return started_at is None or started_at <= written_at + 1


def _process_start(pid: int) -> int | None:
    """Start time of `pid` in clock ticks since boot (`/proc/<pid>/stat` field 22), if known."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(stat[stat.rindex(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _process_started_at(start: int | None) -> float | None:
    if start is None:
        return None
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return int(line.split()[1]) + start / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return None
    return None


def _lock_source() -> str:
    return " ".join(sys.argv)[:200]
