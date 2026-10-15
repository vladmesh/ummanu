"""Own PO turns through the local socket and durable per-session FIFO.

Claim before removing an input; recover before pumping. Sessions run in parallel with one
turn each. Restart drains turns and holds queued inputs for the next process. Production
rights annotate operation inputs; sprint-session resolution is serialized and replayable.
See docs/OPERATIONS.md and the method contracts below.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import fcntl
import inspect
import json
import os
import socketserver
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ummanu.board import owner_events
from ummanu.board.production_rights import (
    CARD_INPUT,
    NO_PRODUCTION,
    OPERATION_KIND,
    OWNER_ANSWER_INPUT,
    facts_problem,
    is_allowed,
    rights_line,
    rights_note,
)
from ummanu.po.attachments import AttachmentRefused, attachment_path
from ummanu.po.client import (
    LOCK_NAME,
    MAX_MESSAGE_BYTES,
    restart_marker_path,
    service_dir,
    socket_path,
)
from ummanu.po.context_budget import (
    CONTEXT_METRIC,
    context_budget_bytes,
    conversation_bytes,
    rollover_request_id,
)
from ummanu.po.models import (
    EffortRefused,
    efforts_from_instance,
    first_effort,
    models_from_instance,
    require_explicit_effort,
    successor_choice,
)
from ummanu.po.queue import (
    DISPATCHER_SOURCE,
    SERVICE_SOURCE,
    SOURCES,
    PoQueue,
    QueuedInput,
    QueueError,
)
from ummanu.po.runner import PoRunner, RunnerError
from ummanu.po.sprints import (
    BoardSprintSessions,
    SprintRecord,
    SprintSessions,
    reseed_comment,
    seed_message,
)
from ummanu.po.store import (
    CLIS,
    SEND,
    SESSION_CLOSED,
    SESSION_CREATE,
    SESSION_OPEN,
    SPRINT_SESSION,
    PoRequest,
    PoStoreError,
    RequestConflict,
    Session,
    SessionClosed,
    SessionNotFound,
    TitleRefused,
    TurnInProgress,
    send_fingerprint,
    session_fingerprint,
    sprint_session_fingerprint,
)
from ummanu.runtime.cli_composition import add_command_parser
from ummanu.runtime.paths import add_instance_argument, instance_file
from ummanu.runtime.provider_errors import KIND_AUTH, KIND_PLAN, KIND_QUOTA, ProviderError

# How often the service looks at its queue, its restart marker and a recovery that did not run yet,
# besides being woken by a submit or a settled turn.
TICK_SECONDS = 1.0
# Provider refusals of the account itself: a session moved for one of these needs the owner.
ACCOUNT_FAILURE_KINDS = frozenset({KIND_AUTH, KIND_PLAN, KIND_QUOTA})
# The longest wait between two recovery passes while a `running` row has no process here.
RECOVERY_MAX_DELAY_SECONDS = 30.0
# The longest `sun_path` Linux takes, with its terminating NUL.
MAX_SOCKET_PATH_BYTES = 107
STOP_ACTOR = "owner"


class ServiceStartError(RuntimeError):
    """The PO service cannot serve here: another one holds the lock, or the socket cannot be bound."""


class Refused(Exception):
    """A request refused before it reached the store; `code` is the endpoint's."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PoService:
    """Turns, the queue and the endpoint's operations of one installation. No socket here: see `listening`."""

    def __init__(
        self,
        runner: PoRunner,
        queue: PoQueue | None = None,
        *,
        data_dir: Path | str | None = None,
        instance: Path | str | None = None,
        sprints: SprintSessions | None = None,
        models: dict[str, tuple[str, ...]] | None = None,
        efforts: dict[str, tuple[str, ...]] | None = None,
        owner_events: Any = None,
    ) -> None:
        self.runner = runner
        self.store = runner.store
        # Where a failed turn's owner event goes: the given sink, else the PO store's own board store.
        self.owner_events = owner_events if owner_events is not None else self.store
        self.data_dir = Path(data_dir) if data_dir is not None else runner.data_dir
        # What `sprint_session` needs: the installation's sprints, and the models a session opened
        # without a previous one takes its default from (read from instance.yaml when not given).
        # The efforts every new session's effort is checked against, and a sprint's session takes
        # its first from: given, else instance.yaml's, else the product's.
        self.instance = Path(instance) if instance is not None else None
        self.sprints = sprints
        self.models = models
        self.efforts = efforts
        self.queue = queue or PoQueue(self.data_dir)
        self.marker = restart_marker_path(self.data_dir)
        runner.on_settled = self._settled
        runner.on_failed = self._turn_failed
        runner.on_fallback = self._turn_moved
        if runner.fallback_choice is None:
            runner.fallback_choice = self.fallback_choice
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._exit = threading.Event()
        # True only once every `running` row is live under this service or settled.
        self._recovered = False
        self._recovery_delay = 0.0
        self._next_recovery = 0.0

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> list[str]:
        """Settle what a previous run left, drop a restart this process fulfils, take the queue; journal lines."""
        lines = []
        try:
            if self.marker.exists():
                self.marker.unlink()
                lines.append("ummanu po: a pending restart request is fulfilled by this start")
        except OSError as exc:
            lines.append(f"ummanu po: could not clear the restart marker {self.marker}: {exc}")
        try:
            self.queue.ensure()
        except QueueError as exc:
            lines.append(f"ummanu po: {exc}")
        lines.extend(self._recover())
        self.pump()
        return lines

    def _recover(self, *, tick: float = TICK_SECONDS) -> list[str]:
        """Retry incomplete recovery with bounded exponential delay.

        Unowned running rows block only their sessions; other sessions keep pumping.
        """
        lines: list[str] = []
        with self._lock:
            try:
                recovered = self.runner.recover(rerun=True)
                orphaned = self.runner.orphaned_turns()
            except Exception as exc:  # noqa: BLE001 - retried with backoff until the store answers
                orphaned = None
                lines.append(f"ummanu po: turn recovery did not complete: {type(exc).__name__}: {exc}")
            else:
                lines.extend(
                    f"ummanu po: turn {turn.session_id}/{turn.seq} {turn.state}: {turn.reason}"
                    for turn in recovered
                )
            self._recovered = orphaned == []
            if self._recovered:
                self._recovery_delay = 0.0
            else:
                self._recovery_delay = min(max(self._recovery_delay * 2, tick), RECOVERY_MAX_DELAY_SECONDS)
                self._next_recovery = time.monotonic() + self._recovery_delay
                for turn in orphaned or []:
                    lines.append(
                        f"ummanu po: turn {turn.session_id}/{turn.seq} still running without a process; "
                        f"recovery retries in {self._recovery_delay:g}s"
                    )
        return lines

    def run(self, *, tick: float = TICK_SECONDS, say: Callable[[str], None] | None = None) -> int:
        """Serve until a restart is due at idle (exit 0, `Restart=always` starts the new code)."""
        say = say or _say
        while not self._exit.is_set():
            self._wake.wait(tick)
            self._wake.clear()
            if not self._recovered and time.monotonic() >= self._next_recovery:
                for line in self._recover(tick=tick):
                    say(line)
            self.pump()
            if self.restart_due():
                say("ummanu po: exiting for a pending restart; no turn is running")
                self._exit.set()
        return 0

    def stop(self) -> None:
        self._exit.set()
        self._wake.set()

    @property
    def exiting(self) -> bool:
        return self._exit.is_set()

    def _settled(self, _session_id: str, _seq: int) -> None:
        # A waiter finishing can leave an orphan when scope cleanup did not complete.
        self._schedule_recovery()
        self._wake.set()

    def _schedule_recovery(self) -> None:
        with self._lock:
            try:
                if not self.runner.orphaned_turns():
                    return
            except Exception:  # noqa: BLE001, S110 - an unreadable store also needs bounded recovery
                pass
            self._recovered = False
            if self._next_recovery <= time.monotonic():
                self._next_recovery = time.monotonic() + TICK_SECONDS
        self._wake.set()

    def fallback_choice(self, session: Session) -> tuple[str, str, str] | None:
        """Return the other CLI's first model, preserving effort if offered, else its first effort.

        Return None when that CLI offers no model.
        """
        try:
            models = self._model_list()
            efforts = self._effort_list()
        except Refused:
            return None
        for cli in CLIS:
            if cli == session.cli:
                continue
            listed = [str(model) for model in models.get(cli) or () if str(model).strip()]
            if not listed:
                continue
            offered = [str(value) for value in efforts.get(cli) or ()]
            effort = session.effort if session.effort in offered else (first_effort(cli, efforts) or "")
            if not effort:
                continue
            return cli, listed[0], effort
        return None

    def _turn_failed(self, session_id: str, seq: int, reason: str) -> None:
        """A turn settled `failed`: one `po_turn_failed` notice (a stop by the owner is `interrupted`).

        Its subject is the card when a dispatcher input started the turn (the facts the runner kept
        beside it), else the session.
        """
        card = self.runner.turn_card(session_id, seq) or {}
        card_ref = str(card.get("card_ref") or "")
        on = f" on {card_ref}" + (" (the owner's answer)" if card.get("input") == OWNER_ANSWER_INPUT else "")
        owner_events.record(
            owner_events.PO_TURN_FAILED,
            card_ref or owner_events.po_session_subject(session_id),
            f"PO turn {session_id}/{seq} failed{on if card_ref else ''}: {reason}",
            f"{owner_events.PO_TURN_FAILED}:{session_id}:{seq}",
            to=self.owner_events,
        )

    def _turn_moved(self, picked: Session, running: Session, seq: int, failure: ProviderError) -> None:
        """A turn moved to the other CLI family: one event per session and turn on `po-session:<id>`.

        An auth, plan or quota refusal needs the owner (`po_session_moved_account`); any other is a notice.
        """
        kind = (
            owner_events.PO_SESSION_MOVED_ACCOUNT
            if failure.kind in ACCOUNT_FAILURE_KINDS
            else owner_events.PO_SESSION_MOVED
        )
        owner_events.record(
            kind,
            owner_events.po_session_subject(picked.session_id),
            f"PO session {picked.session_id} turn {seq}: picked {picked.cli}/{picked.model}, now running "
            f"{running.cli}/{running.model}; {picked.cli} refused it ({failure.kind}): {failure.summary}",
            f"{kind}:{picked.session_id}:{seq}",
            to=self.owner_events,
        )

    # --- the queue --------------------------------------------------------------------------

    def pump(self) -> None:
        """Hand the oldest input of every idle session to the runner; hold everything while a restart is pending."""
        with self._lock:
            # Not gated on `_recovered`: a row recovery has not settled is `running`, so its session
            # is busy below and takes nothing until it is.
            if self._exit.is_set() or self.marker.exists():
                return
            try:
                heads = self.queue.heads()
                if not heads:
                    return
                busy = {turn.session_id for turn in self.store.running_turns()}
            except (QueueError, PoStoreError) as exc:
                _say(f"ummanu po: the queue waits: {exc}")
                return
            for item in heads:
                if item.session_id not in busy:
                    self._hand_over(item)

    def _hand_over(self, item: QueuedInput) -> None:
        """One input becomes its turn, then leaves the queue; the claim's request id makes a repeat harmless."""
        try:
            self.runner.send_request(item.session_id, item.text, item.request_id, card=item.card,
                                     note=item.note, metadata=item.metadata)
        except TurnInProgress:
            return
        except (SessionNotFound, SessionClosed, RequestConflict) as exc:
            self._refuse(item, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - the claim may have committed before the launch failed
            self._schedule_recovery()
            try:
                known = self.store.request(item.request_id)
            except PoStoreError:
                _say(f"ummanu po: {item.name} waits: {type(exc).__name__}: {exc}")
                return
            if known is None:
                if isinstance(exc, RunnerError):
                    self._refuse(item, str(exc))
                else:
                    _say(f"ummanu po: {item.name} waits: {type(exc).__name__}: {exc}")
                return
            # Claimed: the turn exists (settled `failed` by the runner if its CLI never started).
        try:
            self.queue.remove(item)
        except QueueError as exc:
            _say(f"ummanu po: {exc}; the next hand-over answers the same turn")

    def _refuse(self, item: QueuedInput, reason: str) -> None:
        _say(f"ummanu po: {item.name} for session {item.session_id} set aside: {reason}")
        try:
            self.queue.refuse(item, reason)
        except QueueError as exc:
            _say(f"ummanu po: {exc}")

    # --- the endpoint's operations ----------------------------------------------------------

    def create_session(self, *, cli: str, model: str, effort: str = "", request_id: str) -> dict[str, Any]:
        """Claim one session per request id with explicit, offered effort.

        Acceptance starts at claim_session or its replay; later failure still answers the session.
        """
        request_id = _required(request_id, "request_id")
        model = str(model or "").strip()
        if cli not in CLIS:
            raise Refused("validation", f"a PO session runs {' or '.join(CLIS)}, not {cli!r}")
        if not model:
            raise Refused("validation", "a PO session needs a model")
        efforts = self._effort_list()
        try:
            effort = require_explicit_effort(cli, effort, efforts)
        except EffortRefused as exc:
            raise Refused("validation", str(exc)) from None
        fingerprint = session_fingerprint(cli, model, effort)
        with self._lock:
            known = self._reserve(request_id, SESSION_CREATE, fingerprint)
            if isinstance(known, PoRequest):
                self._accepted({"session_id": known.session_id, "effort": effort, "repeated": True})
                session = self.store.session(known.session_id)
                return {"session_id": session.session_id, "effort": session.effort, "repeated": True}
            self._accepting()
            session, created = self.runner.create_session_request(
                cli, model, request_id, effort, efforts=efforts
            )
            answer = {"session_id": session.session_id, "effort": session.effort, "repeated": not created}
            self._accepted(answer)
        return answer

    def submit(
        self,
        *,
        session_id: str,
        text: str,
        request_id: str,
        source: str = "web",
        card: dict[str, Any] | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        """Queue durably before answering; replay identical id/session/text/card facts, reject conflicts.

        Unknown/closed sessions write nothing. Operation inputs carry the production-rights note;
        idle sessions hand over immediately, and the answer records whether they did. `attachments`
        names images the session holds (`ummanu.po.attachments`); the text carries their paths.
        """
        request_id = _required(request_id, "request_id")
        session_id = _required(session_id, "session_id")
        if not str(text or "").strip():
            raise Refused("validation", "an empty message starts no turn")
        if source not in SOURCES:
            raise Refused("validation", f"a PO input comes from {' or '.join(SOURCES)}, not {source!r}")
        if card is not None and source != DISPATCHER_SOURCE:
            raise Refused("validation", f"only a {DISPATCHER_SOURCE} input carries card facts")
        if card is not None and not isinstance(card, dict):
            raise Refused("validation", "card facts are a JSON object")
        if attachments is not None:
            self._check_attachments(session_id, text, attachments, source)
        with self._lock:
            known = self._reserve(
                request_id,
                SEND,
                send_fingerprint(session_id, text, card),
                session_id=session_id,
                text=text,
                card=card,
            )
            if known is not None:
                self._accepted(_known_answer(session_id, known))
                return {**self._sent(session_id, request_id), "repeated": True}
            session = self.store.session(session_id)
            if session.state == SESSION_CLOSED:
                raise SessionClosed(f"PO session {session_id} is closed; open a new session to continue")
            note = self._production_rule(card, request_id) if source == DISPATCHER_SOURCE else None
            metadata = None
            if source == DISPATCHER_SOURCE:
                metadata, comments = self._input_context(session_id, card)
                note = "\n\n".join(part for part in (comments, note) if part) or None
            elif attachments:
                metadata = {"attachments": list(attachments)}
            self._accepting()
            self.queue.put(
                session_id=session_id, text=text, request_id=request_id, source=source, card=card,
                note=note, metadata=metadata
            )
            self._accepted(
                {"session_id": session_id, "queued": True, "seq": None, "state": None, "repeated": False}
            )
            # Best effort from here: the answer above stands if the hand-over or the lookup fails.
            self.pump()
            return {**self._sent(session_id, request_id), "repeated": False}

    def _check_attachments(self, session_id: str, text: str, attachments: Any, source: str) -> None:
        """Each name is an image this session holds, and the text names its path for the head."""
        if source == DISPATCHER_SOURCE:
            raise Refused("validation", f"a {DISPATCHER_SOURCE} input carries no attachments")
        if not isinstance(attachments, list) or not all(isinstance(name, str) for name in attachments):
            raise Refused("validation", "attachments are a list of attachment names")
        for name in attachments:
            try:
                path = attachment_path(self.data_dir, session_id, name)
            except AttachmentRefused as exc:
                raise Refused("validation", str(exc)) from None
            if str(path) not in text:
                raise Refused("validation", f"the message text does not name the attachment {path}")

    def _input_context(self, session_id: str, card: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Freeze the selected delta under the submit lock before acceptance.

        Claim moves metadata atomically into the feed. Refusal creates no boundary; legacy frozen
        facts neither consume nor reformat comments.
        """
        from ummanu.po.input_context import comment_position, comments_note

        metadata = {"source": DISPATCHER_SOURCE,
                    "summary": card.get("display_summary") or f"{card['card_ref']} ({card['kind']})"}
        if "deliver_sprint_comments" in card and type(card["deliver_sprint_comments"]) is not bool:
            raise Refused("validation", "deliver_sprint_comments is an explicit boolean")
        if card.get("deliver_sprint_comments") is not True:
            return metadata, ""
        sprint_ref = card["sprint_ref"]
        if not sprint_ref:
            raise Refused("validation", "deliver_sprint_comments requires a sprint_ref")
        try:
            sprint = self._sprint_sessions().sprint(sprint_ref)
        except Exception as exc:  # noqa: BLE001 - unreadable comments accept no input
            raise Refused(
                "unavailable", f"sprint {sprint_ref} cannot be read for its comments ({type(exc).__name__}: {exc})"
            ) from None
        if sprint is None:
            raise Refused("unavailable", f"sprint {sprint_ref} cannot be read for its comments")
        comments = list(sprint.comments)
        delivered = [entry.metadata for entry in self.store.feed(session_id)]
        delivered += [item.metadata for item in self.queue.pending(session_id)]
        start = max((comment_position(value, sprint_ref) for value in delivered), default=0)
        if len(comments) < start:
            raise Refused("unavailable", f"sprint {sprint_ref} comment history is shorter than its accepted position")
        metadata.update(sprint_ref=sprint_ref, comment_position=len(comments))
        if card.get("input") == OWNER_ANSWER_INPUT and start == len(comments):
            return metadata, ""
        return metadata, comments_note(sprint_ref, comments, start)

    def _production_rule(self, card: dict[str, Any] | None, request_id: str) -> str | None:
        """Annotate operation inputs with sprint allowance or the owner's standing decision rule.

        Decision cards and owner answers get no note. Outside a sprint, the PO decides under the
        standing rule. Missing/malformed facts or an unreadable sprint refuse as unavailable.
        """
        problem = facts_problem(card)
        if problem:
            _say(f"ummanu po: dispatcher input {request_id} not queued: {problem}")
            raise Refused(
                "unavailable", f"{problem}; a dispatcher input is executed only with its card's facts"
            )
        assert card is not None  # facts_problem refuses a missing one
        if card["kind"] != OPERATION_KIND or card["input"] != CARD_INPUT:
            return None
        production = str(card["touches_production"])
        sprint_ref = str(card["sprint_ref"])
        allow_id = f"{request_id}:allow-production"
        if production == NO_PRODUCTION:
            return rights_note(production, sprint_ref, None, request_id=allow_id)
        if not sprint_ref:
            # Cut outside every sprint by a PO session (secretary-1792): no allowance to read.
            _say(
                f"ummanu po: {card['card_ref']} queued for the PO to decide: touches production "
                f"{production}; no sprint"
            )
            return rights_note(production, "", None, request_id=allow_id)
        try:
            sprint = self._sprint_sessions().sprint(sprint_ref)
        except Exception as exc:  # noqa: BLE001 - a sprint that cannot be read runs nothing
            sprint, unread = None, f"{type(exc).__name__}: {exc}"
        else:
            unread = "" if sprint is not None else "there is no such sprint"
        if sprint is None:
            _say(
                f"ummanu po: {card['card_ref']} not queued: sprint {sprint_ref} cannot be read "
                f"for its allowed productions ({unread})"
            )
            raise Refused(
                "unavailable",
                f"sprint {sprint_ref} cannot be read for its allowed productions ({unread}); "
                f"operation card {card['card_ref']} is not executed",
            )
        if not is_allowed(production, sprint.allowed_productions):
            _say(
                f"ummanu po: {card['card_ref']} queued for the PO to decide: "
                f"{rights_line(production, sprint.ref, sprint.allowed_productions)}"
            )
        return rights_note(production, sprint.ref, sprint.allowed_productions, request_id=allow_id, owner_decisions=sprint.owner_decisions)

    def _reserve(
        self,
        request_id: str,
        operation: str,
        fingerprint: str,
        *,
        session_id: str | None = None,
        text: str | None = None,
        card: dict[str, Any] | None = None,
    ) -> PoRequest | QueuedInput | None:
        """Check installation-wide request-id ownership under the service lock.

        po_requests, pending inputs and refused inputs all reserve ids. Identical operation/inputs
        replay; different inputs raise RequestConflict before another write can claim the id.
        """
        known = self.store.request(request_id)
        if known is not None:
            if (known.operation, known.fingerprint) != (operation, fingerprint):
                raise RequestConflict(
                    f"request id {request_id!r} already belongs to another {known.operation} request; "
                    "a request id is repeated only with the same operation and inputs"
                )
            return known
        queued = self.queue.find(request_id)
        if queued is not None:
            if operation != SEND or (queued.session_id, queued.text, queued.card) != (session_id, text, card):
                raise RequestConflict(
                    f"request id {request_id!r} already belongs to a message queued for PO session "
                    f"{queued.session_id}; a request id is repeated only with the same operation and inputs"
                )
            return queued
        refused = self.queue.find_refused(request_id)
        if refused is not None:
            raise RequestConflict(
                f"request id {request_id!r} belongs to a message the PO service set aside "
                f"({refused.get('reason') or 'no reason recorded'}); send it again with a new form"
            )
        return None

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]:
        """Return {session_id, created} under the service lock; reuse open sessions within budget or busy.

        Replace idle over-budget sessions under the sprint/predecessor id without closing history.
        Missing/closed sessions use request_id and recorded CLI/model/effort, with current defaults
        and explicit offered effort as needed. Claim accepts; queue seed, comment, then record the
        session using derived ids. Partial failure is outcome_unknown; replay completes those steps.
        """
        request_id = _required(request_id, "request_id")
        sprint_ref = _required(sprint_ref, "sprint_ref")
        fingerprint = sprint_session_fingerprint(sprint_ref)
        with self._lock:
            known = self._reserve(request_id, SPRINT_SESSION, fingerprint)
            threshold = self._context_budget()
            sprints = self._sprint_sessions()
            sprint = sprints.sprint(sprint_ref)
            if sprint is None:
                raise Refused("validation", f"there is no sprint {sprint_ref}")
            if isinstance(known, PoRequest):
                self._accepting()
                self._reseed(sprints, sprint, known.session_id, request_id)
                answer = {"session_id": known.session_id, "created": True, "repeated": True}
                self._accepted(answer)
                return answer
            if sprint.status != "open":
                raise Refused(
                    "validation", f"sprint {sprint_ref} is {sprint.status}; its PO session is not resolved"
                )
            previous = self._session_or_none(sprint.po_session)
            rollover = None
            resolve_id = request_id
            if previous is not None and previous.state == SESSION_OPEN:
                feed = self.store.feed(previous.session_id)
                measured = conversation_bytes(feed)
                if measured <= threshold or self._session_busy(previous.session_id):
                    return {"session_id": previous.session_id, "created": False, "repeated": False}
                resolve_id = rollover_request_id(sprint.ref, previous.session_id)
                # Read and bound the sources before the first write. A partially written
                # canonical resolve is completed below using its frozen first seed.
                if self._reserve(resolve_id, SPRINT_SESSION, fingerprint) is None:
                    rollover = self._rollover_seed(sprints, sprint, threshold, feed)
            else:
                # Missing/closed replacements use the same bounded durable sources,
                # while keeping their real reason and released request identity.
                if sprint.po_session:
                    canonical_id = rollover_request_id(sprint.ref, sprint.po_session)
                    canonical = self._reserve(canonical_id, SPRINT_SESSION, fingerprint)
                    if isinstance(canonical, PoRequest):
                        resolve_id = canonical_id
                if resolve_id == request_id:
                    self._durable_seed(sprints, sprint, threshold,
                                       self.store.feed(previous.session_id) if previous else [],
                                       "is closed" if previous else "no longer exists")
            efforts = self._effort_list()
            choice = successor_choice(
                (previous.cli, previous.model, previous.effort) if previous is not None else None,
                self._model_list(),
                efforts,
            )
            if choice is None:
                raise Refused("validation", "this installation offers no model for a PO session")
            cli, model, effort = choice
            try:
                effort = require_explicit_effort(cli, effort, efforts)
            except EffortRefused as exc:
                raise Refused("validation", str(exc)) from None
            self._accepting()
            session, _created = self.runner.create_session_request(
                cli,
                model,
                resolve_id,
                effort,
                efforts=efforts,
                operation=SPRINT_SESSION,
                fingerprint=fingerprint,
                title=sprint.ref,
            )
            if resolve_id != request_id:
                self.store.bind_sprint_session_request(request_id, sprint.ref, session.session_id)
            self._reseed(sprints, sprint, session.session_id, resolve_id, rollover=rollover)
            answer = {"session_id": session.session_id, "created": True, "repeated": False}
            self._accepted(answer)
            # Best effort from here: the seed is queued and the answer above stands.
            self.pump()
            return answer

    def _reseed(
        self, sprints: SprintSessions, sprint: SprintRecord, session_id: str, request_id: str,
        *, rollover: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        """Finish seeding and announcement idempotently, recording the session last.

        The previous session identifies unfinished work; leave a different open successor untouched.
        """
        if sprint.po_session == session_id:
            return
        current = self._session_or_none(sprint.po_session)
        canonical = None
        if sprint.po_session:
            canonical_id = rollover_request_id(sprint.ref, sprint.po_session)
            canonical = self.store.request(canonical_id)
            if canonical is not None and (canonical.operation, canonical.fingerprint) != (
                SPRINT_SESSION, sprint_session_fingerprint(sprint.ref)
            ):
                canonical = None
            if canonical is not None and canonical.session_id == session_id:
                # The predecessor may have been closed since the first seed or
                # comment committed. Its canonical write IDs still own replay.
                request_id = canonical_id
        if current is not None and current.state == SESSION_OPEN:
            # Only the native request for this exact predecessor may intentionally
            # replace an open session. Released requests keep their original replay.
            if canonical is None or canonical.session_id != session_id:
                return
            if self._session_busy(current.session_id):
                raise Refused("unavailable", "context rollover waits for the predecessor to become idle")
        why = "is closed" if current is not None else "no longer exists"
        seed_id = f"{request_id}:seed"
        seeded = self.store.request(seed_id) or self.queue.find(seed_id) or self.queue.find_refused(seed_id)
        metadata = None
        if isinstance(seeded, PoRequest):
            metadata = next(entry.metadata for entry in self.store.feed(session_id)
                            if entry.turn_seq == seeded.seq and entry.role == "owner")
        elif isinstance(seeded, QueuedInput):
            metadata = seeded.metadata
        if seeded is None:
            if current is not None and current.state == SESSION_OPEN:
                rollover = rollover or self._rollover_seed(
                    sprints, sprint, self._context_budget(), self.store.feed(current.session_id)
                )
            metadata = {"source": SERVICE_SOURCE, "summary": f"Sprint session context for {sprint.ref}"}
            if rollover is not None:
                text, transition = rollover
                transition = {**transition, "successor": session_id}
                transition["comment"] = (
                    f"PO context rollover {sprint.po_session} -> {session_id}: "
                    f"{transition['measured_bytes']} > {transition['threshold_bytes']} {CONTEXT_METRIC}. "
                    "Predecessor retained as readable history. Seed sources: "
                    + json.dumps(transition["seed_sources"], ensure_ascii=False, sort_keys=True)
                )
                metadata.update(summary=f"Context rollover for {sprint.ref}", transition=transition)
            else:
                text, sources = self._durable_seed(
                    sprints, sprint, self._context_budget(),
                    self.store.feed(current.session_id) if current else [], why,
                )
                metadata["seed_sources"] = sources
            self.queue.put(
                session_id=session_id,
                text=text,
                request_id=seed_id,
                source=SERVICE_SOURCE,
                metadata=metadata,
            )
        transition = (metadata or {}).get("transition")
        documents = [] if transition else sprints.why_documents(sprint.ref)
        sprints.comment(
            sprint.ref,
            transition["comment"] if transition else
            reseed_comment(sprint.po_session, session_id, documents, reason=why),
            request_id=f"{request_id}:comment",
        )
        sprints.record_po_session(sprint.ref, session_id, request_id=f"{request_id}:record")

    def _context_budget(self) -> int:
        try:
            return context_budget_bytes(self._instance_config())
        except ValueError as exc:
            raise Refused("validation", str(exc)) from None

    def _session_busy(self, session_id: str) -> bool:
        return (any(turn.session_id == session_id for turn in self.store.running_turns())
                or bool(self.queue.pending(session_id)))

    def _rollover_seed(self, sprints: SprintSessions, sprint: SprintRecord,
                       threshold: int, feed: list[Any]) -> tuple[str, dict[str, Any]]:
        measured = conversation_bytes(feed)
        why = f"crossed its context budget ({measured} > {threshold} {CONTEXT_METRIC})"
        text, sources = self._durable_seed(sprints, sprint, threshold, feed, why)
        return text, {"reason": "context_budget", "predecessor": sprint.po_session,
                      "measured_bytes": measured, "threshold_bytes": threshold,
                      "metric": CONTEXT_METRIC, "seed_sources": sources}

    def _durable_seed(self, sprints: SprintSessions, sprint: SprintRecord, threshold: int,
                      feed: list[Any], why: str) -> tuple[str, dict[str, Any]]:
        try:
            text, sources = seed_message(
                sprint, why, sprints.why_documents(sprint.ref), threshold=threshold,
                measured=conversation_bytes(feed), latest_answer=next(
                    (entry for entry in reversed(feed) if entry.role == "agent"), None),
                notes=self.runner.workspace / "NOTES.md",
            )
        except ValueError as exc:
            raise Refused("validation", str(exc)) from None
        return text, sources

    def _session_or_none(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        try:
            return self.store.session(session_id)
        except SessionNotFound:
            return None

    def _sprint_sessions(self) -> SprintSessions:
        if self.sprints is None:
            raise Refused(
                "validation", "this PO service was started without an instance; it resolves no sprint"
            )
        return self.sprints

    def _model_list(self) -> dict[str, tuple[str, ...]]:
        """The models a new session may take, per CLI (`models_from_instance`)."""
        return self.models if self.models is not None else models_from_instance(self._instance_config())

    def _effort_list(self) -> dict[str, tuple[str, ...]]:
        """The efforts a new session may take, per CLI (`efforts_from_instance`)."""
        if self.efforts is not None:
            return self.efforts
        return efforts_from_instance(self._instance_config())

    def _instance_config(self) -> Any:
        """instance.yaml's content, or None for a service started without an instance."""
        if self.instance is None:
            return None
        from ummanu.config import ConfigError, load_config

        try:
            return load_config(instance_file(self.instance))
        except ConfigError as exc:
            raise Refused("unavailable", f"the instance config cannot be read: {exc}") from None

    def _sent(self, session_id: str, request_id: str) -> dict[str, Any]:
        """Where an acknowledged message is now: its turn, or still in the queue."""
        known = self.store.request(request_id)
        if known is not None and known.seq is not None:
            turn = self.store.turn(known.session_id, int(known.seq))
            return {"session_id": session_id, "queued": False, "seq": turn.seq, "state": turn.state}
        if self.queue.find(request_id) is not None:
            return {"session_id": session_id, "queued": True, "seq": None, "state": None}
        raise Refused(
            "unavailable", "the message was queued and then set aside; the service journal says why"
        )

    def stop_turn(self, *, session_id: str, seq: int) -> dict[str, Any]:
        """Stop turn `seq` only if it is the one running; queued inputs of the session then go on."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise Refused("validation", "seq names the running turn to stop, as a whole number")
        try:
            turn = self.runner.stop_turn(_required(session_id, "session_id"), seq)
        finally:
            self._schedule_recovery()
        self._wake.set()
        return {"session_id": session_id, "seq": seq, "stopped": turn is not None}

    def close_session(self, *, session_id: str, actor: str = STOP_ACTOR) -> dict[str, Any]:
        """Close as `actor`; a running turn or a message still queued for the session refuses it."""
        session_id = _required(session_id, "session_id")
        with self._lock:
            waiting = self.queue.pending(session_id)
            if waiting:
                raise TurnInProgress(
                    f"{len(waiting)} message(s) queued in PO session {session_id} have not run yet; "
                    "wait for their answers, then close"
                )
            session = self.store.close_session(session_id, _required(actor, "actor"))
        return {"session_id": session.session_id, "state": session.state}

    def rename_session(self, *, session_id: str, title: str) -> dict[str, Any]:
        """Set a session's title, open or closed; an empty one clears it. A repeat sets the same value.

        No request id: the write is idempotent by its value. The rule is the store's (`session_title`).
        """
        if not isinstance(title, str):
            raise Refused("validation", "title is text; an empty one clears it")
        session = self.store.set_title(_required(session_id, "session_id"), title)
        return {"session_id": session.session_id, "title": session.title}

    def status(self) -> dict[str, Any]:
        return {
            "running": self.runner.live_count(),
            "queued": len(self.queue.pending()),
            "restart_pending": self.marker.exists(),
            "recovered": self._recovered,
        }

    def request_restart(self, *, reason: str = "") -> dict[str, Any]:
        """Restart after answering if idle, else defer until running turns settle.

        The requester's marker holds new inputs in the queue so they cannot starve the restart.
        """
        with self._lock:
            if not self.marker.exists():
                from ummanu.po.client import write_restart_marker

                write_restart_marker(self.data_dir, reason or "restart requested")
            running = max(self.runner.live_count(), len(self.store.running_turns()))
            if running:
                return {
                    "restart": "deferred",
                    "running": running,
                    "detail": f"PO service restart deferred: {running} turn(s) running",
                }
            self._exit.set()
            self._wake.set()
            return {"restart": "now", "running": 0, "detail": "PO service is idle and exits for the restart"}

    def restart_due(self) -> bool:
        return self.marker.exists() and self.runner.live_count() == 0 and not self.store.running_turns()

    # --- the wire ---------------------------------------------------------------------------

    def handle(self, request: Any) -> dict[str, Any]:
        """Answer one decoded request without raising.

        Request-id operations use _answer_id_operation to preserve the acceptance decision.
        """
        try:
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise Refused("validation", "a PO service request is a JSON object with an op")
            fields = {key: value for key, value in request.items() if key != "op"}
            operation = _OPERATIONS.get(request["op"])
            if operation is None:
                raise Refused("validation", f"the PO service has no operation {request['op']!r}")
            method = getattr(self, operation)
            try:
                inspect.signature(method).bind(**fields)
            except TypeError as exc:
                raise Refused("validation", f"{request['op']}: {exc}") from None
        except Refused as exc:
            return _error(exc.code, str(exc), nothing_written=True)
        if operation in ID_OPERATIONS:
            return self._answer_id_operation(method, fields)
        try:
            return {"ok": True, "result": method(**fields)}
        except Exception as exc:  # noqa: BLE001 - an answer, not a dead connection
            return _refusal(exc, nothing_written=False)

    def _answer_id_operation(
        self, method: Callable[..., dict[str, Any]], fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Preserve request acceptance across failures.

        Return the known answer after acceptance, outcome_unknown during a possibly committed write,
        and a refusal before acceptance. Mark nothing_written only for known no-write refusals.
        """
        acceptance = _Acceptance()
        token = _ACCEPTANCE.set(acceptance)
        try:
            return {"ok": True, "result": method(**fields)}
        except Exception as exc:  # noqa: BLE001 - an answer, not a dead connection
            if acceptance.answer is not None:
                _say(
                    f"ummanu po: request accepted, answered from acceptance after "
                    f"{type(exc).__name__}: {exc}"
                )
                return {"ok": True, "result": acceptance.answer}
            if acceptance.accepting:
                return _error(
                    "outcome_unknown",
                    f"the PO service may have accepted this request ({type(exc).__name__}: {exc}); "
                    "repeat it with the same request id",
                )
            definite = (
                isinstance(exc, _DEFINITE_REFUSALS) and getattr(exc, "code", "validation") == "validation"
            )
            return _refusal(exc, nothing_written=definite)
        finally:
            _ACCEPTANCE.reset(token)

    @staticmethod
    def _accepting() -> None:
        acceptance = _ACCEPTANCE.get()
        if acceptance is not None:
            acceptance.accepting = True

    @staticmethod
    def _accepted(answer: dict[str, Any]) -> None:
        acceptance = _ACCEPTANCE.get()
        if acceptance is not None:
            acceptance.accepting = True
            acceptance.answer = dict(answer)


class _Acceptance:
    """How far one id-taking request got: into its accepting write, and the answer known right after it."""

    def __init__(self) -> None:
        self.accepting = False
        self.answer: dict[str, Any] | None = None


_ACCEPTANCE: contextvars.ContextVar[_Acceptance | None] = contextvars.ContextVar(
    "po_acceptance", default=None
)
# The endpoint operations that take a request id; `handle` answers them through `_answer_id_operation`.
ID_OPERATIONS = frozenset({"create_session", "submit", "sprint_session"})


# Refusals raised before acceptance that are known to have written nothing (`nothing_written`).
_DEFINITE_REFUSALS = (Refused, RequestConflict, SessionClosed, SessionNotFound, RunnerError)


def _known_answer(session_id: str, known: PoRequest | QueuedInput) -> dict[str, Any]:
    """What a replayed message is known to be without another read: its turn's seq, or still queued."""
    if isinstance(known, PoRequest):
        return {"session_id": session_id, "queued": False, "seq": known.seq, "state": None, "repeated": True}
    return {"session_id": session_id, "queued": True, "seq": None, "state": None, "repeated": True}


def _refusal(exc: Exception, *, nothing_written: bool) -> dict[str, Any]:
    """One exception to one error answer; `nothing_written` marks a refusal that wrote nothing."""
    if isinstance(exc, Refused):
        code = exc.code
    elif isinstance(exc, SessionNotFound):
        code = "session_not_found"
    elif isinstance(exc, TitleRefused):
        code = "validation"
    elif isinstance(exc, SessionClosed):
        code = "session_closed"
    elif isinstance(exc, RequestConflict):
        code = "request_conflict"
    elif isinstance(exc, TurnInProgress):
        code = "turn_in_progress"
    elif isinstance(exc, RunnerError):
        code = "validation"
    elif isinstance(exc, (PoStoreError, QueueError)):
        return _error("unavailable", str(exc))
    else:
        return _error("unavailable", f"the PO service failed: {type(exc).__name__}: {exc}")
    return _error(code, str(exc), nothing_written=nothing_written)


_OPERATIONS = {
    "create_session": "create_session",
    "submit": "submit",
    "stop_turn": "stop_turn",
    "close_session": "close_session",
    "rename_session": "rename_session",
    "sprint_session": "sprint_session",
    "status": "status",
    "restart": "request_restart",
}


def _error(code: str, message: str, *, nothing_written: bool = False) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if nothing_written:
        error["nothing_written"] = True
    return {"ok": False, "error": error}


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise Refused("validation", f"{name} is required")
    return text


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# --- the socket -----------------------------------------------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        try:
            # Only a complete line runs: a sender whose write failed before its newline was
            # told "nothing written" (`ummanu.po.client`), and that has to stay true.
            if not raw.endswith(b"\n"):
                raise ValueError("incomplete request line")
            request = json.loads(raw)
        except ValueError:
            answer = _error(
                "validation",
                "a PO service request is one complete JSON object on one line",
                nothing_written=True,
            )
        else:
            answer = self.server.service.handle(request)  # type: ignore[attr-defined]
        self.wfile.write(json.dumps(answer, ensure_ascii=False, default=str).encode("utf-8") + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, service: PoService) -> None:
        self.service = service
        previous = os.umask(0o177)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)


@contextlib.contextmanager
def listening(service: PoService) -> Iterator[Path]:
    """Hold the service lock and serve the socket for as long as the block runs."""
    directory = service_dir(service.data_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = socket_path(service.data_dir)
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        raise ServiceStartError(f"the PO service socket path is too long for a Unix socket: {path}")
    with open(directory / LOCK_NAME, "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceStartError(
                f"another PO service already serves this installation (it holds {directory / LOCK_NAME})"
            ) from None
        # Under the lock no other service is alive, so a socket file left here is a dead one's.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        try:
            server = _Server(path, service)
        except OSError as exc:
            raise ServiceStartError(f"could not listen on {path}: {exc}") from None
        thread = threading.Thread(target=server.serve_forever, name="po-service-socket", daemon=True)
        thread.start()
        try:
            yield path
        finally:
            server.shutdown()
            server.server_close()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


# --- `ummanu po-serve` -------------------------------------------------------------------------


def add_po_serve_subcommands(subparsers) -> None:
    command = add_command_parser(subparsers, "po-serve")
    add_instance_argument(command, help="path to an instance dir or instance.yaml")
    command.add_argument(
        "--data-dir",
        default=os.environ.get("UMMANU_DATA_DIR"),
        help="override the instance's configured data directory",
    )
    command.set_defaults(handler=run_po_serve)


def run_po_serve(args: argparse.Namespace) -> int:
    from ummanu.config import DataDirError, instance_data_dir

    try:
        data_dir = Path(args.data_dir) if args.data_dir else instance_data_dir(Path(args.instance))
    except DataDirError as exc:
        _say(f"ummanu po-serve: {exc}")
        return 2
    service = PoService(
        PoRunner.for_instance(args.instance, data_dir),
        data_dir=data_dir,
        instance=args.instance,
        sprints=BoardSprintSessions(args.instance, data_dir),
    )
    try:
        with listening(service) as path:
            _say(f"ummanu po: serving {path}; queue {service.queue.directory}")
            # Before `start` fulfils a pending restart: a cleared marker then means a written receipt.
            _say(_write_process_receipt(data_dir))
            for line in service.start():
                _say(line)
            return service.run()
    except ServiceStartError as exc:
        _say(f"ummanu po-serve: {exc}")
        return 1


def _write_process_receipt(data_dir: Path) -> str:
    """Bind this process to the checkout its code was imported from; a journal line either way."""
    from ummanu.infra.process_receipts import ReceiptError
    from ummanu.installation.upgrade.services import write_po_process_receipt

    # src/ummanu/po/service.py: the checkout this process runs, not the one it was pointed at.
    product_root = Path(__file__).resolve().parents[3]
    try:
        return f"ummanu po: {write_po_process_receipt(data_dir, product_root)}"
    except ReceiptError as exc:
        return f"ummanu po: no process receipt was written ({exc}); an upgrade will ask for a restart"


__all__ = [
    "TICK_SECONDS",
    "PoService",
    "Refused",
    "ServiceStartError",
    "add_po_serve_subcommands",
    "listening",
    "run_po_serve",
]
