"""Singleton head driver, shared by every triggered-agent (curator, retro, steward).

One agent = at most one live head, held under a supervisor of this product's own (`local-pty`).
On a trigger (after precheck passes, under the run lock) a tick resolves which head this agent
gets and then does exactly one of these:

  * the resolution names a `local-pty` head -> raise it under its supervisor with its skill
  * ...and this role's head is still working -> the supervisor refuses, dispatch nothing
  * the resolution names no `local-pty` head  -> fail closed: raise nothing, record why, exit 1

`local-pty` is the one backend. There is no pane lifecycle here any more (A20, secretary-1720):
no warm reuse, no `/clear`, no ghost-tab reap, no idle probe, no watchdog restart and no finalizer
trailer. Every one of those existed to cope with the dead pty Orca keeps as a tab in its session
store; a supervisor leaves none behind, so a tick is one head raised with its own skill and the run
ends when that head exits. Nothing in `ummanu.automations` reaches Orca, and
`tests/test_architecture.py` keeps it that way.

Fail closed, in one place. A tick whose resolution does not name a usable `local-pty` head has no
`HeadSpec` a supervisor could raise, so `_resolve_launch` refuses it with `NoSupervisedHead` and
`_tick` hands the refusal to `_fail_closed`. The causes are: the head registry would not load, no
profile is routed to the role, the profile will not make a `HeadSpec`, the profile names a runtime
other than `local-pty`, and the command will not render. The refusal is decided before a command is
built, so it creates no steward report card; `_fail_closed` records one `runs.jsonl` entry
(`action="no-supervised-head"`, `result="error"`, the reason), prints the reason to stderr and the
tick exits 1, which the systemd unit records as a failed run. A paused pipeline is not a refusal:
it still exits 0 with `action="paused"`.

The resolution is decided off one reading of the registry. `run()`'s tick takes a
`RegistrySnapshot` once, and the routed profile, its fallback chain and the resolved profile's
runtime all come out of that one reading, so an ordinary profile publication landing mid-tick
cannot make the tick act on two different registries.

The head outlives the tick that raised it; `AgentState.save_head_run` is what a later tick reaches
it through, and handing that record back to `start` is what makes a bring-up over a head that is
still working a refusal (`HEAD_BUSY`) rather than a second head. A failed-closed tick changes
nothing: it raises no head and stops none, leaves `head_run.json` and `active_report.json` as they
are and closes no report. A head an earlier tick raised finishes its turn under its own supervisor,
and the next tick with a usable profile finds it through `head_run.json` as usual. A head that
finished its turn and stayed up idle is retired by the next tick (`_retire_idle_head`), so a role is
not held off duty by a head with nothing left to do.

One role, one owner of its head. A `terminal_handle.json` left over from the retired pane backend
names a pane as this role's owner. The fence is the file's existence, not its parsed content, so an
empty, unreadable or handle-less record fences too. This driver never deletes it and never raises a
head beside it: the tick refuses with `action="supervised-owner-conflict"` and exits 1 until an
operator has confirmed that pane is gone and removed the file.

A report card belongs to a head, so stopping the head is what closes it. The steward's report card
is created by the render of the skill that names it and is written by the head that render is
launched with; `active_report.json` is which card that is and which head has it. That card, and the
card a tick built but never handed to a head, are one tick-long obligation with one place that
discharges it: `_TickReports`, entered by `run()` around the whole tick, so every terminal path of
`_tick` — bring-up, busy-skip, every fail-closed bail, every raise — leaves through it. A refused
tick holds still: it discharges nothing standing, not even an ownerless report.
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

from ummanu.head_health import HeadHealth, resolve_head_chain
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
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))

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
    """The report-card surface the head driver actually needs.

    It is deliberately narrower than either board implementation.  A future
    composition root may pass Ummanu's canonical adapter without making the
    triggered-agent runtime import Ummanu back.
    """

    def create_report(self, *, project: str, title: str, slug: str) -> str: ...

    def move_report(self, *, reference: str, target: Literal["done", "blocked"], reason: str) -> None: ...


class NoSupervisedHead(Exception):
    """This tick has no `local-pty` head to raise, and `reason` says which cause it was.

    Raised only by the resolution and the render, before any head exists, and caught only by
    `_tick`, which hands it to `_fail_closed`. It never escapes a tick.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _workspace(agent: str) -> str:
    return os.environ.get("TA_WORKSPACE") or str(Path.home() / "orca/workspaces/ummanu" / agent)


