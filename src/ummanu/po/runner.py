"""Headless turns of the PO head: one Claude or Codex process per turn, in the PO workspace.

Each turn uses the scoped local-pty supervisor, with the CLI's standard streams redirected to
the turn's files and its own process group. The owner's
message goes to the child on stdin; its stdout is kept raw in a file under
``<data_dir>/po-runs/<session>/`` (outside the workspace the agent can write), and a waiter thread
settles the turn when the process exits. Only the owner's message and the agent's final answer reach
the feed in the board store (`ummanu.po.store`).

The runner lives in the PO service (`ummanu.po.service`, unit `ummanu-po.service`), one per
installation; the web never builds one.

The CLIs own their conversation memory, addressed by their native flags:

* Claude: turn 1 ``claude -p --session-id <uuid>`` with a uuid the ummanu chose at session
  creation, later turns ``--resume <uuid>``; ``--output-format json`` carries the final answer.
* Codex: turn 1 ``codex exec --json``, whose event stream names the ``thread_id`` the session then
  keeps; later turns ``codex exec resume <thread_id>``. ``-o`` writes the final answer to a file.

A session's reasoning effort is passed on every turn: ``--effort <level>`` to Claude,
``-c model_reasoning_effort=<level>`` to Codex. A new session always has an explicit one; a session
stored with the legacy ``default`` (opened before that rule) still resumes with no effort flag. What
model a turn actually ran is kept on the turn: Claude's result object keys ``modelUsage`` by full
model id, the session's own model first and any subagent's after it; Codex's event stream names no
model, so it is read from the ``turn_context`` of the thread's rollout under ``$CODEX_HOME/sessions`` (``~/.codex`` by default).
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.head_health import HeadHealth, failure_status, failure_until, until_text
from ummanu.head_registry import HeadRegistryConfigError, installed_pair
from ummanu.po import PO_REQUEST_ENV, PO_SESSION_ENV
from ummanu.po.models import DEFAULT_EFFORTS, EffortRefused, require_explicit_effort
from ummanu.po.store import (
    CLIS,
    COMPLETED,
    DEFAULT_EFFORT,
    FAILED,
    INTERRUPTED,
    OWNER,
    RUNNING,
    SESSION_CREATE,
    PoStore,
    PoStoreError,
    Session,
    Turn,
)
from ummanu.po.workspace import workspace_dir
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.client import HeadHandle, spawn_head
from ummanu.runtime.head.local_pty.journal import RUN_EXITED, read_events
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.memory import MemoryScopeError
from ummanu.runtime.head.spec import HeadSpec, load_head_specs
from ummanu.runtime.heads import HeadRegistryError, Registry, load_registry
from ummanu.runtime.provider_errors import (
    CODEX_QUOTA_ERROR_INFOS,
    KIND_QUOTA,
    ProviderError,
    classify_provider_error,
    reset_time,
    summarize_provider_error,
)
from ummanu.runtime.provider_models import codex_rollout_path, codex_session_models

RUNS_DIR_NAME = "po-runs"
STOPPED_REASON = "stopped by the owner"
RECOVERED_REASON = "the PO service restarted while this turn was running"
# Recorded on a running turn the PO service re-runs at start (`PoStore.mark_rerun`).
RERUN_REASON = "re-run: the PO service restarted while this turn was running"
# The settled reason of a re-run that was itself interrupted: it is not re-run a second time.
RERUN_INTERRUPTED_REASON = (
    "the PO service restarted again during this turn's re-run; not re-run a second time"
)
# How much of stderr a failed turn quotes in its reason.
STDERR_TAIL_BYTES = 2000
STOP_JOIN_SECONDS = 10.0
# Claude Code's refusal of `--session-id` for a conversation that already exists (checked on 2.1.270).
CLAUDE_SESSION_IN_USE = "is already in use"
# The resource each PO CLI draws on when the head registry names none for it.
DEFAULT_CLI_RESOURCES = {"claude": "claude-sub", "codex": "openai-sub"}
# How much of a session's feed a turn carried over to the other CLI is given as its context.
FALLBACK_CONTEXT_ENTRIES = 24
FALLBACK_CONTEXT_BYTES = 24_000


class RunnerError(RuntimeError):
    """A PO turn could not be started or addressed."""


def runs_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / RUNS_DIR_NAME


def process_identity(pid: int) -> str | None:
    """Boot id and kernel start time of a live process, or ``None`` when it cannot be read.

    A PID alone names whatever process holds it now; with the start time it names the one process
    the runner started, so a restart never kills a stranger that inherited the number.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces and parentheses; the rest starts after the last ')'.
    fields = stat[stat.rindex(")") + 2 :].split()
    if len(fields) < 20:
        return None
    return f"{boot_id}:{fields[19]}"


def still_running(pid: int | None, identity: str | None) -> bool:
    """Whether the process recorded as (`pid`, `identity`) still runs: same identity, not a zombie.

    A killed turn whose parent has not reaped it yet keeps its identity; it runs nothing any more.
    """
    if not pid or not identity or process_identity(pid) != identity:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[:1] not in (["Z"], ["X"])


def turn_environment(
    environ: Mapping[str, str] | None = None, *, interpreter: str | None = None
) -> dict[str, str]:
    """The environment of a turn: the service's own, with the product runtime first on ``PATH``.

    The PO workspace tells the head to run ``python3 -P -m ummanu``; with the service's ``PATH``
    that is the system Python, which lacks the product's dependencies. The directory of the
    interpreter running this process (the production runtime) goes first, so ``python3`` and the
    ``ummanu`` console script resolve there, and the source this process imports goes first on
    ``PYTHONPATH``, as the control-plane commands keep it importable. The turn writes the board as
    the PO (`BOARD_ACTOR`, the actor every board command defaults to) unless the service was given
    another name for it. Everything else is kept.
    """
    env = dict(os.environ if environ is None else environ)
    env["BOARD_ACTOR"] = str(env.get("BOARD_ACTOR") or "").strip() or "po"
    bin_dir = str(Path(interpreter or sys.executable).parent)
    path = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != bin_dir]
    env["PATH"] = os.pathsep.join([bin_dir, *path])
    source = str(Path(__file__).resolve().parents[2])
    pythonpath = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry and entry != source]
    env["PYTHONPATH"] = os.pathsep.join([source, *pythonpath])
    return env


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def claude_final_answer(stdout: str) -> tuple[str | None, str | None]:
    """The final answer from ``--output-format json``, or ``None`` and why there is none."""
    for document in _json_documents(stdout):
        if not isinstance(document, dict) or document.get("type", "result") != "result":
            continue
        result = document.get("result")
        if document.get("is_error"):
            return None, f"claude reported an error: {result or document.get('subtype') or 'no detail'}"
        if isinstance(result, str) and result.strip():
            return result.strip(), None
        return None, "claude's result object carries no final answer"
    return None, "claude printed no result object"


