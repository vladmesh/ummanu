"""Drive one supervised local-PTY head per triggered agent under its run lock.

Resolve one registry snapshot before rendering a command/report. Unusable profiles fail
closed: record the reason, exit 1, preserve existing heads/reports. Pause exits 0. Persist
head_run for later ticks; working heads refuse duplicate bring-up. Retire only positively
quiescent idle heads through guarded stop. Any terminal_handle.json fences launch until
operator removal, regardless of content. _TickReports owns created/inherited obligations
on every exit; refused ticks hold existing state still. See docs/OPERATIONS.md.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from ummanu.head_health import HeadHealth, failure_status, failure_until, resolve_head_chain, until_text
from ummanu.infra.env import env_value
from ummanu.runtime import claude_env
from ummanu.runtime.codex_preflight import (
    CodexPreflightError,
    preflight_codex_launch,
)
from ummanu.runtime.head import (
    HEAD_ALIVE,
    HEAD_BUSY,
    STANDING_BINDING,
    HeadRun,
    HeadSpec,
    NudgePointer,
    StopInitiator,
    TaskRef,
    new_run_id,
    render_head_command,
    with_pid_heartbeat,
)
from ummanu.runtime.head.identity import head_process_status
from ummanu.runtime.head_runtime_backends import build_head_runtime
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.state import AgentState

from .production_telemetry import data_dir as _installation_data_dir

_REPO_ROOT = Path(__file__).resolve().parents[4]
CLAUDE_JSON = Path(env_value("UMMANU_CLAUDE_JSON", str(Path.home() / ".claude.json")))

#: The `runs.jsonl` action of a tick that failed closed because it had no supervised head to raise.
NO_SUPERVISED_HEAD = "no-supervised-head"
#: The `runs.jsonl` action of a tick refused because a pane is still recorded as this role's head.
SUPERVISED_OWNER_CONFLICT = "supervised-owner-conflict"
#: The exit status of a tick that refused to raise a head. Nonzero, so the unit records a failure.
REFUSED_EXIT = 1


@dataclass(frozen=True)
class DispatchCommand:
    skill: str
    launch: str
    profile: str | None
    card_ref: str | None = None
    prompt_after_start: bool = False
    head_profile: dict | None = None


class StewardReportBoard(Protocol):
    """The narrow report-card port required by the triggered-agent head driver."""

    def create_report(self, *, project: str, title: str, slug: str) -> str: ...

    def move_report(self, *, reference: str, target: Literal["done", "blocked"], reason: str) -> None: ...


class NoSupervisedHead(Exception):
    """Resolution/render refusal before launch, naming the missing usable local-PTY head.

    _tick catches it and delegates to _fail_closed; it never escapes a tick.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _workspace(agent: str) -> str:
    return env_value("UMMANU_WORKSPACE", nonempty=True) or str(Path.home() / "orca/workspaces/ummanu" / agent)


def _load_spec(agent: str) -> dict:
    return tomllib.loads(
        (_REPO_ROOT / "src" / "ummanu" / "automations" / "agents" / agent / "automation.toml").read_text()
    )


