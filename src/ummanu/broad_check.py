"""Workspace-local structured receipt for one broad check run.

A worker runs a broad suite once per report generation. This module runs the broad command once,
keeps the combined output visible while it runs, and writes what the run actually decided into a
bounded local artifact: the command and its digest, where it ran and which project it imported,
when it started and how long it took, the process exit status, the parsed verdict and counts, and
a bounded diagnostic tail.

The receipt is evidence about content, not about a wall clock: it records the checkout's content
identity (the git tree object id the worktree would commit to). Everything else
fails closed — an incomplete run, a corrupt or truncated artifact, a checkout with no resolvable
identity, or a receipt for other content is not usable evidence, and the reader is told which.

A receipt only reports the project a check imported when the check process itself said so, which
is why the standard shape is a module this wrapper launches; an arbitrary shell may `cd` elsewhere
before any work starts, so that shape records no import. That shape also makes the candidate the
project by construction: the wrapper puts the candidate's own import roots at the front of the
check process's `sys.path` before anything imports the project, ahead of whatever `PYTHONPATH` and
whatever editable install the launching head happened to carry (issue:8b39e60e4df361c6138e).

The runner's verdict is scanned off the stream while it goes past rather than reconstructed from
the diagnostic tail, because output printed after a summary must not be able to erase it.

This is deliberately not the exact-SHA gate receipt in ``dispatch.gate_receipt``: that one is
machinery-owned attestation that travels downstream, this one is a worker's own note-to-self.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from signal import NSIG
from typing import IO, Any

from ummanu._fsutil import write_text_atomic
from ummanu.runtime.role_env import dispatcher_workspace_namespace

SCHEMA_VERSION = 1
RECEIPT_DIR_NAME = Path("state") / "checks"
#: In a dispatcher workspace the receipt is the pipeline's own output, kept in its owned namespace.
DISPATCHER_RECEIPT_DIR_NAME = Path("checks")
#: Bound the artifact's only unbounded input by bytes and lines.
TAIL_BYTES = 8192
TAIL_LINES = 120
MAX_COMMAND_CHARS = 4096
#: Bound parser state; runner summaries and verdict details fit within these limits.
_MAX_LINE_BYTES = 4096
#: The widest normal exit status a POSIX process can hand back.
_MAX_EXIT_STATUS = 255
_MAX_DETAIL_CHARS = 512
_READ_CHUNK = 65536
_GIT_TIMEOUT = 60

_STATUS_COMPLETE = "complete"
_STATUS_INCOMPLETE = "incomplete"
_VERDICT_PASSED = "passed"
_VERDICT_FAILED = "failed"
_VERDICT_UNKNOWN = "unknown"

_RAN_RE = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s$", re.MULTILINE)
_OK_RE = re.compile(r"^OK(?: \((?P<detail>[^)]*)\))?\s*$", re.MULTILINE)
_FAILED_RE = re.compile(r"^FAILED \((?P<detail>[^)]*)\)\s*$", re.MULTILINE)
_DETAIL_RE = re.compile(r"(?P<name>[a-z][a-z ]*)=(?P<count>\d+)")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class BroadCheckError(Exception):
    """A refusal to produce or trust a receipt; the message names what failed closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ContentIdentity:
    """What the receipt is a receipt *about*: the exact content of a checkout.

    One git tree object id stands for that content. Committing a dirty worktree unchanged moves
    HEAD but not the tree, so a receipt taken before the commit still describes the checkout.
    """

    tree_sha: str

    @property
    def resolved(self) -> bool:
        return bool(self.tree_sha)

    def as_dict(self) -> dict[str, str]:
        return {"tree_sha": self.tree_sha}

    def matches(self, other: ContentIdentity) -> bool:
        return self.resolved and other.resolved and self.as_dict() == other.as_dict()


#: Give the candidate import precedence, then record what the check process actually imported.
#
# The order of these two steps is the whole fix for issue:8b39e60e4df361c6138e. This block used to
# import the configured package first and only afterwards *append* the workspace root to
# `sys.path`, which could never make a src-layout candidate importable and would have been too late
# if it could: by then the package object was already bound. Every head runs with
# `PYTHONPATH=$UMMANU_REPO/src` (see `ummanu/runtime/launch_prefix.py`) and every
# worktree shares one venv holding an editable install of the production checkout, so the check
# process imported production sources, ran the candidate's test files against them, printed OK and
# exited 0. `candidate_import_refusal()` then honestly refused the receipt, which made `--reuse`
# dead for this project and made every round pay for the full suite again.
#
# So the candidate's own import roots go to the FRONT of `sys.path`, ahead of any inherited
# control-plane `PYTHONPATH`, and they go there before the import. Provenance is still *observed*
# rather than asserted: the record says what `importlib` actually returned, and the roots are
# recorded as what the process was told to prefer, not as a claim about where the import landed.
_PROVENANCE_BOOTSTRAP = """\
import importlib, json, os, runpy, sys

_record, _module, _package, _roots = sys.argv[1:5]
sys.argv = [_module, *sys.argv[5:]]
_import_roots = [_entry for _entry in _roots.split(os.pathsep) if _entry]
for _entry in _import_roots:
    while _entry in sys.path:
        sys.path.remove(_entry)
sys.path[:0] = _import_roots
try:
    _project = importlib.import_module(_package)
    _imported = getattr(_project, "__file__", "") or ""
except Exception:
    _imported = ""
with open(_record, "w", encoding="utf-8") as _handle:
    json.dump(
        {
            "python": sys.executable,
            "environment_prefix": sys.prefix,
            "cwd": os.getcwd(),
            "imported_package": _package,
            "imported_project": _imported,
            "import_roots": _import_roots,
        },
        _handle,
    )
runpy.run_module(_module, run_name="__main__", alter_sys=True)
"""