def _load_spec(agent: str) -> dict:
    return tomllib.loads(
        (_REPO_ROOT / "src" / "ummanu" / "automations" / "agents" / agent / "automation.toml").read_text()
    )


def _pipeline_paused() -> bool:
    """Whether the pipeline-wide pause flag (triggered-agents-281, agents/pipeline/pause.py) is
    set — checked first thing in a tick, before the registry is read, so a paused pipeline never
    spends a token on steward/curator/retro either: none of them carry an in-flight card of their
    own the way a worker/reviewer head does, so pause has no "let it finish its cycle" case here in
    either mode, soft or hard. Lazy import, same reason as the lazy imports elsewhere in this
    module — this module is imported at process start by every agent, so a top-level import back
    into agents.pipeline would risk a circular import the first time either side changes its own
    imports. Any failure is a pause: dispatching while an operator's stop condition cannot be read
    is worse than deferring one tick. The warning goes to the service log every affected tick
    until the state is repaired."""
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
    """The head registry as one tick read it, once.

    A tick asks the registry which profile this agent is routed to, which profile of its fallback
    chain health resolves it onto, and what runtime that profile names. An ordinary profile
    publication is atomic but not instantaneous relative to a scheduled tick, so the reading is
    taken once and every one of those questions is answered from it.

    `registry` is `None` when the registry could not be read at all, and `error` then says why.
    Such a tick fails closed: with no registry there is no profile, and with no profile no spec a
    supervisor could raise a head from.
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
    """The head this agent launches on: the selected registry's role default for it.

    The spec's own `head` is the last resort for a registry that routes this role nowhere, and it
    goes through the registry's own resolution. A resolution refusal reaches the caller: a spec
    naming a head the registry does not define is a dispatch that must not happen.

    `snapshot` is the tick's one reading of the registry (`RegistrySnapshot`), so this answer and
    the resolution's cannot come from two different registries.
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
    """The installation's one resource-health cache, the one the production dispatcher probes into.

    A standing agent reads readiness through the same `ummanu.head_health` TTL cache and status
    vocabulary as a card head, so a resource is probed once per window for both and "red" means
    one thing: not `launch_allowed`. `unknown` stays launchable, as it is for cards.
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
    """Which head this agent gets this tick: a profile whose head a supervisor can hold.

    A resolution, not a dispatch, and it exists before any report card does. The card is created by
    the render that names it in the skill, so everything this tick decides about the head — above
    all whether there is one it may raise — is decided on the resolution, before a card exists to
    be left behind.
    """

    #: The role's skill text as its spec has it, with no `--card` argument yet.
    skill: str
    #: The profile this launch resolved onto, and its data as the registry gave it.
    profile: str
    head_profile: dict


def _resolve_launch(
    agent: str, variant: str | None = None, snapshot: RegistrySnapshot | None = None
) -> LaunchResolution:
    """Resolve this agent's head against this run's live resource health, or refuse.

    The head comes from `_preferred_head` and resolves through the same registry machinery a
    worker or reviewer head gets. Every way that fails to end on a `local-pty` profile whose
    command renders raises `NoSupervisedHead` naming the cause; there is no bare-`claude`
    fallback, because a launch with no usable profile has no spec to raise a supervised head from.

    `variant` reads `skill` from `spec["variants"][variant]` instead of the top-level one.

    `snapshot` is the tick's one reading of the registry.
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
    """(skill, launch command, prompt-after-start) for a resolution, or `NoSupervisedHead`.

    `card_ref` appends `--card <ref>` to the skill text BEFORE it is handed to the head, so the
    augmented text is what actually gets sent rather than landing outside the quoted prompt. It is
    the whole reason rendering is a separate step from resolving: a card is younger than the head
    it names, and every question this tick asks about the head is older than the card.
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
    """(skill, full launch command, resolved head profile, prompt-after-start, profile data) from
    the agent's automation.toml: `_resolve_launch` and then `_render_launch`, for a caller that
    wants both halves at once. Raises `NoSupervisedHead` where either of them does.
    """
    resolution = _resolve_launch(agent, variant, snapshot)
    skill, launch, after_start = _render_launch(agent, resolution, card_ref)
    return skill, launch, resolution.profile, after_start, resolution.head_profile


def _steward_report_card(
    agent: str, variant: str | None, *, report_board: StewardReportBoard | None = None
) -> str | None:
    """Create the steward's own wake-up report card (project ummanu, non-code type,
    straight into In progress, already claimed by itself — through the supplied
    Ummanu-owned report-board port)
    right before a dispatch actually reaches the head. None for every agent but steward
    (triggered-agents-255): the rest keep their existing dispatch untouched.
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
    """Prepare `ws` for the head about to be raised in it, on that head's own adapter.

    The first-run question an adapter asks is the one thing that can make a fresh head never come
    up at all, and which question it is depends on the adapter, so this is the one place that
    branches on it, right before the supervisor starts the head. The two failure modes differ
    deliberately: Claude's preparation stays best-effort, while the Codex preflight is a hard
    precondition — without the trust entry the head cannot reach readiness — and it raises before
    any head is started.
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
    return f"env $({grant}) {command}"


def _memory_heartbeat(run: HeadRun, command: str) -> str:
    return with_pid_heartbeat(
        command,
        run.pid_file,
        identity={"run_id": run.run_id, "role": run.role, "task": f"{run.task_ref.kind}:{run.task_ref.ref}"},
    )


def _recover_steward_dispatch_failure(
    state: AgentState,
    event: str,
    cmd: DispatchCommand,
    failure: BaseException,
    *,
    report_board: StewardReportBoard | None = None,
) -> None:
    """Close out a steward report card whose head was brought up but never took the run."""
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    body = f"steward dispatch failed before the head accepted the report-card run.\n\nfailure: {failure}"
    try:
        if report_board is None:
            raise RuntimeError("steward report board must be supplied by the composition root")
        report_board.move_report(reference=cmd.card_ref, target="done", reason=body)
        state.log_run(event, action="dispatch-recovery", result="done", reference=cmd.card_ref)
    except Exception as recovery_error:  # noqa: BLE001 - a failed recovery is logged, not raised
        state.log_run(
            event,
            action="dispatch-recovery",
            result="failed",
            reference=cmd.card_ref,
            error=str(recovery_error),
        )


def _release_steward_report(
    state: AgentState,
    event: str,
    cmd: DispatchCommand,
    note: str,
    *,
    report_board: StewardReportBoard | None = None,
) -> None:
    """Close a steward report card whose tick turned out to dispatch nothing after all.

    The card is created by the same call that renders the skill naming it, so a tick cannot know
    it will refuse until it holds one. Leaving it open would park a report in progress that no head
    is ever going to write, so it is closed with the reason it went unused.
    """
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    try:
        if report_board is None:
            raise RuntimeError("steward report board must be supplied by the composition root")
        report_board.move_report(reference=cmd.card_ref, target="done", reason=note)
        state.log_run(event, action="dispatch-release", result="done", reference=cmd.card_ref)
    except Exception as error:  # noqa: BLE001 - a failed release is logged, not raised
        state.log_run(
            event, action="dispatch-release", result="failed", reference=cmd.card_ref, error=str(error)
        )


def _escalate_steward_preflight_failure(
    state: AgentState,
    event: str,
    cmd: DispatchCommand,
    failure: BaseException,
    *,
    report_board: StewardReportBoard | None = None,
) -> None:
    """Put a steward report card in front of a human when its workspace could not be prepared.

    The preflight fails before any head is started, so no head has seen this card. Closing it as
    Done would record a sweep that never ran, and the condition — an untrusted repository root, a
    codex config the launcher may not rewrite — does not heal on its own, so the card goes to
    Blocked with the preflight's reason. A card that cannot even be moved leaves its reason in the
    run log rather than raising over the real cause.
    """
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    body = (
        "steward dispatch could not prepare the head workspace, so no head was started "
        "and no sweep ran.\n\n"
        f"failure: {failure}"
    )
    try:
        if report_board is None:
            raise RuntimeError("steward report board must be supplied by the composition root")
        report_board.move_report(reference=cmd.card_ref, target="blocked", reason=body)
        state.log_run(
            event, action="dispatch-preflight", result="blocked", reference=cmd.card_ref, error=str(failure)
        )
    except Exception as escalation_error:  # noqa: BLE001 - a failed escalation is logged, not raised
        state.log_run(
            event,
            action="dispatch-preflight",
            result="failed",
            reference=cmd.card_ref,
            error=f"{failure} (escalation failed: {escalation_error})",
        )


def _release_standing_report(
    state: AgentState,
    event: str,
    note: str,
    *,
    report_board: StewardReportBoard | None = None,
) -> None:
    """Close the steward report card in `active_report.json` whose writer is no longer recorded.

    The card belongs to the head recorded as this role's owner, not to the tick that finds it.
    With no such head left, nobody is going to write it, so it is closed with the reason.

    Nothing for a role with no reporting contract: only a steward dispatch ever records a
    reference here, so for curator and retro this reads an empty record and returns.
    """
    reference = (state.load_active_report() or {}).get("reference")
    if not reference:
        return
    state.clear_active_report(reference)
    try:
        if report_board is None:
            raise RuntimeError("steward report board must be supplied by the composition root")
        report_board.move_report(reference=reference, target="done", reason=note)
        state.log_run(event, action="owner-report-release", result="done", reference=reference)
    except Exception as error:  # noqa: BLE001 - a failed release is logged, not raised
        state.log_run(
            event, action="owner-report-release", result="failed", reference=reference, error=str(error)
        )


class _TickReports:
    """Everything one tick owes a steward report card, and the one place that discharges it.

    A tick owes two cards, and neither of them used to have a single owner in this module. One is
    its own: `command` creates a report card as it renders the skill that names it, so a tick that
    turns out to dispatch nothing after all is holding a report nobody will ever write.
    The other was already standing when the tick began — the card in `active_report.json`, being
    written by the head this role already had — and a tick that stops that head inherits it.

    Both are discharged through this object, and `run()` enters it once around the whole tick, so
    every terminal path of the tick leaves through `__exit__`: the bring-up, the busy-skip, the
    stop of a standing owner, every fail-closed bail and every raise. The question is
    never "did this branch remember" but "what is still outstanding", which is the only form of it
    a branch cannot get wrong by being added later.

    A tick builds at most one command, so at most one card is ever this tick's own. Handing it to
    a head (`taken`) is what settles it; so is closing it (`undispatched`), recording a dispatch
    that failed after the head was up (`failed`), and escalating a workspace that could not hold a
    head at all (`preflight_failed`). Anything still unsettled when the tick ends is closed here.
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
        """This tick's dispatch command, and the card it carries, recorded as outstanding.

        The one spot that creates the steward's report card, so every real dispatch carries one and
        a tick that dispatches nothing never does. It is reached only once the resolution has named
        a `local-pty` head whose command renders, so a failed-closed tick creates no card. The
        render is repeated here with the card in the skill; should that one refuse after all, the
        card is closed unwritten before the refusal goes on to `_fail_closed`.
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
        _release_steward_report(self.state, self.event, cmd, note, report_board=self.report_board)
        self.settled = True

    def failed(self, cmd: DispatchCommand, failure: BaseException) -> None:
        _recover_steward_dispatch_failure(
            self.state, self.event, cmd, failure, report_board=self.report_board
        )
        self.settled = True

    def preflight_failed(self, cmd: DispatchCommand, failure: BaseException) -> None:
        _escalate_steward_preflight_failure(
            self.state, self.event, cmd, failure, report_board=self.report_board
        )
        self.settled = True

    def _close_an_orphan(self) -> None:
        """The backstop under the above: a report with no owner left anywhere.

        `active_report.json` names a card and its writer at once, and the writer of a live head is
        recorded in `head_run.json` — or, left over from the retired pane backend and never removed
        by this driver, `terminal_handle.json`, which counts whenever the file exists. A tick that
        ends with a report standing and neither record standing has lost that card's writer, and the
        card is closed here rather than left in progress for a later tick to overwrite. A refused
        tick (`hold_still`) skips this: it changes nothing it found.
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
        _release_standing_report(
            self.state,
            self.event,
            "the head that was writing this report is no longer this role's recorded head, so "
            "the report was closed unwritten by the tick that found it ownerless.",
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
    """This driver's supervised backend, built through the product's one name-to-backend mapping.

    The run root is the installation's own `heads/` directory — the same one the Ummanu
    dispatcher supervises its heads under — so a host keeps one place where a supervised head
    lives and one place an operator looks for it. The launch-identity reader is the product's
    single one, which is what lets a later tick ask whether the head an earlier tick started is
    still running.
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
#: The `runs.jsonl` action of a tick that retired the previous tick's finished, idle head.
SUPERVISED_IDLE_STOP = "supervised-idle-stop"
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
    """What a standing head's skill prompt is handed to the supervised backend with.

    The supervised backend owns the delivery — settle, type, submit on its own, confirm a turn —
    and reads only one thing off a caller's transport: the `before_send` hook it runs before the
    first byte (a retained worker's resume, a Codex provider-source binding). A standing head needs
    neither, so it carries none.
    """

    before_send: Callable[[], Any] | None = None


_STANDING_PROMPT_TRANSPORT = _StandingPromptTransport()


def _fail_closed(
    agent: str, state: AgentState, event: str, reports: _TickReports, refusal: NoSupervisedHead
) -> int:
    """The one place a tick with no supervised head to raise ends: nothing raised, reason recorded.

    One `runs.jsonl` entry — `action="no-supervised-head"`, `result="error"` and the specific
    cause — and the same reason on stderr, and a nonzero exit, which the systemd unit records as a
    failed run. Nothing else changes: no head is raised or stopped, `head_run.json` and
    `active_report.json` stay as they are and no report is closed. The tick created no report card
    of its own: the refusal is decided before one is built.
    """
    reports.hold_still = True
    state.log_run(event, action=NO_SUPERVISED_HEAD, result="error", error=refusal.reason)
    print(f"dispatch[{agent}]: no supervised head to raise — {refusal.reason}", file=sys.stderr)
    return REFUSED_EXIT


def _retire_idle_head(
    agent: str, runtime: Any, prior: dict | None, state: AgentState, event: str, reports: _TickReports
) -> None:
    """End the head an earlier tick raised once its turn is over and it has sat idle since.

    A tick is one head raised with its own skill, and the run is meant to end when that head exits.
    An adapter whose head does not exit after its turn (Codex's TUI keeps its composer open) left
    the head up with nothing to do, and the bring-up below refused every later tick over it
    (`supervised-busy-skip head_already_up`) until someone stopped it by hand.

    Only a head the supervisor positively reads as quiet is ended: alive, no turn open, no delivery
    in flight, and nothing new in its journal for `IDLE_HEAD_GRACE_SECONDS`. The stop is the
    runtime's own `stop_if_quiescent`, so a turn that starts between this look and the stop refuses
    it. Anything else — a working head, an unreadable record, a refused stop — changes nothing,
    and the bring-up decides as before. A steward report the retired head was writing is closed,
    because no head is writing it any more.
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
    state.log_run(event, action=SUPERVISED_IDLE_STOP, reference=run.run_id, idle_seconds=int(idle))
    print(f"dispatch[{agent}]: retired the idle head {run.run_id} after {int(idle)}s")
    _release_standing_report(
        state,
        event,
        "the head that was writing this report finished its turn and was retired idle, "
        "so the report was closed by the tick that raised its successor.",
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
    """Raise this role's head under a supervisor of this product's own, with its skill.

    Ephemeral by construction: a tick is one head raised with its own skill and the run ends when
    that head exits. Every outcome below is read from the backend's own typed receipt.

    The head outlives the tick. What the next tick needs to reach it — its run id, workspace and
    spec — is written to this agent's state directory as the receipt recorded it, and handing that
    record back to `start` is what makes a second bring-up over a working head a refusal
    (`HEAD_BUSY`, `head_already_up`, made before anything is spawned) rather than a second head.
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
    state.save_head_profile(cmd.profile)
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
    """`variant` selects a differently-scheduled mode of the same agent: a different prompt from
    `_resolve_launch`, and its own runs.jsonl event name so the two wake-up kinds stay
    distinguishable in the agent's own telemetry.

    `cleanup_only` is the gate's call on a precheck skip (`scripts/ummanu-agent-gate.sh`). It
    is a no-op for every agent: its subject was a pane a finished run left behind, and a supervised
    head's supervisor reaps its own process. It returns before `AgentState` is built or the run lock
    taken, so a precheck skip stays a zero-side-effect exit 0 and cannot contend the lock. It is
    still accepted because the systemd units pass it.

    The tick itself is `_tick`, and this is what stands around it: the run lock, and the one place
    a tick's obligations to a steward report card are discharged. `_TickReports` is entered here
    and every way `_tick` can end — every return, every raise — leaves through it.
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
    """One dispatch tick, under the run lock and inside `reports`.

    The order is the contract. The registry is read once and the head resolved out of that one
    reading; a resolution that names no usable `local-pty` head ends the tick in `_fail_closed`;
    a pane still recorded as this role's head refuses it; and only then is a command built, because
    building one creates the steward's report card. A tick that refuses therefore never holds one.
    """
    if _pipeline_paused():
        state.log_run(event, action="paused")
        print(f"dispatch[{agent}]: pipeline paused — no dispatch")
        return 0
    registry = _registry_snapshot()
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
