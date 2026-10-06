"""The one place a product run changes phase, and the order that makes a run safe to own.

Phases `claimed → raising → raised → settled`, with `unresolved` for an unconfirmed ending (meanings
in :mod:`ummanu.webproto.runs`). Invariants: a write-ahead record can address and stop a head before
any spawn; ownership is recovered from disk, never from the spawn's return value; a run settles only
on a confirmed ending, otherwise it stays `unresolved` (unsettled, so admission fences the card) and
every later close retries the stop; "is over" and "how it ended" are separate facts. Within
`ummanu.webproto` the backend's `start`/`stop` and `RunStore.settle` are called only from this module
(enforced by `tests/test_web_run_protocol.py`). See docs/PROTOCOLS.md, "The lifecycle of a run, and
the order it holds".
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ummanu.runtime.codex_preflight import CodexPreflightError, preflight_codex_launch
from ummanu.runtime.head.command import HeadCommandError, render_head_command
from ummanu.runtime.head.operations import NudgePointer
from ummanu.runtime.head.run import HeadRun, HeadRunError, StopInitiator
from ummanu.runtime.head.runtime import HEAD_BUSY, HEAD_OK
from ummanu.runtime.head.spec import HeadSpec
from ummanu.runtime.head.task_ref import TaskRef
from ummanu.webproto import run_state as run_state_reads
from ummanu.webproto.errors import (
    OwnerConflict,
    ReadError,
    RuntimeUnavailable,
    ValidationRefused,
)
from ummanu.webproto.runs import (
    CLAIMED,
    RAISED,
    RAISING,
    SETTLED,
    UNRESOLVED,
    ProductRun,
    RunStore,
    RunStoreError,
)

#: The initiator this product names on every stop of a head it owns.
INITIATOR = "ummanu.webproto"

#: Where a Claude head's first-run answers live; same file and env override as the pipeline's pane driver.
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))

#: The composer's "send" key, delivered as its own payload after the line. A raw-mode TUI reads a bare
#: line feed as a newline inside the message, and reads text plus carriage return in one burst as a
#: paste, so neither would submit.
SUBMIT_KEY = "\r"

#: Bring-up waits for an interactive head to stop printing (bound, quiet span, poll interval) before
#: putting the task in front of it.
SETTLE_SECONDS = 90.0
SETTLE_QUIET_SECONDS = 4.0
SETTLE_POLL_SECONDS = 0.5

#: Stop reasons recorded on the initiator, visible in the supervisor journal.
STOP_RESULT_IN = "this run published its result, so the product that owns its process ended it"
STOP_DEADLINE = "this run passed its deadline without publishing a result"
STOP_ENDED = "this run reached a terminal state, so the product ended the head it owned"
STOP_BRING_UP_FAILED = "this run's bring-up failed, so the product ended whatever it had raised"

#: Allowed phase transitions. Anything else is a caller defect, raised as a runtime failure.
ALLOWED: dict[str, frozenset[str]] = {
    CLAIMED: frozenset({RAISING, SETTLED}),
    RAISING: frozenset({RAISED, SETTLED, UNRESOLVED}),
    RAISED: frozenset({SETTLED, UNRESOLVED}),
    UNRESOLVED: frozenset({SETTLED, UNRESOLVED}),
    SETTLED: frozenset({SETTLED}),
}


class RunLifecycle:
    """One installation's product runs, moving between phases and nowhere else.

    Holds no policy: *when* to close is the operation layer's decision; *how* to close honestly is this.
    """

    def __init__(
        self,
        store: RunStore,
        runtime: Any,
        *,
        instance_env: dict[str, str] | None = None,
        settle_seconds: float = SETTLE_SECONDS,
        settle_quiet_seconds: float = SETTLE_QUIET_SECONDS,
        settle_poll_seconds: float = SETTLE_POLL_SECONDS,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.instance_env = dict(instance_env or {})
        self.settle_seconds = float(settle_seconds)
        self.settle_quiet_seconds = float(settle_quiet_seconds)
        self.settle_poll_seconds = float(settle_poll_seconds)

    # -- the one function ----------------------------------------------------------------------

    def advance(self, run: ProductRun, to: str, *, now: float, **evidence: Any) -> ProductRun:
        """Move one run to its next phase, durably, in the order the phases require.

        ``raising``: prepare the workspace and write the record that can address the head; only then
        may a spawn be attempted. ``raised``: spawn, point the head at its task, bind the handle; any
        failure is compensated by a close. ``settled``: end any head, confirm the ending from disk,
        settle once; an unconfirmed ending lands in ``unresolved`` instead.
        """
        if to not in ALLOWED.get(run.phase, frozenset()):
            raise RuntimeUnavailable(
                f"a product run does not move from {run.phase!r} to {to!r}; this run is "
                f"{run.run_id}"
            )
        if to == RAISING:
            return self._prepare(run, **evidence)
        if to == RAISED:
            return self._raise_the_head(run, now=now, **evidence)
        return self._close(run, now=now, **evidence)

    # -- claimed -> raising --------------------------------------------------------------------

    def _prepare(self, run: ProductRun, *, spec: HeadSpec, profile: dict[str, Any], document: Path) -> ProductRun:
        """The write-ahead. After this the record can address and stop a head; before it, nothing can.

        The `HeadRun` written here is the one `LocalPtyHeadRuntime.start` would build, and every
        address it derives comes from the run id and `pid_file`, so it addresses the head the spawn produces.
        """
        try:
            Path(run.run_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeUnavailable(f"this run's directory could not be created: {exc}") from None
        self._preflight(run, spec=spec, profile=profile)
        ahead = HeadRun(
            run_id=run.run_id,
            spec=spec,
            workspace=run.workspace,
            task_ref=self._task_ref(run, document),
            role=run.role,
            pid_file=run.pid_file,
            scope_generation=uuid.uuid4().hex if spec.memory_limit_mib is not None else "",
        )
        return self._save(run.with_head(ahead.to_json(), phase=RAISING))

    # -- raising -> raised ---------------------------------------------------------------------

    def _raise_the_head(
        self,
        run: ProductRun,
        *,
        now: float,
        spec: HeadSpec,
        profile: dict[str, Any],
        document: Path,
        note: str,
        env: dict[str, str],
    ) -> ProductRun:
        """Bring one head up for this run, and close the run if anything about that goes wrong.

        Every exit other than a bound, saved run goes through :meth:`_close` carrying `spawned`:
        whether the backend returned a successful receipt. A failed receipt left no process; after a
        successful one, the close may not settle until the head is ended and confirmed gone. The
        original failure is re-raised.
        """
        pointer = NudgePointer.at_document(str(document), note)
        # Adapters that take the prompt on the command line get it at launch; the others
        # (`HeadSpec.prompt_after_start`) are pointed at the document afterwards.
        prompt = None if spec.prompt_after_start else pointer.text
        spawned = False
        try:
            try:
                rendered = render_head_command(profile, prompt=prompt, workspace=run.workspace, role="")
            except HeadCommandError as exc:
                raise ValidationRefused(
                    f"this run's head command could not be rendered: {exc}"
                ) from None
            ahead = HeadRun.from_json(run.head_run)
            receipt = self.runtime.start(
                spec,
                run.workspace,
                self._task_ref(run, document),
                command=rendered.command,
                title=f"product-run:{run.run_id}",
                run_id=run.run_id,
                run=ahead,
                scope_generation=ahead.scope_generation,
                role=run.role,
                env=env,
                subject=f"product-run:{run.run_id}",
            )
            if receipt.status == HEAD_BUSY:
                raise OwnerConflict(f"a head is already up for run {run.run_id}: {receipt.reason}")
            if not receipt.ok or receipt.run is None:
                raise RuntimeUnavailable(
                    "the product runtime could not raise this run's head: "
                    f"{receipt.reason or receipt.status}"
                )
            # A process provably exists from here; no close may skip ending and confirming it.
            spawned = True
            live = receipt.run
            if spec.prompt_after_start:
                live = self._point_at_the_task(live, run, pointer)
            return self._save(
                run.with_head(
                    live.to_json(),
                    phase=RAISED,
                    head_pid=_pid_of(run),
                    supervisor_pid=_supervisor_pid_of(run),
                    head_raised=True,
                )
            )
        except BaseException as exc:
            with contextlib.suppress(ReadError, RunStoreError):
                # Through `advance` so that one function owns every close.
                self.advance(
                    run,
                    SETTLED,
                    now=now,
                    reason=STOP_BRING_UP_FAILED,
                    failure=f"this run's head could not be raised: {exc}",
                    spawned=spawned,
                )
            raise

    # -- anything -> settled, or honestly to unresolved ----------------------------------------

    def _close(
        self,
        run: ProductRun,
        *,
        now: float,
        reason: str = STOP_ENDED,
        failure: str = "",
        spawned: bool = False,
        observed: dict[str, Any] | None = None,
    ) -> ProductRun:
        """End whatever head this run may hold, confirm it, and settle exactly once.

        The head is ended and the ending confirmed from disk before anything terminal is written;
        otherwise the run goes `unresolved` and the card stays fenced. Reaching the settle means the
        run is over (no process was ever spawned, or its ending was confirmed). How it ended is read
        off the process via :func:`ummanu.webproto.run_state.from_evidence`, never off the run's own
        `unresolved` record, so a head that published its result before a later confirmed stop
        settles as a success.
        """
        if self._may_hold_a_process(run, spawned=spawned):
            confirmed, detail = self._end_the_head(run, reason)
            if not confirmed:
                return self._save(run.in_doubt(detail))
        state = observed if observed is not None else self._evidence(run, now=now)
        value, why = self._ending(state, failure=failure, reason=reason)
        exit_status, result = run_state_reads.terminal_evidence(run)
        try:
            settled, _first = self.store.settle(
                run.run_id, value, why, now=now, exit_status=exit_status, result=result
            )
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        return settled

    def _evidence(self, run: ProductRun, *, now: float) -> dict[str, Any]:
        """What the process says right now; a settled run is read as history."""
        if run.ended:
            return run_state_reads.observe(run, now=now)
        return run_state_reads.from_evidence(run, now=now)

    def _ending(self, state: dict[str, Any], *, failure: str, reason: str) -> tuple[str, str]:
        """How this run is recorded as having ended, and why. Whether it is over is decided by the caller.

        * a failed bring-up: `process_failed` with its named cause;
        * evidence naming an ending (`finished`, `process_failed`, `source_unavailable`): as it stands;
        * anything else (`running`, `unknown`): `source_unavailable` -- over, ending not established.
          Never invent `process_failed` for missing evidence.
        """
        if failure:
            return run_state_reads.PROCESS_FAILED, failure
        value = str(state.get("value") or "")
        if value in run_state_reads.ENDING_VALUES:
            return value, str(state["reason"])
        return run_state_reads.SOURCE_UNAVAILABLE, (
            f"{reason}; this run is over -- its head was ended and confirmed gone, or none was "
            "ever raised under it -- and the evidence establishes no ending of its own "
            f"({state.get('reason') or value or 'no evidence'}), so how it ended is not established"
        )

    def _may_hold_a_process(self, run: ProductRun, *, spawned: bool) -> bool:
        """Whether a head may exist under this run, decided conservatively and from disk.

        Any one witness suffices: `spawned` (a successful receipt in this call); the phase (`raised`
        or `unresolved`); or a trace in the run directory (scope owner, pid file, supervisor
        journal). A scope owner precedes launch work, so its presence alone requires the scope's
        empty proof.
        """
        if not run.addressable:
            return False
        if spawned or run.phase in (RAISED, UNRESOLVED):
            return True
        return self._left_a_trace(run)

    def _left_a_trace(self, run: ProductRun) -> bool:
        for candidate in (str(Path(run.run_dir) / "scope-owner.json"), run.pid_file, run.journal_path):
            if candidate and Path(candidate).exists():
                return True
        return False

    def _end_the_head(self, run: ProductRun, reason: str) -> tuple[bool, str]:
        """Stop this run's head from the record alone, and say whether the ending was confirmed.

        Confirmation is the backend's: the launch identity going dead, not a socket disappearing.
        """
        try:
            head = HeadRun.from_json(run.head_run)
        except (HeadRunError, ValueError, TypeError) as exc:
            return False, (
                "this run's head record could not be read back, so its process could not be ended "
                f"and may still be running: {exc}"
            )
        try:
            receipt = self.runtime.stop(head, StopInitiator(INITIATOR, reason))
        except Exception as exc:  # noqa: BLE001 - a backend failure is an unconfirmed cleanup
            return False, (
                f"the stop of this run's head failed ({type(exc).__name__}: {exc}), so its process "
                "may still be running"
            )
        if getattr(receipt, "status", "") == HEAD_OK:
            return True, ""
        return False, (
            "this run's head was asked to stop and its ending could not be confirmed "
            f"({getattr(receipt, 'reason', '') or getattr(receipt, 'status', 'no answer')}), so its "
            "process may still be running"
        )

    # -- the pieces a bring-up is made of -------------------------------------------------------

    def _task_ref(self, run: ProductRun, document: Path) -> TaskRef:
        return TaskRef.card(run.ref, document=str(document))

    def _preflight(self, run: ProductRun, *, spec: HeadSpec, profile: dict[str, Any]) -> None:
        """Prepare the workspace for the head, before any spawn.

        Codex workspace trust is a hard precondition (the TUI never reaches readiness without it);
        Claude trust and theme are best-effort. A failure here closes a run that holds no process.
        """
        if spec.adapter == "codex":
            try:
                preflight_codex_launch(
                    profile,
                    run.workspace,
                    HeadRun(
                        run_id=run.run_id,
                        spec=spec,
                        workspace=run.workspace,
                        task_ref=TaskRef.card(run.ref),
                        role=run.role,
                    ),
                )
            except (CodexPreflightError, HeadRunError) as exc:
                raise RuntimeUnavailable(
                    f"this run's Codex head could not be prepared for {run.workspace}: {exc}"
                ) from None
            return
        if spec.adapter == "claude":
            from ummanu.runtime import claude_env

            try:
                claude_env.ensure_trust(CLAUDE_JSON, run.workspace)
                claude_env.ensure_theme(CLAUDE_JSON)
            except claude_env.ClaudeConfigError:
                # Best-effort, as on the pipeline's path: a head stuck on the trust dialog shows
                # up as an undelivered receipt.
                pass

    def _point_at_the_task(self, live: HeadRun, run: ProductRun, pointer: NudgePointer) -> HeadRun:
        """Hand an interactive head its task, once it is actually ready to read one.

        A line delivered while the TUI is still starting lands in the composer unsubmitted, and the
        delivery still reads as successful. So: wait until the head stops printing (supervisor
        `output_bytes`); deliver the line; wait until the backend reports it idle (otherwise the
        substrate's own turn refuses the next payload `HEAD_BUSY`); deliver `SUBMIT_KEY` alone.
        `settle_seconds` bounds both waits; on timeout the deliveries proceed and their receipts decide.
        """
        subject = f"product-run:{run.run_id}"
        self._wait_until_quiet(live)
        delivered = self.runtime.deliver(live, pointer, subject=subject)
        if not getattr(delivered, "arrived", delivered.ok):
            raise RuntimeUnavailable(
                "this run's head came up and its task could not be put in front of it: "
                f"{delivered.reason or delivered.status}"
            )
        live = delivered.run or live
        self._wait_until_idle(live)
        sent = self.runtime.deliver(live, NudgePointer.line(SUBMIT_KEY), subject=f"{subject}:submit")
        if not getattr(sent, "arrived", sent.ok):
            raise RuntimeUnavailable(
                "this run's head was given its task and could not be told to send it: "
                f"{sent.reason or sent.status}"
            )
        return sent.run or live

    def _wait_until_quiet(self, live: HeadRun) -> None:
        """Wait until the head has stopped printing, or until this bring-up's bound runs out."""
        deadline = time.monotonic() + self.settle_seconds
        printed = -1
        steady_since = time.monotonic()
        while time.monotonic() < deadline:
            receipt = self.runtime.observe(live)
            evidence = receipt.evidence if isinstance(receipt.evidence, dict) else {}
            current = evidence.get("output_bytes")
            current = current if isinstance(current, int) else -1
            if current != printed:
                printed, steady_since = current, time.monotonic()
            elif printed > 0 and time.monotonic() - steady_since >= self.settle_quiet_seconds:
                return
            time.sleep(self.settle_poll_seconds)

    def _wait_until_idle(self, live: HeadRun) -> None:
        """Wait until the backend will take another payload for this head.

        `busy` covers the substrate's turn and this runtime's turn lease. `None` (unknown) is not
        waited on; the next delivery reports its own refusal.
        """
        deadline = time.monotonic() + self.settle_seconds
        while time.monotonic() < deadline:
            if not self.runtime.observe(live).busy:
                return
            time.sleep(self.settle_poll_seconds)

    # -- the store, with this layer's own failure vocabulary ------------------------------------

    def _save(self, run: ProductRun) -> ProductRun:
        try:
            return self.store.save(run)
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None


def _pid_of(run: ProductRun) -> int:
    return _identity_field(run, "pid")


def _supervisor_pid_of(run: ProductRun) -> int:
    """The supervisor's pid, from the file it writes into its own run directory."""
    try:
        return int(Path(run.run_dir, "supervisor.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _identity_field(run: ProductRun, name: str) -> int:
    """One integer out of the head's launch-identity record, or zero when it has not landed.

    Diagnostic only; liveness is decided by the classified heartbeat in `run_state`.
    """
    try:
        record = json.loads(Path(run.pid_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    value = record.get(name) if isinstance(record, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