def candidate_import_roots(root: Path) -> list[str]:
    """The paths a check process must prefer so that "the project" means *this* checkout.

    Two entries, and the order matters. The workspace root comes first because that is what an
    ordinary `python -m` in the checkout already puts at `sys.path[0]`, so a flat-layout candidate
    keeps behaving exactly as it did before this list existed. `src/` follows, because a src-layout
    project (Ummanu itself, `src/ummanu/`) has nothing importable at its root and the root
    entry alone silently resolves the package from wherever else it happens to be installed --
    which is the defect in issue:8b39e60e4df361c6138e.

    Both entries are handed over unconditionally, whether or not they exist: a candidate that has
    no `src/` is not a candidate that should start importing one from somewhere else, and a
    non-existent `sys.path` entry costs a failed stat.
    """
    resolved = Path(root).resolve()
    return [str(resolved), str(resolved / "src")]


_CHECK_SET_SCHEMA = 1
_SHAPE_MODULE = "module"
_SHAPE_SHELL = "shell"
_ORIGIN_CHECK_PROCESS = "check-process"
_ORIGIN_UNOBSERVABLE = "unobservable"
_UNOBSERVED_PROVENANCE = {
    "origin": _ORIGIN_UNOBSERVABLE,
    "python": "",
    "environment_prefix": "",
    "cwd": "",
    "imported_package": "",
    "imported_project": "",
    "inside_workspace": False,
    "import_roots": [],
}


@dataclass(frozen=True)
class CheckSpec:
    """One accepted shape of a broad check, and what its receipt may claim.

    ``module`` is the documented standard shape: this wrapper builds the argv itself and runs the
    suite in a process that reports its own import provenance. ``shell`` accepts any command and
    attests nothing about imports, because a shell command may change directory or import environment
    between the wrapper and the interpreter that ends up doing the work.
    """

    shape: str
    module: str = ""
    module_args: tuple[str, ...] = ()
    command: str = ""
    interpreter: str = sys.executable
    import_package: str = "ummanu"

    @classmethod
    def for_module(
        cls,
        module: str,
        args: Iterable[str] = (),
        *,
        interpreter: str = sys.executable,
        import_package: str = "ummanu",
    ) -> CheckSpec:
        module = module.strip()
        if not module or module.startswith("-"):
            raise BroadCheckError("empty_module", "a module check needs a module name")
        interpreter = interpreter.strip()
        import_package = import_package.strip()
        if not interpreter:
            raise BroadCheckError("empty_interpreter", "a module check needs an interpreter")
        if not import_package:
            raise BroadCheckError("empty_import_package", "a module check needs an import package")
        return cls(
            _SHAPE_MODULE,
            module,
            tuple(args),
            interpreter=interpreter,
            import_package=import_package,
        )

    @classmethod
    def for_shell(cls, command: str) -> CheckSpec:
        if not command.strip():
            raise BroadCheckError("empty_command", "a broad check needs a command")
        return cls(_SHAPE_SHELL, command=command)

    @property
    def check_set(self) -> dict[str, object]:
        """The canonical structured identity of this check: what is digested and stored.

        A rendered command line cannot carry an argument vector faithfully — `--module-arg 'one two'` and
        `--module-arg one --module-arg two` render identically while running different checks — so the
        argument vector is kept as a list and every route that keys, looks up or validates a receipt uses
        this representation rather than the display string.
        """
        if self.shape == _SHAPE_MODULE:
            return {
                "schema": _CHECK_SET_SCHEMA,
                "shape": _SHAPE_MODULE,
                "module": self.module,
                "args": list(self.module_args),
                "interpreter": self.interpreter,
                "import_package": self.import_package,
            }
        return {"schema": _CHECK_SET_SCHEMA, "shape": _SHAPE_SHELL, "command": self.command}

    @property
    def digest(self) -> str:
        return check_set_digest(self.check_set)

    @property
    def identity(self) -> str:
        """A human rendering for reports and logs.  Never used to key or match a receipt."""
        if self.shape == _SHAPE_MODULE:
            return " ".join(["python", "-m", self.module, *self.module_args])
        return self.command

    @property
    def attests_provenance(self) -> bool:
        return self.shape == _SHAPE_MODULE

    def argv(self, record: Path | None, root: Path) -> list[str]:
        if self.shape == _SHAPE_MODULE:
            return [
                self.interpreter,
                "-c",
                _PROVENANCE_BOOTSTRAP,
                str(record),
                self.module,
                self.import_package,
                os.pathsep.join(candidate_import_roots(root)),
                *self.module_args,
            ]
        return ["bash", "-lc", self.command]

    def displayed_argv(self) -> list[str]:
        if self.shape == _SHAPE_MODULE:
            return [
                self.interpreter,
                "-c",
                "<provenance bootstrap>",
                "<provenance record>",
                self.module,
                self.import_package,
                "<candidate import roots>",
                *self.module_args,
            ]
        return ["bash", "-lc", self.command]