def claude_resolved_model(stdout: str) -> str | None:
    """The session's own model from ``--output-format json``: the first ``modelUsage`` key.

    Claude Code keys ``modelUsage`` by the full model id each model ran under (`claude-opus-5-5`, or
    `claude-opus-5-5[1m]` with the long context), the session's model first and the models its
    subagents ran after it.
    """
    for document in _json_documents(stdout):
        if not isinstance(document, dict) or document.get("type", "result") != "result":
            continue
        usage = document.get("modelUsage")
        if isinstance(usage, dict):
            return next((key for key in usage if isinstance(key, str) and key.strip()), None)
        return None
    return None


def codex_resolved_model(codex_home: Path | str, thread_id: str | None) -> str | None:
    """The model the last turn of a Codex thread ran, from its rollout's ``turn_context``."""
    path = codex_rollout_path(codex_home, thread_id or "")
    if path is None:
        return None
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    return codex_session_models(_json_documents(text, whole=False)).model or None


def _json_documents(text: str, *, whole: bool = True) -> list[Any]:
    """The JSON value `text` is, else every line that parses: last line first, or in order without `whole`.

    A turn relaunched with `--resume` appends a second result object to the same stdout, so the one
    that counts is the last.
    """
    text = text.strip()
    if whole:
        try:
            return [json.loads(text)]
        except ValueError:
            pass
    found: list[Any] = []
    for line in text.splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(found)) if whole else found