def _pipeline_paused() -> bool:
    """Read pipeline pause before the registry; any read failure also pauses dispatch.

    Both pause modes defer standing agents. Warn on unreadable ticks; import lazily to avoid
    a process-start cycle with the pipeline package.
    """
    try:
        from ..agents.pipeline import pause as pipeline_pause

        return pipeline_pause.is_paused()
    except Exception as exc:  # noqa: BLE001 - an unreadable pause state refuses the dispatch
        print(
            f"dispatch: pipeline pause state is unreadable; refusing dispatch ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return True


@dataclass(frozen=True)
class RegistrySnapshot:
    """One registry reading for routing, health fallback and runtime resolution in a tick.

    Unreadable registry is None with its error and requires fail-closed dispatch.
    """

    registry: Any | None
    error: str = ""


def _registry_snapshot() -> RegistrySnapshot:
    """This tick's one reading of the head registry.

    Cheap by construction: parsing the registry file probes no resource. A caller outside a tick
    (a test, a one-shot helper) passes no snapshot and gets one of its own.
    """
    try:
        from ummanu.runtime import heads as pipeline_heads

        return RegistrySnapshot(pipeline_heads.load_registry())
    except Exception as exc:  # noqa: BLE001 - an unreadable registry is reported, not raised
        return RegistrySnapshot(None, f"{type(exc).__name__}: {exc}")


def _preferred_head(agent: str, spec: dict, snapshot: RegistrySnapshot | None = None) -> str | None:
    """Resolve the role default from the tick snapshot, with spec.head as last resort.

    Propagate undefined-profile refusal; routing and launch share the same snapshot.
    """
    registry = (_registry_snapshot() if snapshot is None else snapshot).registry
    if registry is None:
        return spec.get("head")
    routed = registry.role_default(agent)
    if routed:
        return routed
    fallback = spec.get("head")
    return registry.resolve(fallback) if fallback else fallback


class _RegistryCatalog:
    """The two questions `HeadHealth` asks of a head catalog, answered from a loaded registry."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def head_profile(self, head: str) -> dict:
        return self.registry.profile(head)

    def resource(self, resource: str) -> dict:
        return self.registry.resources[resource]


def _head_health(registry: Any) -> HeadHealth:
    """Use the shared resource-health TTL cache and launch_allowed vocabulary.

    Standing/card heads share probes; unknown remains launchable.
    """
    return HeadHealth(_RegistryCatalog(registry), _installation_data_dir())


def _head_fallback(registry: Any, head: str) -> list[str] | None:
    """`head`'s fallback chain, or None when the registry does not describe it."""
    from ummanu.runtime.heads import HeadRegistryError

    try:
        return list(registry.profile(head).get("fallback") or [])
    except HeadRegistryError:
        return None


@dataclass(frozen=True)
class LaunchResolution:
    """The usable supervised head selected before rendering creates any report card."""

    #: The role's skill text as its spec has it, with no `--card` argument yet.
    skill: str
    #: The profile this launch resolved onto, and its data as the registry gave it.
    profile: str
    head_profile: dict


def _resolve_launch(
    agent: str, variant: str | None = None, snapshot: RegistrySnapshot | None = None
) -> LaunchResolution:
    """Resolve a local-PTY profile against live health using the tick snapshot.

    Raise NoSupervisedHead on resolution/render failure; no bare-CLI fallback. variant selects
    spec["variants"][variant].skill instead of the top-level skill.
    """
    snapshot = _registry_snapshot() if snapshot is None else snapshot
    spec = _load_spec(agent)
    skill = spec["variants"][variant]["skill"] if variant else spec["skill"]
    registry = snapshot.registry
    if registry is None:
        raise NoSupervisedHead(f"the head registry would not load ({snapshot.error or 'unreadable'})")
    try:
        head = _preferred_head(agent, spec, snapshot)
    except Exception as exc:  # noqa: BLE001 - any routing failure is no supervised head
        raise NoSupervisedHead(f"no head profile is routed to {agent} ({exc})") from None
    if not head:
        raise NoSupervisedHead(f"no head profile is routed to {agent}")
    try:
        health = _head_health(registry)
        choice = resolve_head_chain(head, health.check, lambda pid: _head_fallback(registry, pid))
        resolved = choice.head or head
        profile = dict(registry.profile(resolved))
    except Exception as exc:  # noqa: BLE001 - any profile failure is no supervised head
        raise NoSupervisedHead(f"head {head!r} could not be resolved to a profile ({exc})") from None
    try:
        # `from_profile` refuses every runtime but `local-pty`, so a spec is a supervised head.
        HeadSpec.from_profile(resolved, profile)
    except Exception as exc:  # noqa: BLE001 - any spec failure is no supervised head
        raise NoSupervisedHead(f"head profile {resolved!r} will not make a head spec ({exc})") from None
    resolution = LaunchResolution(skill, resolved, profile)
    # Rendered once here, without a card, so a profile whose command will not render is refused
    # before `_TickReports.command` creates a report card for it.
    _render_launch(agent, resolution)
    return resolution


def _render_launch(
    agent: str,
    resolution: LaunchResolution,
    card_ref: str | None = None,
) -> tuple[str, str, bool]:
    """Return skill, launch command and prompt-after-start, or NoSupervisedHead.

    Append card_ref to the skill before quoting/sending the prompt, after head resolution.
    """
    skill = f"{resolution.skill} --card {card_ref}" if card_ref else resolution.skill
    try:
        rendered = render_head_command(
            resolution.head_profile,
            prompt=skill,
            role=agent,
            workspace=_workspace(agent),
            binding=STANDING_BINDING,
        )
    except Exception as exc:  # noqa: BLE001 - any render failure is no supervised head
        raise NoSupervisedHead(
            f"the command for head profile {resolution.profile!r} will not render ({exc})"
        ) from None
    return skill, rendered.command, rendered.prompt_after_start


def _launch_cmd(
    agent: str,
    variant: str | None = None,
    card_ref: str | None = None,
    snapshot: RegistrySnapshot | None = None,
) -> tuple[str, str, str, bool, dict]:
    """Resolve then render: skill, command, profile name, prompt-after-start and profile data.

    Propagate NoSupervisedHead from either step.
    """
    resolution = _resolve_launch(agent, variant, snapshot)
    skill, launch, after_start = _render_launch(agent, resolution, card_ref)
    return skill, launch, resolution.profile, after_start, resolution.head_profile


def _steward_report_card(
    agent: str, variant: str | None, *, report_board: StewardReportBoard | None = None
) -> str | None:
    """Create a claimed, non-code ummanu steward report in In progress through the report-board port.

    Only steward creates one, immediately before dispatch reaches the head; others return None.
    """
    if agent != "steward":
        return None
    if report_board is None:
        raise RuntimeError("steward report board must be supplied by the composition root")
    now = datetime.now(UTC)
    kind = variant or "hourly"
    slug = f"steward-sweep-{now:%Y%m%d-%H%M%S}"
    return report_board.create_report(
        project=os.environ.get("UMMANU_META_PROJECT", "ummanu"),
        title=f"steward: {kind} sweep {now:%Y-%m-%d %H:%M UTC}",
        slug=slug,
    )


def _ensure_head_ready(ws: str, cmd: DispatchCommand, *, role: str = "service") -> None:
    """Prepare the workspace with its head adapter before supervisor start.

    Claude preparation is best-effort; Codex trust preflight is mandatory and raises before launch.
    """
    if cmd.prompt_after_start and str((cmd.head_profile or {}).get("adapter") or "") == "codex":
        spec = HeadSpec.from_profile(str(cmd.profile or role), cmd.head_profile)
        preflight_codex_launch(
            cmd.head_profile,
            ws,
            HeadRun(
                run_id=new_run_id(),
                spec=spec,
                workspace=ws,
                task_ref=TaskRef.standing(role),
                role=role,
            ),
        )
        return
    _ensure_claude_ready(ws)


def _ensure_claude_ready(ws: str) -> None:
    """Pre-answer folder trust + the onboarding theme picker before a fresh `claude` starts.

    Without this a head can land on an interactive prompt and wait forever for input nobody sends.
    Best-effort.
    """
    try:
        claude_env.ensure_trust(CLAUDE_JSON, ws)
        claude_env.ensure_theme(CLAUDE_JSON)
    except claude_env.ClaudeConfigError as e:
        print(f"dispatch: claude config prep failed ({e})")


def _standing_memory_run(agent: str, spec: HeadSpec, workspace: str, run_id: str) -> HeadRun:
    """The heartbeat path of a scheduled head: its run, and where its pid heartbeat is written."""
    return HeadRun(
        run_id=run_id,
        spec=spec,
        workspace=workspace,
        task_ref=TaskRef.standing(agent),
        role=agent,
        pid_file=str(_installation_data_dir() / "memory" / "access-grants" / "heads" / f"{run_id}.pid"),
    )


def _memory_bound_launch(agent: str, run: HeadRun, command: str) -> str:
    """Attach a scheduled role's launch-bound Memory grant to its head command."""
    if agent not in {"curator", "retro", "steward"}:
        return command
    product_root = Path(os.environ.get("UMMANU_REPO") or _REPO_ROOT)
    grant = " ".join(
        (
            f"PYTHONPATH={shlex.quote(str(product_root / 'src'))}",
            "python3 -m ummanu.memory.grant_env",
            f"--head-run {shlex.quote(json.dumps(run.to_json(), separators=(',', ':')))}",
            f"--subject {shlex.quote(json.dumps({'kind': 'standing', 'ref': agent}, separators=(',', ':')))}",
            f"--data-dir {shlex.quote(str(_installation_data_dir()))}",
        )
    )
    # `exec env`, not bare `exec`: the rendered head command leads with installation assignments
    # (`UMMANU_RUNTIME_ENV_FILE=… UMMANU_INSTANCE=… python3 …`) and POSIX `exec` takes them as the program
    # name, so the head exits 127 before its prompt is delivered.
    launch = f'grant="$({grant})"; export "$grant"; exec env {command}'
    return f"/bin/sh -c {shlex.quote(launch)}"


def _memory_heartbeat(run: HeadRun, command: str) -> str:
    return with_pid_heartbeat(
        command,
        run.pid_file,
        identity={"run_id": run.run_id, "role": run.role, "task": f"{run.task_ref.kind}:{run.task_ref.ref}"},
    )


def _move_report(
    state: AgentState,
    event: str,
    reference: str | None,
    *,
    target: Literal["done", "blocked"],
    reason: str,
    action: str,
    report_board: StewardReportBoard | None,
    cause: BaseException | None = None,
) -> None:
    """Settle one steward report card no head will write: forget it, move it, log the outcome.

    A failed move is logged, never raised, so the caller's own outcome stands. `cause` is the
    failure the move answers when the log names it (a preflight escalation); without it the reason
    alone carries the story. A reference of None or "" means there is no card to settle.
    """
    if not reference:
        return
    state.clear_active_report(reference)
    try:
        if report_board is None:
            raise RuntimeError("steward report board must be supplied by the composition root")
        report_board.move_report(reference=reference, target=target, reason=reason)
        named = {} if cause is None else {"error": str(cause)}
        state.log_run(event, action=action, result=target, reference=reference, **named)
    except Exception as error:  # noqa: BLE001 - a failed move is logged, not raised
        detail = str(error) if cause is None else f"{cause} (escalation failed: {error})"
        state.log_run(event, action=action, result="failed", reference=reference, error=detail)


def _active_reference(state: AgentState) -> str | None:
    """The standing steward report this role recorded; other roles have no reporting contract."""
    return (state.load_active_report() or {}).get("reference")


class _TickReports:
    """Own the rendered report and any standing report inherited by stopping its head.

    run enters once around the tick; every exit discharges outstanding obligations. At most one
    command/report is created. taken, undispatched, failed and preflight_failed settle it; close
    anything unsettled at exit, preserving hold_still for refused ticks.
    """

    NOTHING_DISPATCHED = (
        "this tick dispatched nothing after all, so the report card it made is closed unwritten."
    )

    def __init__(
        self, agent: str, state: AgentState, event: str, *, report_board: StewardReportBoard | None = None
    ) -> None:
        self.agent = agent
        self.state = state
        self.event = event
        self.report_board = report_board
        #: This tick's own card, and whether it has been discharged. Nothing is outstanding until
        #: a command carrying one exists.
        self.cmd: DispatchCommand | None = None
        self.settled = True
        #: Set by a refused tick: it leaves every standing record and report as it found them.
        self.hold_still = False

    def __enter__(self) -> _TickReports:  # noqa: PYI034 - a final class; Self buys nothing here
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.cmd is not None and not self.settled:
            if isinstance(exc, CodexPreflightError):
                self.preflight_failed(self.cmd, exc)
            elif exc is not None:
                self.failed(self.cmd, exc)
            else:
                self.undispatched(self.cmd, self.NOTHING_DISPATCHED)
        self._close_an_orphan()
        return False

    def command(self, variant: str | None, resolution: LaunchResolution) -> DispatchCommand:
        """Render the command and record its steward report as outstanding.

        Create only after usable local-PTY resolution; re-render with its card in the skill. If that
        render refuses, close the unwritten card before propagating to _fail_closed.
        """
        card_ref = _steward_report_card(self.agent, variant, report_board=self.report_board)
        self.cmd = DispatchCommand(resolution.skill, "", resolution.profile, card_ref)
        self.settled = card_ref is None
        try:
            skill, launch, after_start = _render_launch(self.agent, resolution, card_ref)
        except NoSupervisedHead as refusal:
            self.undispatched(self.cmd, f"{self.NOTHING_DISPATCHED}\n\nrefusal: {refusal.reason}")
            raise
        self.cmd = DispatchCommand(
            skill,
            launch,
            resolution.profile,
            card_ref,
            prompt_after_start=after_start,
            head_profile=resolution.head_profile,
        )
        return self.cmd

    def taken(self, cmd: DispatchCommand, handle: str | None) -> None:
        """A head has this card now: it is that head's to write, and the record says whose."""
        self.state.save_active_report(cmd.card_ref, handle)
        self.settled = True

    def undispatched(self, cmd: DispatchCommand, note: str) -> None:
        _move_report(self.state, self.event, cmd.card_ref, target="done", reason=note,
                     action="dispatch-release", report_board=self.report_board)
        self.settled = True

    def failed(self, cmd: DispatchCommand, failure: BaseException) -> None:
        _move_report(
            self.state, self.event, cmd.card_ref, target="done",
            reason=(
                "steward dispatch failed before the head accepted the report-card run.\n\n"
                f"failure: {failure}"
            ),
            action="dispatch-recovery", report_board=self.report_board,
        )
        self.settled = True

    def preflight_failed(self, cmd: DispatchCommand, failure: BaseException) -> None:
        _move_report(
            self.state, self.event, cmd.card_ref, target="blocked",
            reason=(
                "steward dispatch could not prepare the head workspace, so no head was started "
                "and no sweep ran.\n\n"
                f"failure: {failure}"
            ),
            action="dispatch-preflight", report_board=self.report_board, cause=failure,
        )
        self.settled = True

    def _close_an_orphan(self) -> None:
        """Close an active report only with neither head_run nor terminal_handle recording its writer.

        Any terminal_handle.json existence counts as ownership. A refused hold_still tick preserves
        standing reports; other roles have no report contract.
        """
        if self.hold_still:
            return
        try:
            if self.state.load_active_report() is None:
                return
            if self.state.terminal_handle_file.exists():
                return
            if self.state.load_head_run() is not None:
                return
        except Exception:  # noqa: BLE001 - an unreadable head record leaves the report alone
            return
        _move_report(
            self.state,
            self.event,
            _active_reference(self.state),
            target="done",
            reason="the head that was writing this report is no longer this role's recorded head, so "
            "the report was closed unwritten by the tick that found it ownerless.",
            action="owner-report-release",
            report_board=self.report_board,
        )


def _codex_skill_prompt(skill: str) -> str:
    """Render the portable slash-named automation skill for Codex's composer."""
    if not skill.startswith("/"):
        return skill
    name, separator, arguments = skill[1:].partition(" ")
    # A bare `$name` opens Codex's skill picker and consumes Enter by selecting the match instead
    # of submitting a turn.  The trailing space accepts the exact mention before the delivery
    # path's one Enter; an invocation with arguments already has that separator.
    return f"${name} {arguments}" if separator else f"${name} "


class LocalPtyDispatchError(RuntimeError):
    """A head this driver holds itself could not be brought up, as its own receipt said so."""


def _local_pty_runtime() -> Any:
    """Build the supervised backend through the shared product mapping.

    Use the installation heads/ root and canonical launch-identity reader, shared with dispatch.
    """
    return build_head_runtime(
        LOCAL_PTY_RUNTIME,
        local_pty_root=lambda: _installation_data_dir() / "heads",
        head_process_status=head_process_status,
    )


#: Who ended a head this driver was holding. A stop names its initiator, and this is the one this
#: driver makes.
HANDOVER_INITIATOR = "triggered-agent-dispatch"
FAILED_BRING_UP_REASON = "this tick's bring-up failed, so the head it raised is nobody's"
IDLE_HEAD_REASON = "its turn ended and it sat idle, so this tick retires it and raises a fresh head"
PROVIDER_FAILURE_REASON = (
    "its provider refused its turn, so this tick records the resource red and raises the role on the "
    "next head of its chain"
)
#: The `runs.jsonl` action of a tick that retired the previous tick's finished, idle head.
SUPERVISED_IDLE_STOP = "supervised-idle-stop"
#: The `runs.jsonl` action of a tick that retired a head its provider refused (ummanu-108).
SUPERVISED_PROVIDER_FAILURE = "supervised-provider-failure"
#: How long a standing head's turn must have been over before a tick may retire it. A head whose
#: adapter never exits on its own (Codex's TUI) otherwise holds its role off duty for good: every
#: later tick is a busy-skip over a head that has nothing left to do. The grace keeps a head that
#: only paused inside its turn from being taken for finished.
IDLE_HEAD_GRACE_SECONDS = 600.0
#: How long the tick waits for a retired head's supervisor to release its socket.
IDLE_STOP_SUPERVISOR_EXIT_SECONDS = 10.0
#: The launch identity a supervised head writes beside its socket in its run directory (the
#: substrate's `PID_FILE_NAME`, which this driver may not import).
HEAD_PID_FILE_NAME = "head.pid"


@dataclass(frozen=True)
class _StandingPromptTransport:
    """Deliver a standing skill through the supervised backend without a before_send hook.

    The backend owns settling, typing, submitting and turn confirmation.
    """

    before_send: Callable[[], Any] | None = None


_STANDING_PROMPT_TRANSPORT = _StandingPromptTransport()


def _fail_closed(
    agent: str, state: AgentState, event: str, reports: _TickReports, refusal: NoSupervisedHead
) -> int:
    """Record no-supervised-head/error with cause, print stderr and return nonzero.

    Raise/stop no head, create/close no report, and preserve head_run.json and active_report.json.
    """
    reports.hold_still = True
    state.log_run(event, action=NO_SUPERVISED_HEAD, result="error", error=refusal.reason)
    print(f"dispatch[{agent}]: no supervised head to raise — {refusal.reason}", file=sys.stderr)
    return REFUSED_EXIT


def _retire_idle_head(
    agent: str, runtime: Any, prior: dict | None, state: AgentState, event: str, reports: _TickReports
) -> None:
    """Retire only a positively quiescent live head idle for IDLE_HEAD_GRACE_SECONDS.

    Require no open turn/delivery or new journal activity. stop_if_quiescent guards concurrent
    turns; unreadable/busy/refused stops change nothing. Close a retired steward report.
    """
    if not prior:
        return
    try:
        run = HeadRun.from_json(prior)
        seen = runtime.observe(run)
    except Exception as exc:  # noqa: BLE001 - an unreadable head is the bring-up's to refuse
        print(f"dispatch[{agent}]: could not observe the recorded head ({type(exc).__name__}: {exc})")
        return
    if not seen.ok or seen.busy is not False or not seen.last_output_at:
        return
    idle = datetime.now(UTC).timestamp() - float(seen.last_output_at)
    if idle < IDLE_HEAD_GRACE_SECONDS:
        return
    stopped = runtime.stop_if_quiescent(
        run,
        StopInitiator(actor=HANDOVER_INITIATOR, reason=IDLE_HEAD_REASON),
        expected_activity_epoch=seen.epoch,
        head_process_alive=True,
    )
    if not stopped.ok:
        print(
            f"dispatch[{agent}]: the idle head {run.run_id} was not stopped ({stopped.reason or stopped.status})"
        )
        return
    _forget_stopped_head(runtime, run)
    state.log_run(event, action=SUPERVISED_IDLE_STOP, reference=run.run_id, idle_seconds=int(idle))
    print(f"dispatch[{agent}]: retired the idle head {run.run_id} after {int(idle)}s")
    _move_report(
        state,
        event,
        _active_reference(state),
        target="done",
        reason="the head that was writing this report finished its turn and was retired idle, "
        "so the report was closed by the tick that raised its successor.",
        action="owner-report-release",
        report_board=reports.report_board,
    )


def _forget_stopped_head(runtime: Any, run: HeadRun) -> None:
    """Clear what a confirmed-stopped head leaves, so the bring-up can reuse its run id."""
    # The stop left this runtime holding the head's admission closed; the bring-up that follows
    # reuses the run id, exactly as it does over a head that ended on its own.
    runtime.forget_head(run.run_id)
    # The stop confirms the head's exit; its supervisor unlinks the socket a moment later. The
    # bring-up reuses the run directory, so it waits for that rather than probing the old socket.
    socket = Path(run.handle) if run.handle else None
    deadline = time.monotonic() + IDLE_STOP_SUPERVISOR_EXIT_SECONDS
    while socket is not None and socket.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    # The bring-up reuses this run id and directory, and its launch is confirmed by a launch identity
    # naming that run id: the dead head's record would confirm it before the new head wrote its own.
    # The stop has confirmed that process gone, so its identity records are debris.
    for record in (
        socket.parent / HEAD_PID_FILE_NAME if socket is not None else None,
        Path(run.pid_file) if run.pid_file else None,
    ):
        if record is not None:
            for path in (record, Path(f"{record}.leaf")):
                path.unlink(missing_ok=True)


def _retire_refused_head(
    agent: str, registry: Any, state: AgentState, event: str, reports: _TickReports
) -> None:
    """Retire a provider-refused head and mark its resource red before resolving a replacement.

    Use its reset or bounded backoff; the next head resumes durable state. Unreadable evidence
    changes nothing.
    """
    from ummanu.dispatch.provider_failure import provider_failure_for_persisted_run

    try:
        prior = state.load_head_run()
        if not prior:
            return
        reading = provider_failure_for_persisted_run(prior, local_pty_root=_installation_data_dir() / "heads")
    except Exception:  # noqa: BLE001 - an unreadable prior head is no verdict
        return
    if reading.get("state") != "failed":
        return
    error = reading.get("error") if isinstance(reading.get("error"), dict) else {}
    kind = str(error.get("kind") or "")
    summary = " ".join(str(error.get("summary") or kind or "provider error").split())[:200]
    resource = str(reading.get("resource") or "")
    now = time.time()
    until = failure_until(kind, float(error.get("reset_at") or 0.0), now)
    if resource:
        try:
            _head_health(registry).record(
                resource,
                failure_status(kind),
                f"provider error in a turn of {agent} head {reading.get('head') or ''}: {summary}",
                now=now,
                until=until,
            )
        except Exception as exc:  # noqa: BLE001 - the stop below does not depend on it
            print(f"dispatch[{agent}]: could not record {resource} red ({type(exc).__name__}: {exc})")
    runtime = _local_pty_runtime()
    try:
        run = HeadRun.from_json(prior)
        stopped = runtime.stop(run, StopInitiator(actor=HANDOVER_INITIATOR, reason=PROVIDER_FAILURE_REASON))
    except Exception as exc:  # noqa: BLE001 - an unstoppable head is the bring-up's to refuse
        print(f"dispatch[{agent}]: could not stop the refused head ({type(exc).__name__}: {exc})")
        return
    if not stopped.ok:
        print(f"dispatch[{agent}]: the refused head {run.run_id} was not stopped ({stopped.reason or stopped.status})")
        return
    _forget_stopped_head(runtime, run)
    state.log_run(
        event,
        action=SUPERVISED_PROVIDER_FAILURE,
        reference=run.run_id,
        error=f"{resource or '(unnamed resource)'} refused the turn: {summary}; red until {until_text(until)}",
    )
    print(f"dispatch[{agent}]: {resource} refused head {run.run_id} ({summary}); red until {until_text(until)}")
    _move_report(
        state,
        event,
        _active_reference(state),
        target="done",
        reason=f"the head writing this report was refused by its provider ({resource}: {summary}), so it "
        "was retired and the role was raised on the next head of its chain.",
        action="owner-report-release",
        report_board=reports.report_board,
    )


def _supervised_bring_up(
    agent: str,
    ws: str,
    state: AgentState,
    event: str,
    cmd: DispatchCommand,
    *,
    reports: _TickReports | None = None,
) -> int:
    """Launch the supervised head with its skill and read the typed backend receipt.

    Persist run id/workspace/spec for later ticks. Passing that owner to start refuses an already
    working head with HEAD_BUSY before spawning another.
    """
    reports = _TickReports(agent, state, event) if reports is None else reports
    try:
        _ensure_head_ready(ws, cmd, role=agent)
    except CodexPreflightError as exc:
        reports.preflight_failed(cmd, exc)
        raise
    runtime = _local_pty_runtime()
    prior = state.load_head_run()
    _retire_idle_head(agent, runtime, prior, state, event, reports)
    spec = HeadSpec.from_profile(str(cmd.profile or agent), dict(cmd.head_profile or {}))
    # A standing duty, not a card. The run id and the task binding have to be the same facts every
    # tick, because they are what the head's own launch-identity record is compared against when a
    # later tick asks whether it is still up. The steward's report card is an argument of the
    # skill and travels in the prompt.
    task_ref = TaskRef.standing(agent)
    run_id = str((prior or {}).get("run_id") or "") or new_run_id()
    run = _standing_memory_run(agent, spec, ws, run_id)
    # An adapter that takes its prompt on its command line is launched with it; one that starts
    # with an empty composer is pointed at its skill across the same boundary that raised it. The
    # transport is what makes that pointer an agent's prompt rather than a bare line: settled,
    # typed, submitted on its own and confirmed by a turn starting, exactly as a card head's launch
    # prompt is (secretary-1717).
    pointer = NudgePointer.line(_codex_skill_prompt(cmd.skill)) if cmd.prompt_after_start else None
    receipt = runtime.start(
        spec,
        ws,
        task_ref,
        command=f"/bin/sh -c {shlex.quote(_memory_heartbeat(run, _memory_bound_launch(agent, run, cmd.launch)))}",
        title=f"triggered-agent:{agent}",
        pointer=pointer,
        transport=_STANDING_PROMPT_TRANSPORT if pointer is not None else None,
        run_id=run_id,
        role=agent,
        run=run,
        subject=f"{agent}-dispatch",
    )
    if receipt.status == HEAD_BUSY:
        # A head this role already has is still working. For a mechanical role a missed tick is the
        # normal answer to that — it has a watermark and a precheck — and the refusal is made
        # before anything is spawned, so no second head exists and no second skill was sent. The
        # report card this tick made is closed, because no head will ever write it; the report the
        # working head is writing is untouched, because that head is untouched.
        refusal = str((receipt.evidence or {}).get("refusal") or "busy")
        reports.undispatched(
            cmd,
            "the head of this role is still working, so this tick raised none and delivered "
            f"nothing.\n\nrefusal: {refusal}\n{receipt.reason}",
        )
        state.log_run(event, action="supervised-busy-skip", reference=cmd.card_ref, error=refusal)
        print(f"dispatch[{agent}]: head {run_id} is still up ({refusal}) — no dispatch")
        return 0
    if not receipt.ok:
        # Nothing of a failed bring-up is recorded as this role's head. A prompt that was typed and
        # never started a turn has already been stopped by `start`; a head its stop would not
        # confirm is asked once more here, because an unrecorded live head is one no later tick
        # would ever end. Either way the tick fails, and the unit records it.
        if receipt.status == HEAD_ALIVE and receipt.run is not None:
            stopped = runtime.stop(
                receipt.run,
                StopInitiator(actor=HANDOVER_INITIATOR, reason=FAILED_BRING_UP_REASON),
            )
            if not stopped.ok:
                print(
                    f"dispatch[{agent}]: the head {receipt.run.run_id} this tick failed to point at "
                    f"its skill would not confirm it stopped ({stopped.reason or stopped.status})"
                )
        failure = LocalPtyDispatchError(receipt.reason or receipt.status)
        reports.failed(cmd, failure)
        state.log_run(
            event,
            action="supervised-start-failed",
            result="error",
            reference=cmd.card_ref,
            error=receipt.reason or receipt.status,
        )
        raise failure
    live = receipt.run
    state.save_head_run(live.to_json())
    reports.taken(cmd, live.handle)
    state.log_run(event, action="supervised-started", reference=cmd.card_ref)
    print(f"dispatch[{agent}]: raised a supervised head {live.run_id} -> {cmd.skill}")
    return 0


def run(
    agent: str,
    variant: str | None = None,
    cleanup_only: bool = False,
    *,
    report_board: StewardReportBoard | None = None,
) -> int:
    """Run one locked tick with report obligations settled by _TickReports on every exit.

    variant selects prompt/telemetry. cleanup_only returns 0 before state/lock creation: the
    supervisor reaps heads, while systemd callers retain the accepted option.
    """
    if cleanup_only:
        return 0
    if agent == "steward" and report_board is None:
        raise RuntimeError("steward report board must be supplied by the composition root")
    ws = _workspace(agent)
    state = AgentState(agent)
    event = variant or "dispatch"
    with state.lock(), _TickReports(agent, state, event, report_board=report_board) as reports:
        return _tick(agent, variant, ws, state, event, reports)


def _tick(
    agent: str,
    variant: str | None,
    ws: str,
    state: AgentState,
    event: str,
    reports: _TickReports,
) -> int:
    """Dispatch under the run lock and reports scope in contract order.

    Read registry once, resolve or fail closed, fence any recorded pane owner, then render the
    command/report. A tick refused before rendering creates no report.
    """
    if _pipeline_paused():
        state.log_run(event, action="paused")
        print(f"dispatch[{agent}]: pipeline paused — no dispatch")
        return 0
    registry = _registry_snapshot()
    if registry.registry is not None:
        _retire_refused_head(agent, registry.registry, state, event, reports)
    try:
        resolution = _resolve_launch(agent, variant, registry)
    except NoSupervisedHead as refusal:
        return _fail_closed(agent, state, event, reports, refusal)
    if state.terminal_handle_file.exists():
        # `terminal_handle.json` is the retired pane backend's record of this role's head. This
        # driver cannot reach a pane to confirm it is gone and never deletes the record, so it
        # raises nothing beside it: two live heads for one role is the one outcome it may not
        # produce. Existence is the fence: an empty, unreadable or handle-less record still says
        # a pane may be up. An operator removes the file once that pane is confirmed gone.
        reports.hold_still = True
        reason = (
            "a pane is still recorded as this role's head (terminal_handle.json); "
            "remove it once that pane is confirmed gone"
        )
        state.log_run(event, action=SUPERVISED_OWNER_CONFLICT, result="error", error=reason)
        print(f"dispatch[{agent}]: raising no supervised head — {reason}", file=sys.stderr)
        return REFUSED_EXIT
    try:
        cmd = reports.command(variant, resolution)
    except NoSupervisedHead as refusal:
        return _fail_closed(agent, state, event, reports, refusal)
    return _supervised_bring_up(agent, ws, state, event, cmd, reports=reports)