def as_spec(check: CheckSpec | str) -> CheckSpec:
    return check if isinstance(check, CheckSpec) else CheckSpec.for_shell(check)


def receipt_dir(root: Path) -> Path:
    """Where `root`'s receipts live: the dispatcher's namespace when it owns one, else `state/checks`.

    Writer and every reader resolve the directory here, so a dispatcher workspace's receipt is
    found where it was written and owned cleanup removes it with the namespace.
    """
    namespace = dispatcher_workspace_namespace(root)
    if namespace is not None:
        return namespace / DISPATCHER_RECEIPT_DIR_NAME
    return Path(root) / RECEIPT_DIR_NAME


def check_set_digest(check_set: Mapping[str, object]) -> str:
    """Digest the canonical check-set, the way the mechanical gate digests its own check set."""
    canonical = json.dumps(check_set, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8", "surrogateescape")).hexdigest()


def receipt_path(root: Path, check: CheckSpec | str) -> Path:
    return receipt_dir(root) / f"broad-{as_spec(check).digest[:16]}.json"


def _git(
    root: Path, args: list[str], env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=None if env is None else {**os.environ, **env},
        )
    except (OSError, subprocess.SubprocessError):
        return None


def content_identity(root: Path) -> ContentIdentity:
    """Name the checkout's content by a git tree object id, not by the commit it sits on.

    A worker reports from a dirty worktree far more often than from a clean one, and it commits that
    same content moments later; a HEAD object id would call those two checkouts different when they
    are byte for byte the same. So the identity is the tree the worktree *would* commit to: every
    tracked path as it stands on disk plus every untracked, non-ignored file, staged into a scratch
    index and written out with ``write-tree``. On a clean checkout that is exactly ``HEAD^{tree}``.

    The scratch index starts as a copy of the real one, so a tracked path that also matches an ignore
    rule keeps counting; ignored *untracked* paths (the receipt directory among them) are excluded by
    construction. The real index is never touched. An unresolvable identity never matches anything.
    """
    located = _git(root, ["rev-parse", "--git-path", "index"])
    if located is None or located.returncode != 0:
        return ContentIdentity("")
    real_index = Path(root) / located.stdout.strip()
    with tempfile.TemporaryDirectory() as scratch_dir:
        scratch = Path(scratch_dir) / "index"
        try:
            if real_index.exists():
                # Preserve index metadata to keep Git's racy-clean detection intact.
                shutil.copy2(real_index, scratch)
        except OSError:
            return ContentIdentity("")
        env = {"GIT_INDEX_FILE": str(scratch)}
        staged = _git(root, ["add", "-A"], env=env)
        if staged is None or staged.returncode != 0:
            return ContentIdentity("")
        written = _git(root, ["write-tree"], env=env)
    if written is None or written.returncode != 0:
        return ContentIdentity("")
    return ContentIdentity(written.stdout.strip())


def _read_provenance(record: Path | None, root: Path) -> dict[str, object]:
    """Read what the check process said about its own import, or claim nothing at all.

    A separate preflight probe reports what *it* would import, while the check may run somewhere else
    entirely. Only the record written from inside the running check counts, and its absence is
    reported as unobserved rather than filled in from the wrapper's own environment.
    """
    if record is None:
        return dict(_UNOBSERVED_PROVENANCE)
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return dict(_UNOBSERVED_PROVENANCE)
    if not isinstance(payload, Mapping):
        return dict(_UNOBSERVED_PROVENANCE)
    imported = str(payload.get("imported_project") or "")
    recorded_roots = payload.get("import_roots")
    if not isinstance(recorded_roots, list):
        recorded_roots = []
    inside = False
    if imported:
        try:
            inside = Path(imported).resolve().is_relative_to(Path(root).resolve())
        except (OSError, ValueError):
            inside = False
    return {
        "origin": _ORIGIN_CHECK_PROCESS,
        "python": str(payload.get("python") or ""),
        "environment_prefix": str(payload.get("environment_prefix") or ""),
        "cwd": str(payload.get("cwd") or ""),
        "imported_package": str(payload.get("imported_package") or ""),
        "imported_project": imported,
        "inside_workspace": inside,
        # What the check process was told to prefer, as it saw it. This is not evidence about where
        # the import landed -- `imported_project` above is, and `candidate_import_refusal()` reads
        # only that -- but it is what a reader needs to explain a refusal that should have been
        # impossible: roots the process never received, or received and could not use.
        "import_roots": [str(entry) for entry in recorded_roots],
    }


class _BoundedTail:
    """Keep the last bytes of a stream without keeping the stream."""

    def __init__(self, limit: int = TAIL_BYTES) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False
        self.total_bytes = 0
        self.total_lines = 0

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        self.total_lines += chunk.count(b"\n")
        self._buffer.extend(chunk)
        if len(self._buffer) > self._limit:
            del self._buffer[: len(self._buffer) - self._limit]
            self.truncated = True

    def text(self) -> str:
        body = bytes(self._buffer).decode("utf-8", "replace")
        lines = body.splitlines()
        if self.truncated and lines:
            # The first retained line is almost certainly cut mid-way; a partial line reads as a
            # real one in a report, so drop it rather than quote half a traceback.
            lines = lines[1:]
        if len(lines) > TAIL_LINES:
            lines = lines[-TAIL_LINES:]
            self.truncated = True
        return "\n".join(lines)


class _SummaryScanner:
    """Read the runner's verdict off the stream as it goes by, in constant memory.

    The verdict must not depend on the diagnostic tail: a runner prints ``OK (skipped=8)`` and then
    an ``atexit`` handler or a subprocess can print megabytes after it, pushing the summary out of any
    bounded tail. So the scanner keeps only what a summary can be and forgets every other line.
    """

    def __init__(self) -> None:
        self._carry = bytearray()
        self._dropping = False
        self._tests: int | None = None
        self._runner_duration: float | None = None
        self._summary = ""
        self._detail = ""

    def feed(self, chunk: bytes) -> None:
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            if end < 0:
                break
            self._complete(chunk[start:end])
            start = end + 1
        rest = chunk[start:]
        if rest:
            if len(self._carry) + len(rest) > _MAX_LINE_BYTES:
                # No runner summary is this long, so the line is dropped rather than buffered;
                # the scanner resynchronises at the next newline.
                self._carry.clear()
                self._dropping = True
            else:
                self._carry.extend(rest)

    def _complete(self, line: bytes) -> None:
        if self._dropping:
            self._dropping = False
            self._carry.clear()
            return
        if self._carry:
            line = bytes(self._carry) + line
            self._carry.clear()
        if len(line) > _MAX_LINE_BYTES:
            return
        self._line(line.decode("utf-8", "replace").rstrip("\r"))

    def _line(self, text: str) -> None:
        ran = _RAN_RE.match(text)
        if ran is not None:
            self._tests = int(ran.group(1))
            self._runner_duration = float(ran.group(2))
            return
        failed = _FAILED_RE.match(text)
        if failed is not None:
            self._summary, self._detail = "FAILED", failed.group("detail")[:_MAX_DETAIL_CHARS]
            return
        ok = _OK_RE.match(text)
        if ok is not None:
            self._summary = "OK"
            self._detail = (ok.group("detail") or "")[:_MAX_DETAIL_CHARS]

    def finish(self) -> dict[str, object]:
        if self._carry and not self._dropping:
            self._line(bytes(self._carry).decode("utf-8", "replace").rstrip("\r"))
        self._carry.clear()
        parsed: dict[str, object] = {}
        if self._tests is not None:
            parsed["tests"] = self._tests
        if self._runner_duration is not None:
            parsed["runner_duration_seconds"] = self._runner_duration
        if self._summary:
            parsed["summary"] = self._summary
        for match in _DETAIL_RE.finditer(self._detail):
            parsed[match.group("name").strip().replace(" ", "_")] = int(match.group("count"))
        return parsed


def parse_unittest_summary(text: str) -> dict[str, object]:
    """Parse the runner's own verdict where it prints one; absence is not failure."""
    scanner = _SummaryScanner()
    scanner.feed(text.encode("utf-8", "surrogateescape"))
    return scanner.finish()


def _receipt_digest(payload: Mapping[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key != "receipt_digest"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _assert_ignored(root: Path, path: Path) -> None:
    """Refuse to write a receipt anywhere git would offer to commit it."""
    inside = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    ignored = _git(root, ["check-ignore", "-q", str(path)])
    if ignored is None or ignored.returncode != 0:
        raise BroadCheckError(
            "receipt_not_ignored",
            f"{path} is not git-ignored; a broad-check receipt must never be committable",
        )


def run_broad_check(
    check: CheckSpec | str,
    *,
    root: Path,
    stream: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    record_receipt: bool = True,
) -> tuple[int, dict[str, object]]:
    """Run one broad check, keep its combined output visible, and write its receipt.

    A selector uses record_receipt=False: the same process observation stays in memory, and
    neither the receipt namespace nor a full-round artifact is touched.

    Returns the process's own exit status alongside the observation, so the receipt never becomes a
    second, softer answer to what the check decided.
    """
    spec = as_spec(check)
    if len(json.dumps(spec.check_set)) > MAX_COMMAND_CHARS:
        raise BroadCheckError("command_too_long", f"command exceeds {MAX_COMMAND_CHARS} characters")
    root = Path(root)
    if not root.is_dir():
        raise BroadCheckError("missing_root", f"{root} is not a directory")
    target = receipt_path(root, spec) if record_receipt else None
    if target is not None:
        _assert_ignored(root, target)
    environment = dict(os.environ if env is None else env)
    if spec.attests_provenance:
        environment = _normalize_pythonpath_for_child(environment)
    sink = sys.stderr if stream is None else stream

    with tempfile.TemporaryDirectory(prefix="ummanu-broad-check-") as scratch:
        # Keep provenance outside the workspace whose contents the receipt names.
        record = Path(scratch) / "provenance.json" if spec.attests_provenance else None
        environment.pop("UMMANU_TEST_TIMING_RECORD", None)
        if record is not None:
            environment["UMMANU_TEST_TIMING_RECORD"] = str(record.with_name("timing.json"))
        return _run_and_record(
            spec,
            root=root,
            target=target,
            record=record,
            environment=environment,
            sink=sink,
            timeout_seconds=timeout_seconds,
        )


def _normalize_pythonpath_for_child(environment: dict[str, str]) -> dict[str, str]:
    """Preserve a launcher's relative ``PYTHONPATH`` when a module check changes directory.

    A supported source-checkout invocation commonly has ``PYTHONPATH=src``.  Python resolves that
    relative entry only when the child starts.  A broad-check test can then change its child cwd to
    a fixture candidate with its own ``src/`` directory, turning the launcher's Ummanu source
    entry into an unintended candidate entry for a configured virtualenv.  Make relative entries
    absolute in the wrapper's current directory before ``Popen`` changes cwd.  Absolute entries
    retain their ordering and are never hidden from provenance.

    An empty entry is Python's spelling for the startup cwd, so it needs the same treatment.
    """
    pythonpath = environment.get("PYTHONPATH")
    if pythonpath is None:
        return environment
    launch_cwd = Path.cwd()
    normalized: list[str] = []
    for entry in pythonpath.split(os.pathsep):
        path = Path(entry) if entry else launch_cwd
        normalized.append(str(path if path.is_absolute() else launch_cwd / path))
    environment["PYTHONPATH"] = os.pathsep.join(normalized)
    return environment


def _run_and_record(
    spec: CheckSpec,
    *,
    root: Path,
    target: Path | None,
    record: Path | None,
    environment: dict[str, str],
    sink: IO[str],
    timeout_seconds: float | None,
) -> tuple[int, dict[str, object]]:
    identity = content_identity(root)
    started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    started = time.monotonic()
    tail = _BoundedTail()
    scanner = _SummaryScanner()
    incomplete_reason = ""
    # One pipe preserves stdout/stderr ordering in the diagnostic tail.
    try:
        process = subprocess.Popen(
            spec.argv(record, root),
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
        )
    except OSError as exc:
        if spec.shape == _SHAPE_MODULE:
            raise BroadCheckError(
                "interpreter_start_failed",
                f"could not start configured interpreter {spec.interpreter!r}: {exc}",
            ) from exc
        raise BroadCheckError("check_start_failed", f"could not start broad check: {exc}") from exc
    try:
        assert process.stdout is not None
        deadline = None if not timeout_seconds else started + float(timeout_seconds)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    # A command that hangs while printing nothing is the case a read-triggered
                    # deadline would never notice, so the wait itself carries the ceiling.
                    incomplete_reason = f"timed out after {timeout_seconds}s"
                    process.kill()
                    break
                if not selector.select(timeout=remaining if remaining is not None else 1.0):
                    continue
                chunk = process.stdout.read1(_READ_CHUNK)
                if not chunk:
                    break
                tail.feed(chunk)
                scanner.feed(chunk)
                sink.write(chunk.decode("utf-8", "replace"))
                sink.flush()
        exit_code = process.wait()
    except BaseException as exc:  # a killed or interrupted runner still owes an honest receipt
        process.kill()
        exit_code = process.wait()
        incomplete_reason = incomplete_reason or f"runner interrupted: {type(exc).__name__}"
        if target is not None:
            _write_receipt(
                target,
                _build_payload(
                    spec=spec,
                    root=root,
                    identity=identity,
                    provenance=_read_provenance(record, root),
                    started_at=started_at,
                    duration=time.monotonic() - started,
                    exit_code=exit_code,
                    tail=tail,
                    parsed=_with_timing(scanner.finish(), record, incomplete=True),
                    incomplete_reason=incomplete_reason,
                ),
            )
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()

    if exit_code < 0 and not incomplete_reason:
        incomplete_reason = f"killed by signal {-exit_code}"
    payload = _build_payload(
        spec=spec,
        root=root,
        identity=identity,
        provenance=_read_provenance(record, root),
        started_at=started_at,
        duration=time.monotonic() - started,
        exit_code=exit_code,
        tail=tail,
        parsed=_with_timing(scanner.finish(), record, incomplete=bool(incomplete_reason)),
        incomplete_reason=incomplete_reason,
    )
    if target is not None:
        _write_receipt(target, payload)
    return exit_code, payload


def _with_timing(parsed: dict[str, object], record: Path | None, *, incomplete: bool) -> dict[str, object]:
    from ummanu.projects.test_timing import valid_observation

    observation: dict[str, object] = {"status": "unavailable", "tests": [], "modules": {}}
    if record is not None:
        try:
            data = json.loads(record.with_name("timing.json").read_text(encoding="utf-8"))
            if valid_observation(data):
                observation = data
        except (OSError, ValueError):
            pass
    if incomplete:
        observation["status"] = "incomplete"
    return {**parsed, "timing": observation}


def _build_payload(
    *,
    spec: CheckSpec,
    root: Path,
    identity: ContentIdentity,
    provenance: dict[str, object],
    started_at: str,
    duration: float,
    exit_code: int,
    tail: _BoundedTail,
    parsed: dict[str, object],
    incomplete_reason: str,
) -> dict[str, object]:
    tail_text = tail.text()
    # The writer does not compose these fields by hand: it records the one model, so what a reader
    # reconstructs at the boundary is by construction what was written.
    result = RunResult.observe(exit_code, incomplete_reason)
    if result is None:
        raise BroadCheckError(
            "unrepresentable_result",
            f"the check returned {exit_code!r}, which is not a result this wrapper can record",
        )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "command": spec.identity,
        "command_shape": spec.shape,
        "check_set": spec.check_set,
        "argv": spec.displayed_argv(),
        "command_or_check_set_digest": spec.digest,
        "cwd": str(Path(root).resolve()),
        "project_provenance": provenance,
        "content_identity": identity.as_dict(),
        "started_at": started_at,
        "ended_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "duration_seconds": round(max(duration, 0.0), 3),
        **result.as_fields(),
        "parsed": parsed,
        "output_bytes": tail.total_bytes,
        "output_lines": tail.total_lines,
        "tail_truncated": tail.truncated,
        "tail": tail_text,
    }
    payload["receipt_digest"] = _receipt_digest(payload)
    return payload


def _write_receipt(path: Path, payload: Mapping[str, object]) -> None:
    """Publish the artifact in one rename, so a reader never observes a half-written receipt."""
    body = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        write_text_atomic(path, body)
    except RuntimeError as exc:
        raise BroadCheckError("receipt_unwritable", str(exc)) from None


@dataclass(frozen=True)
class RunResult:
    """The one canonical model of what a check process did.

    Every recorded result field is a function of two facts: the raw ``Popen.returncode`` and the
    reason the runner has for calling the run unfinished. Deriving `signal`, `status`, `verdict`, the
    stored reason and the shell status from that one model keeps a reader from having to guess which
    of two disagreeing fields to believe.

    The domain is what this POSIX wrapper can observe: a normal status of 0..255, or a negative code
    naming a signal this platform defines. Anything else was never written by a run.
    """

    exit_code: int
    incomplete_reason: str

    @classmethod
    def observe(cls, exit_code: object, incomplete_reason: object) -> RunResult | None:
        """Build the model from a raw process result, or refuse a result nothing could produce."""
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return None
        if not isinstance(incomplete_reason, str):
            return None
        if exit_code < 0:
            if not (1 <= -exit_code < NSIG):
                return None
            # A signalled run is unfinished by construction; if the runner has nothing more
            # specific to say, the canonical reason is the signal itself.
            reason = incomplete_reason.strip() or f"killed by signal {-exit_code}"
        else:
            if exit_code > _MAX_EXIT_STATUS:
                return None
            reason = incomplete_reason.strip()
        return cls(exit_code, reason)

    @property
    def signal(self) -> int:
        return -self.exit_code if self.exit_code < 0 else 0

    @property
    def status(self) -> str:
        return _STATUS_INCOMPLETE if self.incomplete_reason else _STATUS_COMPLETE

    @property
    def verdict(self) -> str:
        if self.status == _STATUS_INCOMPLETE:
            return _VERDICT_UNKNOWN
        return _VERDICT_PASSED if self.exit_code == 0 else _VERDICT_FAILED

    @property
    def shell_status(self) -> int:
        """The status this result gives a caller: `128+N` for a signal, otherwise its own."""
        return self.exit_code if self.exit_code >= 0 else 128 - self.exit_code

    def as_fields(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "signal": self.signal,
            "status": self.status,
            "incomplete_reason": self.incomplete_reason,
            "verdict": self.verdict,
        }

    @classmethod
    def restore(cls, payload: Mapping[str, object]) -> RunResult | None:
        """Reconstruct the model from stored fields, and insist the store agrees with it exactly."""
        result = cls.observe(payload.get("exit_code"), payload.get("incomplete_reason"))
        if result is None:
            return None
        fields = result.as_fields()
        if any(payload.get(key) != value for key, value in fields.items()):
            return None
        return result


def recorded_result(receipt: Mapping[str, object]) -> RunResult | None:
    """The model behind an already-loaded receipt; readers take their answers from here."""
    return RunResult.restore(receipt)


def result_refusal(payload: Mapping[str, object]) -> str:
    """Why this receipt's recorded result could not have been written by a run, or ``""``.

    The digest proves a payload was not edited after something computed it; it says nothing about
    whether the numbers inside describe a run that happened. This rebuilds the canonical model and
    compares every stored field with it, so the answer cannot drift from what the writer writes.
    """
    result = RunResult.observe(payload.get("exit_code"), payload.get("incomplete_reason"))
    if result is None:
        return (
            f"exit code {payload.get('exit_code')!r} with reason "
            f"{payload.get('incomplete_reason')!r} is not a result this wrapper can record"
        )
    disagreements = [
        f"{key}={payload.get(key)!r} (a run would record {value!r})"
        for key, value in result.as_fields().items()
        if payload.get(key) != value
    ]
    if disagreements:
        return "the stored result disagrees with itself: " + ", ".join(disagreements)
    return ""


def load_receipt(path: Path) -> dict[str, object] | None:
    """The one semantic boundary: a receipt reaches a reader through here or not at all.

    `usable_receipt` — and therefore `check show` and `check broad --reuse` — call this first, so an
    unreadable, undigestible, structurally short or internally contradictory artifact is never
    authorized and never has its status preserved. Corruption outranks both.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return intact_receipt(payload)


def intact_receipt(payload: object) -> dict[str, object] | None:
    """The same semantic reader for a local artifact or its immutable audit snapshot."""
    if not isinstance(payload, dict):
        return None
    digest = payload.get("receipt_digest")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        return None
    if digest != _receipt_digest(payload):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    required = (
        "command",
        "command_shape",
        "check_set",
        "command_or_check_set_digest",
        "cwd",
        "project_provenance",
        "content_identity",
        "started_at",
        "ended_at",
        "duration_seconds",
        "exit_code",
        "signal",
        "status",
        "incomplete_reason",
        "verdict",
        "parsed",
        "tail",
    )
    if any(key not in payload for key in required):
        return None
    # Every field the result invariants are stated over is written by every run, so its absence is
    # itself a refusal rather than something to work around.
    if RunResult.restore(payload) is None:
        return None
    check_set = payload.get("check_set")
    if not isinstance(check_set, Mapping) or check_set.get("schema") != _CHECK_SET_SCHEMA:
        return None
    # The receipt carries the whole check set, so a reader recomputes the digest rather than
    # trusting the name it was filed under.
    if check_set_digest(check_set) != payload.get("command_or_check_set_digest"):
        return None
    return payload


@dataclass(frozen=True)
class ReceiptLookup:
    """Whether an existing receipt may stand in for running the broad check again.

    Constructed only by :func:`usable_receipt`, and the one place a caller may ask "may I skip the
    run?" is :meth:`authorized`. There is no field a caller can read to reach a softer conclusion.
    """

    usable: bool
    reason: str
    receipt: dict[str, object] | None
    path: Path

    def authorized(self) -> dict[str, object] | None:
        """The receipt that may replace a run, or nothing at all."""
        return self.receipt if self.usable else None

    def authorized_result(self) -> RunResult | None:
        """The canonical result an authorized receipt recorded, for a caller that owes a status."""
        receipt = self.authorized()
        return None if receipt is None else recorded_result(receipt)

    def as_dict(self) -> dict[str, object]:
        return {
            "usable": self.usable,
            "reason": self.reason,
            "path": str(self.path),
            "receipt": self.receipt,
        }


def candidate_import_refusal(receipt: Mapping[str, object], root: Path, *, expected_package: str = "") -> str:
    """The candidate-trust boundary: why this receipt's import may not be trusted, or ``""``.

    Observed provenance is necessary and not sufficient: only an import resolved *inside this
    candidate workspace* says the run that produced the receipt was a run of this code. A missing
    record, an unreadable one, an empty path, a path that no longer resolves and a path outside the
    candidate are all refusals, here, once, for every caller.
    """
    provenance = receipt.get("project_provenance")
    if not isinstance(provenance, Mapping):
        return "the receipt records no import provenance"
    if provenance.get("origin") != _ORIGIN_CHECK_PROCESS:
        # A shell shape may change directory or import environment before the interpreter starts,
        # so nothing observed what the check imported.
        return (
            "import provenance was not observed from the check process, so this receipt attests no checkout"
        )
    imported_package = str(provenance.get("imported_package") or "")
    if expected_package and imported_package != expected_package:
        return (
            f"the check process recorded package {imported_package or '(none)'!r}, not the "
            f"configured project package {expected_package!r}"
        )
    imported = str(provenance.get("imported_project") or "")
    if not imported:
        return "the check process imported no project, so it validated no checkout"
    try:
        resolved = Path(imported).resolve()
        resolved_root = Path(root).resolve()
        inside = resolved.is_relative_to(resolved_root)
    except (OSError, ValueError):
        return f"the imported project path could not be resolved: {imported}"
    if not inside:
        # Recomputed against this workspace rather than read off the receipt's own flag: the
        # question is where the import lands for the reader, now.
        return f"the check process imported {imported}, which is outside this candidate workspace"
    environment_prefix = provenance.get("environment_prefix")
    if not isinstance(environment_prefix, str) or not environment_prefix:
        return "the receipt records no interpreter environment provenance"
    try:
        resolved_prefix = Path(environment_prefix).resolve()
        imported_from_environment = resolved_prefix != resolved_root and resolved.is_relative_to(
            resolved_prefix
        )
    except (OSError, ValueError):
        imported_from_environment = False
    if imported_from_environment:
        return (
            f"the check process imported {imported} from its interpreter environment, not "
            "the candidate workspace"
        )
    return ""


def usable_receipt(root: Path, check: CheckSpec | str) -> ReceiptLookup:
    """Answer the only question a scrolled-away pane raises: has this run already happened here?

    Usable means the artifact is intact, the run finished, the check process imported this candidate
    workspace, and the receipt describes the content in this checkout right now. A red result is
    usable evidence too.
    """
    spec = as_spec(check)
    path = receipt_path(root, spec)
    receipt = load_receipt(path)
    if receipt is None:
        return ReceiptLookup(False, "no intact receipt for this check", None, path)
    if (
        receipt.get("command_or_check_set_digest") != spec.digest
        or receipt.get("check_set") != spec.check_set
    ):
        # The stored check set is compared, not only the name the file was filed under, so an
        # argument vector that renders the same as another one cannot answer for it.
        return ReceiptLookup(False, "receipt is for a different check", receipt, path)
    if receipt.get("status") != _STATUS_COMPLETE:
        reason = str(receipt.get("incomplete_reason") or "run did not finish")
        return ReceiptLookup(False, f"run did not finish: {reason}", receipt, path)
    refusal = candidate_import_refusal(receipt, root, expected_package=spec.import_package)
    if refusal:
        return ReceiptLookup(False, refusal, receipt, path)
    provenance = receipt["project_provenance"]
    workspace = str(root.resolve())
    if (receipt.get("cwd") != workspace or provenance.get("cwd") != workspace
            or provenance.get("import_roots") != candidate_import_roots(root)):
        return ReceiptLookup(False, "receipt is for a different checkout/import roots", receipt, path)
    # Keep executable symlinks intact: two venvs may link to the same system Python
    # while supplying different dependencies. Compare the spelling Python observed.
    interpreter = spec.interpreter
    if not Path(interpreter).is_absolute():
        interpreter = os.path.abspath(root / interpreter)
    if provenance.get("python") != interpreter:
        return ReceiptLookup(False, "receipt interpreter provenance differs from the declared interpreter", receipt, path)
    prefix = Path(interpreter).parent.parent
    if (prefix / "pyvenv.cfg").is_file() and provenance.get("environment_prefix") != str(prefix):
        return ReceiptLookup(False, "receipt interpreter environment differs from the declared environment", receipt, path)
    recorded = receipt.get("content_identity")
    if not isinstance(recorded, Mapping):
        return ReceiptLookup(False, "receipt records no content identity", receipt, path)
    # A non-tree identity is unresolved and must never produce a false match.
    stored = ContentIdentity(str(recorded.get("tree_sha") or ""))
    current = content_identity(root)
    if not current.resolved:
        return ReceiptLookup(False, "this checkout has no resolvable content identity", receipt, path)
    if not stored.matches(current):
        return ReceiptLookup(False, "content changed since the receipt was written", receipt, path)
    return ReceiptLookup(True, "receipt describes this exact content", receipt, path)


def summarize(receipt: Mapping[str, Any]) -> str:
    """One-screen rendering for a report body, so nobody reruns a suite to quote it."""
    parsed = receipt.get("parsed")
    counts = ""
    if isinstance(parsed, Mapping) and parsed:
        counts = ", ".join(f"{key}={value}" for key, value in sorted(parsed.items()) if key != "timing")
    identity = receipt.get("content_identity")
    tree = identity.get("tree_sha", "") if isinstance(identity, Mapping) else ""
    provenance = receipt.get("project_provenance")
    if isinstance(provenance, Mapping):
        imported = str(provenance.get("imported_project") or "")
        origin = str(provenance.get("origin") or "")
    else:
        imported, origin = "", ""
    if origin != _ORIGIN_CHECK_PROCESS:
        # `cwd` above is where the check was launched, which a shell command is free to leave;
        # only an observed provenance line says where it ended up importing from.
        imported = "(not observed; this check shape attests no import)"
    elif imported:
        inside = " (inside workspace)" if provenance.get("inside_workspace") else " (outside workspace)"
        imported += inside
    lines = [
        f"- command: {receipt.get('command', '')} [{receipt.get('command_shape', '')}]",
        f"- digest: {receipt.get('command_or_check_set_digest', '')}",
        f"- launched in: {receipt.get('cwd', '')}",
        f"- imported project: {imported or '(unresolved)'}",
        f"- tree_sha: {tree or '(unresolved)'}",
        f"- started_at: {receipt.get('started_at', '')} ({receipt.get('duration_seconds', 0)}s)",
        (
            f"- exit_code: {receipt.get('exit_code', '')} ({receipt.get('status', '')}"
            f"/{receipt.get('verdict', '')})"
        ),
    ]
    if counts:
        lines.append(f"- parsed: {counts}")
    from ummanu.projects.test_timing import summary

    timing = parsed.get("timing", {}) if isinstance(parsed, Mapping) else {}
    lines.append(summary(timing))
    return "\n".join(lines)