def codex_thread_id(stdout: str) -> str | None:
    """The first ``thread_id`` named by Codex's ``--json`` event stream."""

    def find(value: Any) -> str | None:
        if isinstance(value, dict):
            found = value.get("thread_id")
            if isinstance(found, str) and found:
                return found
            for item in value.values():
                found = find(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = find(item)
                if found:
                    return found
        return None

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        found = find(event)
        if found:
            return found
    return None


@dataclass(frozen=True)
class TurnFiles:
    directory: Path
    prompt: Path
    stdout: Path
    stderr: Path
    last_message: Path
    # The card facts of a dispatcher input, kept beside the turn so a failure names the card.
    card: Path | None = None


@dataclass
class _Live:
    process: Any
    thread: threading.Thread


class ScopedPoProcess:
    """The PO service's waitable view of a head owned by the scoped supervisor."""

    def __init__(self, handle: HeadHandle) -> None:
        self.handle = handle
        self.pid = handle.head_pid
        self._supervisor_identity = process_identity(handle.supervisor_pid)
        self.head_loss_reason: str | None = None

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            for event in reversed(read_events(self.handle.journal_path).events):
                if event.get("kind") == RUN_EXITED and event.get("run_id") == self.handle.run_id:
                    self.head_loss_reason = event.get("head_loss_reason")
                    code = event.get("exit_code")
                    number = event.get("signal")
                    return int(code) if code is not None else -int(number or 0)
            if not still_running(self.handle.supervisor_pid, self._supervisor_identity):
                raise RunnerError(f"PO head supervisor {self.handle.supervisor_pid} exited without a run.exited record")
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("PO head", timeout)
            time.sleep(0.05)


class PoRunner:
    """Sessions of the PO head, each turn one process; sessions run their turns in parallel."""

    def __init__(
        self,
        store: PoStore,
        data_dir: Path | str,
        *,
        executables: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        on_settled: Callable[[str, int], None] | None = None,
        on_failed: Callable[[str, int, str], None] | None = None,
        efforts: Mapping[str, tuple[str, ...]] | None = None,
        head_specs: Mapping[str, HeadSpec] | None = None,
        turn_launcher: Callable[..., Any] | None = None,
        scope_owner_unit: str = "ummanu-po.service",
        fallback_choice: Callable[[Session], tuple[str, str, str] | None] | None = None,
    ) -> None:
        self.store = store
        # The CLI, model and effort a session falls over to when its own provider refuses a turn
        # (ummanu-108): the PO service answers from `po.models`; None keeps the turn failed.
        self.fallback_choice = fallback_choice
        # What a new session's effort is checked against unless its create passes its own list.
        self.efforts: Mapping[str, tuple[str, ...]] = (
            dict(efforts) if efforts is not None else DEFAULT_EFFORTS
        )
        # Told (session id, seq) after a waiter settled a turn: the PO service starts the next input.
        self.on_settled = on_settled
        # Told (session id, seq, reason) once when this runner settled a turn `failed` (`_finish`).
        self.on_failed = on_failed
        self.data_dir = Path(data_dir)
        self.workspace = workspace_dir(self.data_dir)
        self.runs = runs_dir(self.data_dir)
        self.executables = {"claude": "claude", "codex": "codex", **dict(executables or {})}
        self.head_specs = dict(head_specs) if head_specs is not None else load_head_specs()
        self._turn_launcher = turn_launcher or self._scoped_launch
        self.scope_owner_unit = scope_owner_unit
        # A turn gets `turn_environment()` unless the caller passes its own.
        self.env = dict(env) if env is not None else turn_environment()
        # Held only while a turn is started, stopped or recovered, never while one runs.
        self._lock = threading.RLock()
        self._live: dict[tuple[str, int], _Live] = {}
        # Re-runs whose launch failed after the allowance was spent, with why: the next recovery pass
        # settles the row `failed` with this reason if `_abandon` could not.
        self._rerun_failures: dict[tuple[str, int], str] = {}

    @classmethod
    def for_instance(cls, instance_dir: Path | str, data_dir: Path | str, **kwargs: Any) -> PoRunner:
        try:
            registry_path = installed_pair(Path(instance_dir)).snapshot
        except HeadRegistryConfigError as exc:
            raise HeadRegistryError(str(exc)) from None
        registry: Registry = load_registry(registry_path)
        kwargs.setdefault("head_specs", load_head_specs(registry))
        return cls(PoStore.for_instance(instance_dir), data_dir, **kwargs)

    def _head_spec(self, session: Session) -> HeadSpec:
        for spec in self.head_specs.values():
            if (spec.adapter, spec.model, spec.effort) == (session.cli, session.model, session.effort):
                return spec
        return HeadSpec.from_profile(
            f"po-{session.cli}-{session.model}-{session.effort}",
            {"adapter": session.cli, "model": session.model, "effort": session.effort},
        )

    def _scoped_launch(
        self, session: Session, seq: int, argv: list[str], files: TurnFiles,
        environment: Mapping[str, str], spec: HeadSpec,
    ) -> ScopedPoProcess:
        # A shell exec keeps the heartbeat PID equal to the CLI PID. Redirection preserves the
        # PO feed's structured stdout and the existing stderr/last-message files.
        command = (
            f"{shlex.join(argv)} < {shlex.quote(str(files.prompt))} "
            f">> {shlex.quote(str(files.stdout))} 2>> {shlex.quote(str(files.stderr))}"
        )
        scope_dir = self._scope_dir(session.session_id, seq)
        scope_dir.parent.mkdir(parents=True, exist_ok=True)
        previous = ScopedHeadLifecycle.from_run_dir(scope_dir)
        with previous.ownership() if previous is not None else nullcontext(None) as record:
            if previous is not None:
                if self._owner_outcome(record) is not None:
                    raise RunnerError("the turn's retained terminal intent must settle before any relaunch")
                previous.stop_owned(record)
            lifecycle = ScopedHeadLifecycle(uuid.uuid4().hex[:24], spec.memory_limit_mib,
                                            self.scope_owner_unit)
            # Keep the old owner serialized through pointer replacement. Its resolved
            # canonical directory stays fixed for concurrent callers holding that owner.
            run_dir = protocol.run_dir_for(self.data_dir / "po-heads", lifecycle.run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            if scope_dir.is_symlink():
                scope_dir.unlink()
            scope_dir.symlink_to(run_dir, target_is_directory=True)
            descriptor = os.open(scope_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            handle = spawn_head(
                root=self.data_dir / "po-heads", run_id=lifecycle.run_id, role="po",
                task=f"po:{spec.profile_id}:{session.session_id}:{seq}", command=command, cwd=session.cwd,
                env=environment, memory_limit_mib=spec.memory_limit_mib,
                owner_unit=self.scope_owner_unit,
            )
            return ScopedPoProcess(handle)

    def _cleanup_turn_scope(self, session_id: str, seq: int) -> bool:
        """The persisted scope, rather than a head PID, owns all turn descendants."""
        lifecycle = ScopedHeadLifecycle.from_run_dir(self._scope_dir(session_id, seq))
        if lifecycle is None:
            return False
        lifecycle.stop_and_prove_empty()
        return True

    def _scope_dir(self, session_id: str, seq: int) -> Path:
        return self.files(session_id, seq).directory / f"turn-{seq:04d}.scope"

    def _pending_outcome(self, session_id: str, seq: int) -> dict[str, Any] | None:
        owner = ScopedHeadLifecycle.from_run_dir(self._scope_dir(session_id, seq))
        if owner is None:
            return None
        assert owner.directory is not None
        with owner.ownership() as record:
            return self._owner_outcome(record)

    def _remember_outcome(self, session_id: str, seq: int, outcome: dict[str, Any]) -> None:
        self._terminal(session_id, seq, outcome, settle=False)

    @staticmethod
    def _owner_outcome(record: dict[str, Any]) -> dict[str, Any] | None:
        outcome = record.get("outcome")
        if outcome is None:
            return None
        if not isinstance(outcome, dict) or outcome.get("state") not in (COMPLETED, FAILED, INTERRUPTED):
            raise MemoryScopeError("scope owner has invalid terminal intent")
        field = "answer" if outcome["state"] == COMPLETED else "reason"
        if not isinstance(outcome.get(field), str):
            raise MemoryScopeError("scope owner has incomplete terminal intent")
        return dict(outcome)

    def _terminal(
        self, session_id: str, seq: int, proposed: dict[str, Any] | None, *, settle: bool = True,
    ) -> bool:
        """Select intent, prove empty and commit that intent as one owner operation.

        The store's running-row transition remains the idempotency boundary. An owner
        interruption overrides uncommitted intent, never an already terminal row.
        The runner lock also serializes genuinely unscoped turns in this process.
        """
        with self._lock:
            owner = ScopedHeadLifecycle.from_run_dir(self._scope_dir(session_id, seq))
            if owner is None:
                if not settle or proposed is None:
                    return False
                return self._commit_outcome(session_id, seq, proposed)
            with owner.ownership() as record:
                if self.store.turn(session_id, seq).state != RUNNING:
                    return False
                selected = self._owner_outcome(record)
                if proposed is not None and (selected is None or proposed["state"] == INTERRUPTED):
                    record["outcome"] = proposed
                    selected = self._owner_outcome(record)
                    owner.update_owner(owner.directory, record)
                if not settle:
                    return False
                if selected is None:
                    raise MemoryScopeError("scope owner has no terminal intent")
                owner.stop_owned(record)
                if not record["cleanup_complete"]:
                    raise MemoryScopeError("scope owner has no durable empty proof")
                return self._commit_outcome(session_id, seq, selected)

    def _commit_outcome(self, session_id: str, seq: int, outcome: dict[str, Any]) -> bool:
        """Only _terminal calls this, while its serialization still covers the store commit."""
        state = outcome["state"]
        resolved = outcome.get("resolved_model")
        if state == COMPLETED:
            return self.store.complete_turn(session_id, seq, outcome["answer"], resolved_model=resolved)
        extra = {"resolved_model": resolved} if resolved is not None else {}
        settled = self.store.finish_turn(session_id, seq, state, outcome["reason"], **extra)
        if settled and state == FAILED and self.on_failed is not None:
            try:
                self.on_failed(session_id, seq, outcome["reason"])
            except Exception as exc:  # noqa: BLE001 - a listener never unsettles a turn
                print(f"ummanu po: after failed turn {session_id}/{seq}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return settled

    # --- sessions ---------------------------------------------------------------------------

    def create_session(
        self, cli: str, model: str, effort: str, *, efforts: Mapping[str, tuple[str, ...]] | None = None
    ) -> Session:
        """A new session at an explicit effort offered for `cli` (`require_explicit_effort`).

        `efforts` is the offered list, this runner's own when not given; `default` is never accepted.
        """
        return self._create(cli, model, effort, None, efforts=efforts)[0]

    def create_session_request(
        self,
        cli: str,
        model: str,
        request_id: str,
        effort: str,
        *,
        efforts: Mapping[str, tuple[str, ...]] | None = None,
        operation: str = SESSION_CREATE,
        fingerprint: str | None = None,
        title: str | None = None,
    ) -> tuple[Session, bool]:
        """`create_session` under a form's request id; the flag says whether this call created it.

        `operation` and `fingerprint` bind the id to another operation that opens a session
        (`PoStore.claim_session`); `title` is the new session's (the resolver's `sprint:<N>`).
        """
        return self._create(
            cli,
            model,
            effort,
            request_id,
            efforts=efforts,
            operation=operation,
            fingerprint=fingerprint,
            title=title,
        )

    def _create(
        self,
        cli: str,
        model: str,
        effort: str,
        request_id: str | None,
        *,
        efforts: Mapping[str, tuple[str, ...]] | None = None,
        operation: str = SESSION_CREATE,
        fingerprint: str | None = None,
        title: str | None = None,
    ) -> tuple[Session, bool]:
        if cli not in CLIS:
            raise RunnerError(f"a PO session runs {' or '.join(CLIS)}, not {cli!r}")
        if not model.strip():
            raise RunnerError("a PO session needs a model")
        try:
            effort = require_explicit_effort(cli, effort, self.efforts if efforts is None else efforts)
        except EffortRefused as exc:
            raise RunnerError(str(exc)) from None
        return self.store.claim_session(
            session_id=str(uuid.uuid4()),
            cli=cli,
            model=model.strip(),
            cwd=str(self.workspace),
            cli_session_id=str(uuid.uuid4()) if cli == "claude" else None,
            request_id=request_id,
            effort=effort,
            operation=operation,
            fingerprint=fingerprint,
            title=title,
        )

    def files(self, session_id: str, seq: int) -> TurnFiles:
        directory = self.runs / session_id
        stem = f"turn-{seq:04d}"
        return TurnFiles(
            directory=directory,
            prompt=directory / f"{stem}.prompt",
            stdout=directory / f"{stem}.stdout",
            stderr=directory / f"{stem}.stderr",
            last_message=directory / f"{stem}.last-message",
            card=directory / f"{stem}.card.json",
        )

    def argv(self, session: Session, files: TurnFiles, *, established: bool = False) -> list[str]:
        """One turn's command. `established`: a Claude conversation is known to exist under its id."""
        executable = self.executables[session.cli]
        if session.cli == "claude":
            assert session.cli_session_id is not None
            argv = [
                executable,
                "-p",
                "--output-format",
                "json",
                "--model",
                session.model,
                *self._effort_options(session),
                "--dangerously-skip-permissions",
            ]
            if established:
                return [*argv, "--resume", session.cli_session_id]
            return [*argv, "--session-id", session.cli_session_id]
        options = [
            "--json",
            "-m",
            session.model,
            *self._effort_options(session),
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-o",
            str(files.last_message),
        ]
        if session.cli_session_id:
            return [executable, "exec", "resume", *options, session.cli_session_id, "-"]
        return [executable, "exec", *options, "-C", session.cwd, "-"]

    @staticmethod
    def _effort_options(session: Session) -> list[str]:
        """The effort flag of this session's CLI, or none for `default`."""
        if session.effort == DEFAULT_EFFORT:
            return []
        if session.cli == "claude":
            return ["--effort", session.effort]
        return ["-c", f"model_reasoning_effort={session.effort}"]

    # --- turns ------------------------------------------------------------------------------

    def send(self, session_id: str, text: str) -> Turn:
        """Start one turn, or refuse with nothing written when one is already running."""
        return self._send(session_id, text, None)[0]

    def send_request(
        self,
        session_id: str,
        text: str,
        request_id: str,
        *,
        card: dict[str, Any] | None = None,
        note: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Turn, bool]:
        """`send` under a form's request id; the flag says whether this call started the turn.

        A request id that already started this send gets that turn back and no process, and one that
        belongs to anything else is `RequestConflict` (`PoStore.claim_turn`): a CLI is launched only for
        a turn this call created, after its transaction committed. `card` is the facts a dispatcher
        input carries beside its text; they are part of what the id is bound to. `note` is the PO
        service's own section after the text (an operation card's production rights): the prompt and
        the feed carry it, the id does not bind it.
        """
        return self._send(session_id, text, request_id, card, note, metadata)

    def _send(
        self,
        session_id: str,
        text: str,
        request_id: str | None,
        card: dict[str, Any] | None = None,
        note: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Turn, bool]:
        if not text.strip():
            raise RunnerError("an empty message starts no turn")
        prompt = f"{text.rstrip()}\n\n{note.strip()}\n" if note and note.strip() else text
        with self._lock:
            turn, created = self.store.claim_turn(
                session_id,
                text,
                lambda seq: self.files(session_id, seq).stdout,
                request_id=request_id,
                card=card,
                prompt=prompt,
                metadata=metadata,
            )
            if not created:
                return turn, False
            if card is not None:
                self._keep_card(session_id, turn.seq, card)
            self._start(session_id, turn.seq, prompt)
        return self.store.turn(session_id, turn.seq), True

    def _start(self, session_id: str, seq: int, text: str) -> None:
        """Launch the process of a claimed `running` turn and its waiter; the caller holds `_lock`."""
        try:
            prepared = self._prepare(session_id, seq, text)
        except Exception as exc:
            self._abandon(session_id, seq, None, f"could not prepare the turn: {type(exc).__name__}: {exc}")
            raise
        self._run(session_id, seq, prepared)

    def _prepare(self, session_id: str, seq: int, text: str) -> tuple[Session, list[str], TurnFiles]:
        """Everything a launch needs, read and written before anything is launched: session, argv, prompt."""
        files = self.files(session_id, seq)
        # Read after the claim: the previous turn may have recorded Codex's thread id.
        session = self.store.session(session_id)
        # Claude is resumed only once a turn completed; after a stopped or failed first turn
        # the conversation may or may not exist, and the waiter settles that (`_resume_instead`).
        established = any(
            earlier.state == COMPLETED for earlier in self.store.turns(session_id) if earlier.seq < seq
        )
        argv = self.argv(session, files, established=established)
        files.directory.mkdir(parents=True, exist_ok=True)
        files.prompt.write_text(text, encoding="utf-8")
        return session, argv, files

    def _run(self, session_id: str, seq: int, prepared: tuple[Session, list[str], TurnFiles]) -> None:
        """Launch a prepared turn and start its waiter; any failure settles it `failed` (`_abandon`)."""
        session, argv, files = prepared
        process = self._launch(session, seq, argv, files)
        try:
            thread = threading.Thread(
                target=self._wait,
                args=(session, seq, process, argv, files),
                name=f"po-turn-{session_id}-{seq}",
                daemon=True,
            )
            self._live[(session_id, seq)] = _Live(process, thread)
            thread.start()
        except BaseException as exc:
            self._live.pop((session_id, seq), None)
            self._abandon(
                session_id,
                seq,
                process,
                f"the turn's waiter did not start: {type(exc).__name__}: {exc}",
            )
            raise

    def _launch(
        self, session: Session, seq: int, argv: list[str], files: TurnFiles
    ) -> Any:
        """Start one CLI process for a turn and record it, or leave no live process group behind.

        Output is appended, so a turn relaunched by `_resume_instead` keeps both attempts' raw output.
        """
        try:
            process = self._turn_launcher(
                session, seq, argv, files, self.session_environment(session, seq),
                self._head_spec(session),
            )
        except (OSError, RuntimeError) as exc:
            reason = f"could not start {argv[0]}: {exc}"
            if getattr(exc, "cleanup_complete", True) is False:
                # The scope may still own a head. Leave the turn running for recovery
                # rather than record a failed turn while that head can execute.
                self._remember_outcome(session.session_id, seq, {"state": FAILED, "reason": reason})
                raise RunnerError(reason) from None
            self._abandon(session.session_id, seq, None, reason)
            raise RunnerError(reason) from None
        try:
            if not self.store.record_process(
                session.session_id, seq, process.pid, process_identity(process.pid)
            ):
                raise RunnerError(
                    f"turn {seq} of PO session {session.session_id} was settled while it started"
                )
        except BaseException as exc:
            self._abandon(
                session.session_id,
                seq,
                process,
                f"the turn's process could not be recorded, so it was killed: {type(exc).__name__}: {exc}",
            )
            raise
        return process

    def session_environment(self, session: Session, seq: int | None = None) -> dict[str, str]:
        """The environment of one of `session`'s turns: the runner's, naming the session (`PO_SESSION_ENV`).

        Every launch goes through here: a new turn, a re-run at start, and a relaunch over a fresh
        conversation. With the turn's `seq` it also names the request id of the input the turn answers
        (`PO_REQUEST_ENV`), read from the store, so a re-run names the same one; a turn whose input
        carried no request id, or a store that does not answer, leaves it unset.
        """
        environment = {key: value for key, value in self.env.items() if key != PO_REQUEST_ENV}
        environment[PO_SESSION_ENV] = session.session_id
        if seq is not None:
            try:
                request_id = self.store.turn_request_id(session.session_id, seq)
            except Exception as exc:  # noqa: BLE001 - a turn is not refused for its provenance
                print(
                    f"ummanu po: turn {session.session_id}/{seq} starts without ${PO_REQUEST_ENV}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                request_id = None
            if request_id:
                environment[PO_REQUEST_ENV] = request_id
        return environment

    def _finish(
        self, session_id: str, seq: int, state: str, reason: str, *, resolved_model: str | None = None
    ) -> bool:
        """`PoStore.finish_turn`, the one way this runner settles a turn failed or interrupted.

        A turn it settled `failed` is told to `on_failed` once, here and nowhere else: every failure
        path of the runner (a launch, a waiter, a re-run, a recovery) ends in this call.
        """
        return self._terminal(session_id, seq, {"state": state, "reason": reason, "resolved_model": resolved_model})

    def _keep_card(self, session_id: str, seq: int, card: Mapping[str, Any]) -> None:
        """The card facts of a claimed dispatcher input, beside its turn's files (best effort)."""
        path = self.files(session_id, seq).card
        try:
            assert path is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(card), sort_keys=True), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            print(f"ummanu po: turn {session_id}/{seq} card facts not kept: {exc}", file=sys.stderr)

    def turn_card(self, session_id: str, seq: int) -> dict[str, Any] | None:
        """The card facts the input of turn `seq` carried, or None for an input that carried none."""
        path = self.files(session_id, seq).card
        try:
            document = json.loads(path.read_text(encoding="utf-8")) if path is not None else None
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _abandon(
        self, session_id: str, seq: int, process: Any | None, reason: str
    ) -> None:
        """Kill and reap a turn's process group, then settle the turn `failed` if the store answers.

        If it does not, the row stays `running` with no recorded process, and `recover()` settles
        it (or re-runs it once) at the next start; there is nothing left alive for it to kill.
        """
        if process is not None and ScopedHeadLifecycle.from_run_dir(self._scope_dir(session_id, seq)) is None:
            _kill_group(process.pid)
        try:
            self._finish(session_id, seq, FAILED, reason)
        except MemoryScopeError:
            raise  # retain ownership and the running row for service recovery
        except Exception as exc:  # noqa: BLE001 - the unsettled row is recover()'s to settle
            print(
                f"ummanu po: turn {session_id}/{seq} left running for recovery: {reason}; "
                f"settling it failed: {type(exc).__name__}: {exc}", file=sys.stderr,
            )
        if process is not None:
            try:
                process.wait(STOP_JOIN_SECONDS)
            except (subprocess.TimeoutExpired, RunnerError):
                pass

    def stop(self, session_id: str) -> Turn | None:
        """Kill the running turn's process group; the turn is `interrupted`, the session goes on."""
        return self._interrupt(session_id, None)

    def stop_turn(self, session_id: str, seq: int) -> Turn | None:
        """`stop`, but only when turn `seq` is the one running: a stop form left open stops nothing newer.

        The check and the kill happen under the same lock, so a turn started in between is never hit.
        """
        return self._interrupt(session_id, seq)

    def _interrupt(self, session_id: str, seq: int | None) -> Turn | None:
        session = self.store.session(session_id)
        with self._lock:
            running = self.store.running_turns(session_id)
            if not running or (seq is not None and running[0].seq != seq):
                return None
            turn = running[0]
            live = self._live.get((session_id, turn.seq))
            scoped = ScopedHeadLifecycle.from_run_dir(self._scope_dir(session_id, turn.seq)) is not None
            if not scoped and live is not None:
                _kill_group(live.process.pid)
            elif not scoped and turn.pid and turn.process_identity and process_identity(turn.pid) == turn.process_identity:
                _kill_group(turn.pid)
            self._finish(session_id, turn.seq, INTERRUPTED, STOPPED_REASON)
        if live is not None:
            live.thread.join(STOP_JOIN_SECONDS)
        else:
            self._capture_thread_id(session, Path(turn.stdout_path))
        return self.store.turn(session_id, turn.seq)

    def recover(self, *, rerun: bool = False) -> list[Turn]:
        """At service start: every `running` turn this runner does not own is settled, or re-run once.

        A turn's own process is killed only while its PID still has the recorded identity, so a
        reused PID is never hit. Without `rerun` every such turn is `interrupted`. With it (the PO
        service's start) a turn is re-run once: the same prompt goes to the same CLI conversation
        (Claude `--resume` once a turn of the session completed, else `--session-id` and
        `_resume_instead`; Codex `exec resume` when its thread id is known). A turn found `running` that
        already carries a re-run's reason was interrupted a second time and is settled `interrupted`,
        never re-run again. A turn the owner stopped was settled by the stop and is never `running` here.

        The re-run allowance (:meth:`PoStore.mark_rerun`) is spent only after everything the re-run
        needs is loaded and its prompt written (:meth:`_prepare`); a store that fails before that
        leaves the row as it was, for the next pass. A re-run whose launch fails after the allowance is
        settled `failed` by `_abandon`, or by the next pass with the reason kept here. A row that
        cannot be handled now is left for :meth:`orphaned_turns` to report; one bad row does not stop
        the others.
        """
        recovered: list[Turn] = []
        for turn in self.store.running_turns():
            key = (turn.session_id, turn.seq)
            with self._lock:
                try:
                    if key in self._live and self._pending_outcome(turn.session_id, turn.seq) is None:
                        continue
                    done = self._recover_one(turn, rerun)
                except Exception as exc:  # noqa: BLE001 - this row stays for the next pass
                    print(
                        f"ummanu po: turn {turn.session_id}/{turn.seq} not recovered yet: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    continue
            if done:
                try:
                    recovered.append(self.store.turn(turn.session_id, turn.seq))
                except PoStoreError:
                    pass
        return recovered

    def _recover_one(self, turn: Turn, rerun: bool) -> bool:
        """Settle or re-run one turn this runner does not own; False when nothing changed. Holds `_lock`."""
        key = (turn.session_id, turn.seq)
        outcome = self._pending_outcome(turn.session_id, turn.seq)
        if outcome is not None:
            return self._terminal(turn.session_id, turn.seq, None)
        alive = still_running(turn.pid, turn.process_identity)
        scoped = self._cleanup_turn_scope(turn.session_id, turn.seq)
        if not rerun or turn.reason is not None:
            failed = self._rerun_failures.get(key)
            if failed is not None:
                state, reason = FAILED, failed
            elif rerun:
                state, reason = (
                    INTERRUPTED,
                    f"{RERUN_INTERRUPTED_REASON} (it was re-run because: {turn.reason})",
                )
            else:
                state, reason = INTERRUPTED, RECOVERED_REASON
            reason += "; its process was killed" if alive else ""
            if alive and not scoped:
                _kill_group(int(turn.pid))
            settled = self._finish(turn.session_id, turn.seq, state, reason)
            if settled:
                self._rerun_failures.pop(key, None)
                self._capture_thread_id(self.store.session(turn.session_id), Path(turn.stdout_path))
            return settled
        # Everything the re-run needs, before the allowance is spent.
        self._capture_thread_id(self.store.session(turn.session_id), Path(turn.stdout_path))
        try:
            prepared = self._prepare(turn.session_id, turn.seq, self._owner_text(turn.session_id, turn.seq))
        except PoStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - not a passing store failure: the row can never re-run
            if alive and not scoped:
                _kill_group(int(turn.pid))
            return self._finish(
                turn.session_id,
                turn.seq,
                FAILED,
                f"{RERUN_REASON}, but its re-run could not be prepared: {type(exc).__name__}: {exc}",
            )
        if alive and not scoped:
            _kill_group(int(turn.pid))
        why = RERUN_REASON + ("; its process was killed first" if alive else "")
        if not self.store.mark_rerun(turn.session_id, turn.seq, why):
            return False
        try:
            self._run(turn.session_id, turn.seq, prepared)
        except Exception as exc:  # noqa: BLE001 - `_abandon` settled it failed, or the next pass will
            self._rerun_failures[key] = f"{why}; the re-run did not start: {type(exc).__name__}: {exc}"
            print(
                f"ummanu po: re-run of turn {turn.session_id}/{turn.seq} did not start: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return True

    def orphaned_turns(self) -> list[Turn]:
        """`running` rows with no waiter of this runner: what recovery has not settled or re-run yet."""
        running = self.store.running_turns()
        with self._lock:
            return [turn for turn in running if (turn.session_id, turn.seq) not in self._live
                    or self._pending_outcome(turn.session_id, turn.seq) is not None]

    def _owner_text(self, session_id: str, seq: int) -> str:
        """The owner's message that started turn `seq`, from the feed the claim wrote it to."""
        for entry in self.store.feed(session_id):
            if entry.turn_seq == seq and entry.role == OWNER:
                return entry.text
        raise RunnerError(f"turn {seq} of PO session {session_id} has no owner message to re-run")

    def wait(self, session_id: str, seq: int, timeout: float | None = None) -> Turn:
        """Block until this runner's waiter has settled the turn (a convenience for callers)."""
        with self._lock:
            live = self._live.get((session_id, seq))
        if live is not None:
            live.thread.join(timeout)
        return self.store.turn(session_id, seq)

    # --- settling ---------------------------------------------------------------------------

    def _wait(
        self,
        session: Session,
        seq: int,
        process: Any,
        argv: list[str],
        files: TurnFiles,
    ) -> None:
        try:
            code = process.wait()
            relaunched = self._resume_instead(session, seq, code, argv, files)
            if relaunched is not None:
                code = relaunched.wait()
            final_process = relaunched if relaunched is not None else process
            offset = 0
            fallen = self._fall_over(session, seq, code, files)
            if fallen is not None:
                session, final_process, offset = fallen
                code = final_process.wait()
                # The replacement's own refusal is recorded too; the turn then settles failed.
                self._provider_failure(session, code, files, offset=offset, record=True)
            self._settle(
                session, seq, code, files,
                head_loss_reason=getattr(final_process, "head_loss_reason", None), offset=offset,
            )
        except Exception as exc:  # noqa: BLE001 - a waiter must never leave a turn running without a word
            try:
                if self._pending_outcome(session.session_id, seq) is None:
                    self._finish(
                        session.session_id,
                        seq,
                        FAILED,
                        f"the runner could not settle this turn: {type(exc).__name__}: {exc}",
                    )
            except Exception as nested:  # noqa: BLE001 - the store itself is what failed
                print(
                    f"ummanu po: turn {session.session_id}/{seq} left running: "
                    f"{type(exc).__name__}: {exc}; then {type(nested).__name__}: {nested}",
                    file=sys.stderr,
                )
        finally:
            with self._lock:
                self._live.pop((session.session_id, seq), None)
            if self.on_settled is not None:
                try:
                    self.on_settled(session.session_id, seq)
                except Exception as exc:  # noqa: BLE001 - a listener never unsettles a turn
                    print(
                        f"ummanu po: after turn {session.session_id}/{seq}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )

    def live_count(self) -> int:
        """How many turns this runner's waiters still hold."""
        with self._lock:
            return len(self._live)

    def _resume_instead(
        self, session: Session, seq: int, code: int, argv: list[str], files: TurnFiles
    ) -> Any | None:
        """Relaunch as `--resume` when Claude says an earlier stopped or failed turn saved the conversation.

        Claude Code 2.1.270 answers `--session-id` over an existing conversation with
        `Error: Session ID <uuid> is already in use.` and exit 1, and `--resume` over a missing one with
        `No conversation found with session ID: <uuid>`. Only the first can follow a turn that never
        completed, and it is retried here, inside the same turn.
        """
        if session.cli != "claude" or code != 1 or "--session-id" not in argv:
            return None
        if CLAUDE_SESSION_IN_USE not in self._stderr_tail(files):
            return None
        with self._lock:
            if self.store.turn(session.session_id, seq).state != RUNNING:
                return None
            if self._pending_outcome(session.session_id, seq) is not None:
                return None
            process = self._launch(session, seq, self.argv(session, files, established=True), files)
            live = self._live.get((session.session_id, seq))
            if live is not None:
                live.process = process
        return process

    def _settle(
        self, session: Session, seq: int, code: int, files: TurnFiles,
        *, head_loss_reason: str | None = None, offset: int = 0,
    ) -> None:
        stdout = files.stdout.read_bytes()[offset:].decode("utf-8", errors="replace")
        self._capture_thread_id(session, files.stdout, stdout)
        if session.cli == "claude":
            answer, missing = claude_final_answer(stdout)
            resolved = claude_resolved_model(stdout)
        else:
            answer, missing = self._codex_final_answer(files)
            resolved = codex_resolved_model(
                self.codex_home(), session.cli_session_id or codex_thread_id(stdout)
            )
        if code != 0:
            reason = f"{head_loss_reason}: {session.cli} exited with status {code}" if head_loss_reason else f"{session.cli} exited with status {code}"
            tail = self._stderr_tail(files)
            if tail:
                reason += f": {tail}"
            self._finish(session.session_id, seq, FAILED, reason, resolved_model=resolved)
        elif answer is None:
            self._finish(
                session.session_id, seq, FAILED, missing or "no final answer", resolved_model=resolved
            )
        else:
            self._terminal(session.session_id, seq, {
                "state": COMPLETED, "answer": answer, "resolved_model": resolved,
            })

    # --- provider fallback (ummanu-108) -------------------------------------------------------

    def cli_resource(self, cli: str) -> str:
        """The resource a CLI's PO turns draw on: the registry's, else the product's default."""
        for spec in self.head_specs.values():
            if spec.adapter == cli and spec.resource:
                return spec.resource
        return DEFAULT_CLI_RESOURCES.get(cli, "")

    def _provider_failure(
        self, session: Session, code: int, files: TurnFiles, *, offset: int = 0, record: bool = False
    ) -> ProviderError | None:
        """The provider refusal a failed turn ended on, read from the CLI's own output; else None.

        With `record`, the CLI's resource is recorded red until the reset the provider named (else
        a bounded backoff), in the same health cache the dispatcher reads.
        """
        try:
            stdout = files.stdout.read_bytes()[offset:].decode("utf-8", errors="replace")
        except OSError:
            stdout = ""
        if session.cli == "claude":
            answer, _missing = claude_final_answer(stdout)
        else:
            answer, _missing = self._codex_final_answer(files)
        if code == 0 and answer is not None:
            return None
        failure = po_provider_error(session.cli, stdout, self._stderr_tail(files))
        if failure is not None and record:
            resource = self.cli_resource(session.cli)
            now = time.time()
            try:
                HeadHealth(None, self.data_dir).record(
                    resource,
                    failure_status(failure.kind),
                    f"provider error in a PO turn on {session.cli}/{session.model}: {failure.summary}",
                    now=now,
                    until=failure_until(failure.kind, failure.reset_at, now),
                )
            except Exception as exc:  # noqa: BLE001 - the fallback below does not depend on it
                print(f"ummanu po: could not record {resource} red: {type(exc).__name__}: {exc}", file=sys.stderr)
        return failure

    def _fall_over(self, session: Session, seq: int, code: int, files: TurnFiles) -> tuple[Session, Any, int] | None:
        """Rerun a turn its provider refused on the session's other CLI, inside the same turn.

        The session moves to the CLI, model and effort `fallback_choice` names (a fresh conversation
        there), and the turn's own prompt is given again, prefixed with the session's recent feed so
        the PO resumes from its durable state. Returns the new session, process and the stdout offset
        the replacement's output starts at; None when there is nothing to fall over to.
        """
        if self.fallback_choice is None:
            return None
        failure = self._provider_failure(session, code, files, record=True)
        if failure is None:
            return None
        choice = self.fallback_choice(session)
        if choice is None or choice[0] == session.cli:
            return None
        cli, model, effort = choice
        held = HeadHealth(None, self.data_dir).red_resources().get(self.cli_resource(cli))
        if held is not None:
            print(
                f"ummanu po: turn {session.session_id}/{seq} refused by {session.cli} ({failure.summary}); "
                f"{cli} is {held.status} until {until_text(held.until)}, so the turn fails",
                file=sys.stderr,
            )
            return None
        with self._lock:
            if self.store.turn(session.session_id, seq).state != RUNNING:
                return None
            if self._pending_outcome(session.session_id, seq) is not None:
                return None
            try:
                prompt = files.prompt.read_text(encoding="utf-8")
            except OSError:
                return None
            moved = self.store.switch_cli(
                session.session_id,
                cli=cli,
                model=model,
                effort=effort,
                cli_session_id=str(uuid.uuid4()) if cli == "claude" else None,
            )
            files.prompt.write_text(self._fallback_prompt(session, moved, seq, failure, prompt), encoding="utf-8")
            try:
                offset = files.stdout.stat().st_size
            except OSError:
                offset = 0
            process = self._launch(moved, seq, self.argv(moved, files, established=False), files)
            live = self._live.get((session.session_id, seq))
            if live is not None:
                live.process = process
        print(
            f"ummanu po: turn {session.session_id}/{seq} refused by {session.cli} ({failure.summary}); "
            f"the session continues on {cli}/{model}",
            file=sys.stderr,
        )
        return moved, process, offset

    def _fallback_prompt(
        self, previous: Session, moved: Session, seq: int, failure: ProviderError, prompt: str
    ) -> str:
        """The rerun turn's prompt: why the CLI changed, the session's recent feed, then the input."""
        try:
            feed = [entry for entry in self.store.feed(previous.session_id) if entry.turn_seq < seq]
        except Exception:  # noqa: BLE001 - the rerun goes ahead without its history
            feed = []
        lines: list[str] = []
        size = 0
        for entry in reversed(feed[-FALLBACK_CONTEXT_ENTRIES:]):
            line = f"[{entry.role} {entry.turn_seq}] {entry.text.strip()}"
            size += len(line.encode("utf-8"))
            if size > FALLBACK_CONTEXT_BYTES:
                break
            lines.append(line)
        history = "\n\n".join(reversed(lines)) or "(no earlier messages)"
        return (
            f"[ummanu] This PO session ran on {previous.cli}/{previous.model} until its provider refused "
            f"this turn ({failure.summary}). It continues here on {moved.cli}/{moved.model} in a new "
            "conversation. The session's recent messages, oldest first, are below; answer the last "
            "input as the same PO would.\n\n"
            f"--- session history ---\n{history}\n--- end of history ---\n\n{prompt}"
        )

    def codex_home(self) -> Path:
        """The Codex home a turn runs with: `$CODEX_HOME` of the turn environment, else `~/.codex`."""
        configured = self.env.get("CODEX_HOME")
        if configured:
            return Path(configured)
        return Path(self.env.get("HOME") or Path.home()) / ".codex"

    @staticmethod
    def _codex_final_answer(files: TurnFiles) -> tuple[str | None, str | None]:
        try:
            answer = files.last_message.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None, "codex wrote no last message"
        return (answer, None) if answer else (None, "codex's last message is empty")

    @staticmethod
    def _stderr_tail(files: TurnFiles) -> str:
        try:
            data = files.stderr.read_bytes()
        except OSError:
            return ""
        return data[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").strip()

    def _capture_thread_id(self, session: Session, stdout_path: Path, stdout: str | None = None) -> None:
        """Keep Codex's thread id from any turn that reached it, so the next turn can resume."""
        if session.cli != "codex" or session.cli_session_id:
            return
        if stdout is None:
            try:
                stdout = stdout_path.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                return
        thread_id = codex_thread_id(stdout)
        if thread_id:
            self.store.set_cli_session_id(session.session_id, thread_id)


def po_provider_error(cli: str, stdout: str, stderr: str) -> ProviderError | None:
    """The provider refusal a PO turn's own output names (ummanu-108), or None.

    Claude `--output-format json`: a result object with `is_error`. Codex `exec --json`: an `error`
    or `turn.failed` event. Then the stderr tail, where both CLIs print a refusal they could not
    turn into an event. Only error records are read, never an answer.
    """
    texts: list[str] = []
    for document in _json_documents(stdout, whole=cli == "claude"):
        if not isinstance(document, dict):
            continue
        if cli == "claude" and document.get("type", "result") == "result" and document.get("is_error"):
            texts.append(str(document.get("result") or document.get("subtype") or ""))
        elif cli == "codex" and document.get("type") in ("error", "turn.failed"):
            error = document.get("error")
            message = error.get("message") if isinstance(error, dict) else document.get("message")
            info = error.get("codex_error_info") if isinstance(error, dict) else None
            if str(info or "") in CODEX_QUOTA_ERROR_INFOS:
                text = str(message or info)
                return ProviderError(KIND_QUOTA, None, summarize_provider_error(text), reset_at=reset_time(text))
            texts.append(str(message or ""))
    texts.append(stderr or "")
    for text in texts:
        found = classify_provider_error(text) if text.strip() else None
        if found is not None:
            return found
    return None


__all__ = [
    "PO_REQUEST_ENV",
    "PO_SESSION_ENV",
    "RECOVERED_REASON",
    "RERUN_INTERRUPTED_REASON",
    "RERUN_REASON",
    "RUNS_DIR_NAME",
    "STOPPED_REASON",
    "PoRunner",
    "RunnerError",
    "TurnFiles",
    "claude_final_answer",
    "claude_resolved_model",
    "codex_resolved_model",
    "codex_thread_id",
    "po_provider_error",
    "process_identity",
    "runs_dir",
    "still_running",
    "turn_environment",
]
