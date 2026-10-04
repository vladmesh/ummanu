"""Decision and operation cards: the dispatcher hands them to the sprint's PO session (secretary-1758).

A `decision` or `operation` card is executed by the PO service, not by a head. When the dispatcher
claims one it cuts no workspace and launches nothing: it resolves the sprint's PO session
(`PoService.sprint_session`) and submits one input to it (`PoService.submit`, `source: dispatcher`)
carrying the card, the sprint's comments and the exact `task complete` command. The PO answers it in
that turn and completes the card itself; its Done wakes the sprint observer like any other.

Every request id is derived at claim from the card ref and the claim attempt and kept on the card's
dispatcher record (`PoSubmission`), so a resolve or submit the service did not answer is repeated
next tick under the same id: a fresh one could open a second PO session. A service that stays down
leaves the card In progress and the tick degraded; it is not a failure of the card.

After the submit the per-tick check reads the PO store, never the service: `po_requests` names the
turn the input became once the service claimed it. A turn that settled while the card is still In
progress Blocks the card when completed/interrupted; failed execution instead escalates its
unresolved episode. Queued/running work escalates at 30 minutes from its audited claim. An input the service set aside in `po-queue/refused/` will never become a turn, so it
Blocks the card with the service's reason.

Every input carries the card's facts beside its text (`card_ref`, `kind`, `touches_production`,
`sprint_ref`; secretary-1764), frozen with the text since the service binds the submit id to both. The
service, and only the service, evaluates the sprint's production rights on them, and refuses nothing by
them (secretary-1769): an operation card's input is queued as a normal turn with the service's rights
section after its text. A production the sprint does not allow is the PO's to decide in that turn:
it records the allowance (`sprint allow-production`) and runs the operation, or hands the card to the
owner like any other.

A card the PO cut inside a PO turn with no sprint (secretary-1792) goes instead to the session of that
turn, its origin (`board/po_origin.py`), or to the session that succeeded it in the card's origin line:
no sprint is resolved, and a closed or missing session gets its successor at the submit
(`origin_returns.succeed_origin`). Such a card's input carries no sprint comments, and its facts an
empty `sprint_ref`; its production rights have no sprint allowance (the PO service's note says so). A
card with an origin records the session it is submitted to (`po_return.executor`), for the reader of
the card; it proves nothing about who completed it (the completion records that itself, `po_session`).

The PO may hand the card to the owner inside its turn (`task handover`, secretary-1761). A card that
carries that mark is not Blocked when the turn settles: it waits for the owner. A genuine owner
comment or a PO-recorded conversation quotation answers that epoch and becomes one follow-up to
the same session, carrying the reason, quotation and completion command, under a request id derived
from the card ref and answer event id. The PO completes the card with `task complete`,
which ends the card. The answer already cleared the handover atomically; a durable answer record
selects the follow-up episode; a completed turn without completion or a new handover returns
the unfinished card to the observer through Blocked.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ummanu.board import po_origin as origin_field
from ummanu.board.completion_evidence import missing_completion_evidence
from ummanu.board.owner_handover import (
    OWNER_ANSWER,
    attention_record,
    owner_answer_event_ids,
    po_episode,
    waiting_owner,
)
from ummanu.board.production_rights import (
    CARD_INPUT,
    NO_PRODUCTION,
    OPERATION_KIND,
    OWNER_ANSWER_INPUT,
    card_facts,
    touches_production,
)
from ummanu.board.terminal_taxonomy import normalize_terminal_taxonomy
from ummanu.dispatch.helpers import _worker_id
from ummanu.dispatch.origin_returns import record_return_state, succeed_origin
from ummanu.dispatch.po_delivery import DISPATCHER_SOURCE, SUCCESSORS_PER_TICK
from ummanu.dispatch.state import (
    DispatcherRecord,
    PoSubmission,
    request_token,
)
from ummanu.dispatch.state import (
    attempt_request_id as _attempt_request_id,
)
from ummanu.dispatch.state import (
    new_attempt_id as _new_attempt_id,
)
from ummanu.dispatch.state import (
    record_attempt as _record_attempt,
)
from ummanu.po.client import OutcomeUnknown, PoServiceError, ServiceRefused, ServiceUnavailable
from ummanu.po.queue import QueuedInput
from ummanu.po.store import (
    COMPLETED,
    FAILED,
    INTERRUPTED,
    PoRequest,
    PoStoreError,
    RequestConflict,
    SessionClosed,
    SessionNotFound,
    Turn,
)

#: Dispatcher record states of a PO-executed card.
PO_SUBMITTING = "po_submitting"
PO_SUBMITTED = "po_submitted"
#: Request-id actions, each under `attempt_request_id(<claim attempt>, <action>, <card ref>)`.
PO_SESSION_ACTION = "po-session"
PO_SUBMIT_ACTION = "po-submit"
PO_COMPLETE_ACTION = "po-complete"
PO_HANDOVER_ACTION = "po-handover"
PO_BLOCKED_ACTION = "po-card-blocked"
#: The follow-up input carrying the owner's answer: `dispatcher-po-owner-answer-<card>-<event id>`.
PO_OWNER_ANSWER_ACTION = "po-owner-answer"
#: Service refusal codes that say nothing about the request: it is repeated, never failed.
_UNANSWERED_CODES = frozenset({"unavailable", "outcome_unknown"})
#: `PoSubmission.session_outcome` of an out-of-sprint card: the session of the PO turn that cut it.
ORIGIN_SESSION = "origin"


class _SuccessorNotOpen(PoServiceError):
    """The origin session is closed and its successor could not be opened this tick; repeated next."""


class PoChannel(Protocol):
    """What the dispatcher needs of the PO service and its store."""

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]: ...

    def submit(
        self, *, session_id: str, text: str, request_id: str, source: str, card: dict[str, Any]
    ) -> dict[str, Any]: ...

    def create_session(self, *, cli: str, model: str, effort: str, request_id: str) -> dict[str, Any]: ...

    def successor_choice(self, session_id: str) -> tuple[str, str, str] | None: ...

    def request(self, request_id: str) -> PoRequest | None: ...

    def turn(self, session_id: str, seq: int) -> Turn: ...

    def queued(self, request_id: str) -> QueuedInput | None: ...

    def refused(self, request_id: str) -> dict[str, Any] | None: ...


class ServicePoChannel:
    """The installation's PO service socket and PO store. Construction does no I/O."""

    def __init__(self, data_dir: Path | str, instance_dir: Path | str | None) -> None:
        self.data_dir = Path(data_dir)
        self.instance_dir = instance_dir
        self._client: Any = None
        self._store: Any = None

    def _service(self) -> Any:
        if self._client is None:
            from ummanu.po.client import PoServiceClient

            self._client = PoServiceClient(self.data_dir)
        return self._client

    def _po_store(self) -> Any:
        if self._store is None:
            if self.instance_dir is None:
                raise PoStoreError("the dispatcher names no instance, so it has no PO store to read")
            from ummanu.po.store import PoStore

            try:
                self._store = PoStore.for_instance(self.instance_dir)
            except Exception as exc:  # credentials that cannot be read are an unanswered store
                raise PoStoreError(f"the PO store is not available: {type(exc).__name__}: {exc}") from exc
        return self._store

    def sprint_session(self, *, sprint_ref: str, request_id: str) -> dict[str, Any]:
        return self._service().sprint_session(sprint_ref=sprint_ref, request_id=request_id)

    def submit(
        self, *, session_id: str, text: str, request_id: str, source: str, card: dict[str, Any]
    ) -> dict[str, Any]:
        return self._service().submit(
            session_id=session_id, text=text, request_id=request_id, source=source, card=card
        )

    def create_session(self, *, cli: str, model: str, effort: str, request_id: str) -> dict[str, Any]:
        return self._service().create_session(cli=cli, model=model, effort=effort, request_id=request_id)

    def successor_choice(self, session_id: str) -> tuple[str, str, str] | None:
        """The CLI, model and effort a successor of this closed or missing session opens with.

        The service's own rule (`ummanu.po.models.successor_choice`) over the session's row and
        the instance's offered models and efforts; None when the installation offers no model.
        """
        from ummanu.config import ConfigError, load_config
        from ummanu.po.models import efforts_from_instance, models_from_instance, successor_choice
        from ummanu.po.store import SessionNotFound

        try:
            row = self._po_store().session(session_id)
            previous: tuple[str, str, str] | None = (row.cli, row.model, row.effort)
        except SessionNotFound:
            previous = None
        instance = Path(self.instance_dir) if self.instance_dir is not None else None
        try:
            config = (
                load_config(instance / "instance.yaml" if instance.is_dir() else instance)
                if instance is not None
                else None
            )
        except ConfigError as exc:
            raise PoStoreError(f"the instance config cannot be read: {exc}") from exc
        return successor_choice(previous, models_from_instance(config), efforts_from_instance(config))

    def request(self, request_id: str) -> PoRequest | None:
        return self._po_store().request(request_id)

    def turn(self, session_id: str, seq: int) -> Turn:
        return self._po_store().turn(session_id, seq)

    def _queue(self) -> Any:
        from ummanu.po.queue import PoQueue

        return PoQueue(self.data_dir)

    def queued(self, request_id: str) -> QueuedInput | None:
        """The input still waiting in the service's queue under `request_id`, read from its directory."""
        from ummanu.po.queue import QueueError

        try:
            return self._queue().find(request_id)
        except QueueError as exc:
            raise PoStoreError(str(exc)) from exc

    def refused(self, request_id: str) -> dict[str, Any] | None:
        """The input the service set aside in `refused/` under `request_id`, with its `reason`."""
        from ummanu.po.queue import QueueError

        try:
            return self._queue().find_refused(request_id)
        except QueueError as exc:
            raise PoStoreError(str(exc)) from exc


def complete_command(reference: str, kind: str, request_id: str) -> str:
    """The exact command the PO runs to complete the card, as its input quotes it."""
    return (
        f"python3 -P -m ummanu task complete --ref {reference} --role po --kind {kind} "
        f"--body-file <file> --request-id {request_id}"
    )


def handover_command(reference: str, request_id: str) -> str:
    """The exact command the PO runs to hand the card to the owner instead of completing it."""
    command = (
        f"python3 -P -m ummanu task handover --ref {reference} --role po --to owner --reason-file <file>"
    )
    return f"{command} --request-id {request_id}" if request_id else command


def completion_sections(kind: str) -> tuple[str, str]:
    from ummanu.board.completion_evidence import PO_COMPLETION_SECTIONS

    first, second = PO_COMPLETION_SECTIONS[kind]
    return first, second


def _of_sprint(submission: PoSubmission) -> str:
    """Where the card belongs, as the inputs say it: its sprint, or no sprint at all."""
    if submission.sprint_ref:
        return f"of {submission.sprint_ref}"
    return "(outside every sprint; you cut it in this session, so this session executes it)"


def render_po_card_input(
    task: dict[str, Any], sprint: dict[str, Any] | None, submission: PoSubmission
) -> str:
    """The one input a decision/operation card becomes in its sprint's PO session.

    A card a PO session cut outside every sprint (`sprint` None, secretary-1792) goes to that session
    instead, with no sprint comments to carry.
    """
    reference = str(task.get("ref") or "")
    kind = submission.kind
    first, second = completion_sections(kind)
    lines = [
        (
            f"The dispatcher hands you {kind} card {reference} {_of_sprint(submission)}. Answer it in "
            "this turn and complete the card before the turn ends; a turn that ends with the card still "
            "In progress Blocks it, unless you handed it to the owner in this turn."
        ),
        "",
        f"Card: {reference} ({kind}): {task.get('title') or ''}",
        *_production_lines(submission),
        "",
        "## Card body",
        "",
        str(task.get("description") or "").strip() or "(empty)",
        "",
    ]
    if sprint is not None:
        lines += [f"## Comments of {submission.sprint_ref}, in board order", ""]
        comments = [comment for comment in sprint.get("comments") or [] if isinstance(comment, dict)]
        if not comments:
            lines.append("(none)")
        for comment in comments:
            lines += [
                f"### {comment.get('created_at') or 'undated'}",
                "",
                str(comment.get("body") or "").rstrip(),
                "",
            ]
    lines += [
        "",
        "## Complete the card",
        "",
        (
            f"Write a body file with two non-empty sections, `## {first}` and `## {second}` (a command "
            "or an observation someone can repeat), then run exactly:"
        ),
        "",
        "    " + complete_command(reference, kind, submission.complete_request_id),
        "",
        "## Or hand it to the owner",
        "",
        (
            "Only when a person is needed: money, a key or access only the owner holds, or a product "
            "decision that is the owner's. An architecture fork is yours to decide. Write the reason "
            "(what the owner has to decide or do) to a file, run exactly this and end the turn; the card "
            "stays In progress and waits, and the owner's answer comes back to this session:"
        ),
        "",
        "    " + handover_command(reference, submission.handover_request_id),
        "",
        "Keep the turn short; anything long-running becomes a card.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _production_lines(submission: PoSubmission, *, owner_answer: bool = False) -> list[str]:
    """What an operation card's input says about the production it touches; nothing for a decision.

    The card's own input points at the PO service's rights section, which the service adds after the
    text: only the service evaluates the rule. The owner's answer to a handed-over card is not
    evaluated again, so its line says the owner decided.
    """
    production = submission.card.get("touches_production")
    if submission.kind != OPERATION_KIND or not production:
        return []
    if production == NO_PRODUCTION:
        return [f"Touches production: {NO_PRODUCTION}. Touch no production in this turn."]
    if owner_answer:
        return [
            (
                f"Touches production: {production}. Follow the quoted owner answer below and the "
                "sprint's effective recorded authority; touch no other production in this turn."
            )
        ]
    if not submission.sprint_ref:
        return [
            (
                f"Touches production: {production}. The card belongs to no sprint, so no sprint allowance "
                "applies; the PO service's production rights section at the end of this input says what "
                "does. Touch no other production in this turn."
            )
        ]
    return [
        (
            f"Touches production: {production}. Whether the sprint allows it is in the PO service's "
            "production rights section at the end of this input; touch no other production in this turn."
        )
    ]


def po_card_facts(task: dict[str, Any], submission: PoSubmission, *, input: str = CARD_INPUT) -> dict[str, Any]:
    """The facts an input about this card carries beside its text (`PoService.submit`)."""
    return card_facts(
        card_ref=str(task.get("ref") or ""),
        kind=submission.kind,
        touches_production=touches_production(task),
        sprint_ref=submission.sprint_ref,
        input=input,
    )


def owner_answer_request_id(reference: str, event_id: str) -> str:
    """The follow-up input's request id: the card ref and the owner comment's event id, nothing else."""
    return "-".join(request_token(part) for part in ("dispatcher", PO_OWNER_ANSWER_ACTION, reference, event_id))


def render_owner_answer_input(
    task: dict[str, Any], submission: PoSubmission, mark: dict[str, str], answers: list[dict[str, str]]
) -> str:
    """The follow-up input the owner's answer on a handed-over card becomes in the same PO session."""
    reference = str(task.get("ref") or "")
    kind = submission.kind
    first, second = completion_sections(kind)
    lines = [
        (
            f"The owner answered {kind} card {reference} {_of_sprint(submission)}, which you handed to "
            f"the owner on {mark['since']}. Finish the work and complete the card in this turn. Only "
            "a new unresolved question needing the owner warrants a new explicit handover and request ID. Ending "
            "this turn without completing the card or a new handover returns the unfinished card "
            "to the observer through Blocked. The recorded answer has ended the previous owner turn."
        ),
        "",
        f"Card: {reference} ({kind}): {task.get('title') or ''}",
        *_production_lines(submission, owner_answer=True),
        "",
        "## Why you handed it to the owner",
        "",
        mark["reason"],
        "",
        "## Recorded owner answer",
        "",
    ]
    for answer in answers:
        lines += [f"### {answer['created_at'] or 'undated'}", "", answer["body"] or "(empty)", ""]
    lines += ["Recording this answer applies no grant. Reference an existing standing decision by its ID; "
              "do not apply an already recorded grant again.", ""]
    if submission.owner_event_id:
        # A command that applies the owner's answer on the owner's authority names it (`--authorized-by`
        # of `sprint e2e-budget`, secretary-1796).
        answer = attention_record(task, OWNER_ANSWER) or {}
        if answer.get("channel", "comment") == "comment":
            lines += [f"The owner's comment is event `{submission.owner_event_id}`.", ""]
        else:
            lines += [f"The quoted owner answer is recorded as event `{submission.owner_event_id}`.",
                "This answer applies no grant. Record new sprint authority with `sprint record-owner-decisions` "
                "and stable decision IDs; do not apply an already recorded grant again.", ""]
    lines += [
        "",
        "## Complete the card",
        "",
        (
            f"Write a body file with two non-empty sections, `## {first}` and `## {second}` (a command "
            "or an observation someone can repeat), then run exactly:"
        ),
        "",
        "    " + complete_command(reference, kind, submission.complete_request_id),
        "",
        "Keep the turn short; anything long-running becomes a card.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def claim_po_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Claim a Ready decision/operation card and submit it; no workspace, no head."""
    ref = task["ref"]
    claim_id = _attempt_request_id(attempt_id, "claim", ref)
    if records.get(ref) is not None or runtime.audit.committed_event(claim_id) is not None:
        # A card back in Ready (after a Blocked, say) is a new attempt with new request ids, so the
        # previous attempt's input is never replayed into this one.
        attempt_id = _new_attempt_id()
        _record_attempt(payload, attempt_id, ref, runtime.owner, runtime.owner)
        payload["attempt_id"] = attempt_id
        claim_id = _attempt_request_id(attempt_id, "claim", ref)
    runtime.writer.claim(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        worker=_worker_id(task),
        request_id=claim_id,
    )
    claimed = runtime.reader.show(ref)
    record = _po_record(claimed, attempt_id)
    records[ref] = record
    runtime.save_records(payload, records)
    if not record.po_submission.sprint_ref and origin_field.po_origin(claimed) is None:
        # Outside every sprint only the PO session that cut it executes it (secretary-1792).
        return _block(
            runtime,
            claimed,
            records,
            payload,
            attempt_id,
            f"a {record.po_submission.kind} card names no sprint and no PO session it came from, so there "
            "is no PO session to execute it",
        )
    return advance_po_card(runtime, claimed, records, payload, attempt_id)


def _po_record(task: dict[str, Any], attempt_id: str, *, worker: str = "") -> DispatcherRecord:
    ref = task["ref"]
    return DispatcherRecord(
        worker=worker or _worker_id(task),
        workspace="",
        handle="",
        head="",
        review_head="",
        attempt_id=attempt_id,
        comment_baseline=len(task.get("comments") or []),
        review_baseline=0,
        state=PO_SUBMITTING,
        claimed_at=time.time(),
        po_submission=PoSubmission(
            kind=str(task.get("type") or ""),
            sprint_ref=str(task.get("sprint") or ""),
            session_request_id=_attempt_request_id(attempt_id, PO_SESSION_ACTION, ref),
            submit_request_id=_attempt_request_id(attempt_id, PO_SUBMIT_ACTION, ref),
            complete_request_id=_attempt_request_id(attempt_id, PO_COMPLETE_ACTION, ref),
            handover_request_id=_attempt_request_id(attempt_id, PO_HANDOVER_ACTION, ref),
        ),
    )


def _recovered_record(runtime: Any, task: dict[str, Any]) -> DispatcherRecord | None:
    """Rebuild a lost record from the dispatcher's own claim of the card, or None when there is none.

    The claim's request id carries the claim attempt, and every other id is derived from it, so the
    rebuilt record repeats exactly the requests the lost one made.
    """
    ref = task["ref"]
    prefix, suffix = "dispatcher-", f"-claim-{request_token(ref)}"
    attempt = ""
    for event in runtime.audit.events(ref):
        request_id = str(event.get("request_id") or "")
        if (
            request_id.startswith(prefix)
            and request_id.endswith(suffix)
            and len(request_id) > len(prefix + suffix)
        ):
            attempt = request_id[len(prefix) : -len(suffix)]
    if not attempt:
        return None
    worker = str((task.get("claim") or {}).get("worker") or "")
    return _po_record(task, attempt, worker=worker)


def advance_po_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """One tick of a decision/operation card, whatever column it stands in."""
    ref = task["ref"]
    state = str(task.get("state") or "")
    if state == "ready":
        return claim_po_card(runtime, task, records, payload, attempt_id)
    if state != "in_progress":
        record = records.pop(ref, None)
        if record is not None:
            runtime.save_records(payload, records)
        return _closed(task, attempt_id, record)
    record = records.get(ref)
    if record is None or not record.po_submission:
        record = _recovered_record(runtime, task)
        if record is None:
            return _block(
                runtime,
                task,
                records,
                payload,
                attempt_id,
                "the card is In progress but the dispatcher never claimed it, so it was never submitted "
                "to the PO; move it back to Ready to submit it",
            )
        records[ref] = record
        runtime.save_records(payload, records)
    if waiting_owner(task) is not None or attention_record(task, OWNER_ANSWER) is not None:
        return _await_owner(runtime, task, record, records, payload)
    if not record.po_submission.submitted:
        outcome = _episode_outcome(runtime, task, record, records, payload)
        if outcome is not None:
            return outcome
        return _submit(runtime, task, record, records, payload)
    return _settle(runtime, task, record, records, payload)


def _submit(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    submission = record.po_submission
    step = "resolve"
    origin = origin_field.po_origin(task)
    try:
        if not submission.session_id:
            if submission.sprint_ref:
                answer = runtime.po.sprint_session(
                    sprint_ref=submission.sprint_ref, request_id=submission.session_request_id
                )
                submission.session_id = str(answer["session_id"])
                submission.session_outcome = "created" if answer.get("created") else "recorded"
            else:
                # Cut outside every sprint inside a PO turn: that session (or its successor) runs it.
                assert origin is not None  # the claim Blocks such a card with no origin
                submission.session_id = origin_field.line_head(origin["session"], origin_field.return_state(task))
                submission.session_outcome = ORIGIN_SESSION
            runtime.save_records(payload, records)
        step = "submit"
        if not submission.card:
            submission.card = po_card_facts(task, submission)
            runtime.save_records(payload, records)
        if not submission.text:
            sprint = (
                runtime.sprints.show(submission.sprint_ref, include_resume_freshness=False)
                if submission.sprint_ref
                else None
            )
            submission.text = render_po_card_input(task, sprint, submission)
            runtime.save_records(payload, records)
        answer = _submit_card(runtime, task, origin, submission, records, payload)
    except _SuccessorNotOpen as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except (ServiceUnavailable, OutcomeUnknown) as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except ServiceRefused as exc:
        if exc.code in _UNANSWERED_CODES:
            return _unanswered(runtime, task, record, records, payload, step, exc)
        return _refused(runtime, task, record, records, payload, step, exc)
    except RequestConflict as exc:
        # Read the same episode after a conflicting retry; unavailable evidence stays degraded.
        outcome = _episode_outcome(runtime, task, record, records, payload) if step == "submit" else None
        if outcome is not None:
            return outcome
        return _refused(runtime, task, record, records, payload, step, exc)
    except (SessionClosed, SessionNotFound) as exc:
        return _refused(runtime, task, record, records, payload, step, exc)
    except PoStoreError as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except PoServiceError as exc:
        return _refused(runtime, task, record, records, payload, step, exc)
    submission.submitted = True
    seq = answer.get("seq")
    submission.seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
    submission.unanswered = 0
    submission.last_error = ""
    record.state = PO_SUBMITTED
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": "po-card-submitted",
        "po_session": submission.session_id,
        "po_session_outcome": submission.session_outcome,
        "po_request_id": submission.submit_request_id,
        "seq": submission.seq,
    }


def _submit_card(
    runtime: Any,
    task: dict[str, Any],
    origin: dict[str, str] | None,
    submission: PoSubmission,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Submit the card's input to its session; a closed origin session of an out-of-sprint card is succeeded.

    A card with an origin records on itself the session it is handed to (`po_return.executor`,
    before the submit), for the reader of the card; the result return does not ask it, since only the
    completion's own `po_session` proves who completed the card (`dispatch/origin_returns.py`). An
    out-of-sprint card whose session is closed or
    missing goes to the successor of the origin's line (`succeed_origin`) under the same submit id; a
    sprint's card is refused as before, its session being the sprint's resolver's to answer for.
    """
    state = origin_field.return_state(task) if origin is not None else None
    for _ in range(SUCCESSORS_PER_TICK + 1):
        if state is not None and state.executor != submission.session_id:
            state.executor = submission.session_id
            record_return_state(runtime, task["ref"], state)
        try:
            return runtime.po.submit(
                session_id=submission.session_id,
                text=submission.text,
                request_id=submission.submit_request_id,
                source=DISPATCHER_SOURCE,
                card=submission.card,
            )
        except (SessionClosed, SessionNotFound):
            if submission.sprint_ref or origin is None or state is None:
                raise
            closed = submission.session_id
            following, why = succeed_origin(runtime, task, origin, state, closed)
            if not following:
                raise _SuccessorNotOpen(why) from None
            submission.session_id = following
            runtime.save_records(payload, records)
    raise _SuccessorNotOpen(
        f"every successor of PO session {submission.session_id} opened this tick was closed again"
    )


def _settle(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    outcome = _episode_outcome(runtime, task, record, records, payload)
    if outcome is not None:
        return outcome
    # The delivery record disappeared. Retry the frozen input under the same episode ID,
    # after the shared rule has applied the persisted deadline and current board precedence.
    record.po_submission.submitted = False
    return _submit(runtime, task, record, records, payload)


def _episode_outcome(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """One outcome rule for the current audited claim or answer and its actual PO request.

    None means no delivery is recorded: the caller may retry that episode's stable submission ID.
    The board is reread after the PO store, before a turn or clock can have a consequence.
    """
    ref = task["ref"]
    current = runtime.reader.show(ref)
    superseded = getattr(runtime.writer, "_card_superseded", None)
    if (current.get("closed") or current.get("state") != "in_progress"
            or (superseded is not None and superseded(ref))):
        records.pop(ref, None)
        runtime.save_records(payload, records)
        return _closed(current, record.attempt_id, record)
    if waiting_owner(current) is not None:
        return _waiting(record, ref, "po-card-waiting-owner", f"handed to the owner: {waiting_owner(current)['reason']}")
    answer = attention_record(current, OWNER_ANSWER)
    previous = attention_record(task, OWNER_ANSWER)
    if answer != previous:
        return _await_owner(runtime, current, record, records, payload) if answer else _episode_outcome(
            runtime, current, record, records, payload)
    submission = record.po_submission
    episode = ({"event_id": answer["event_id"], "occurred_at": answer["at"]} if answer
               else po_episode(runtime.audit.events(ref)))
    request_id = owner_answer_request_id(ref, answer["event_id"]) if answer else submission.submit_request_id
    turn = None
    try:
        known = runtime.po.request(request_id)
        set_aside = None if known is not None else runtime.po.refused(request_id)
        queued = runtime.po.queued(request_id) if known is None and set_aside is None else None
        if known is not None and known.seq is not None:
            turn = runtime.po.turn(known.session_id, int(known.seq))
    except PoStoreError as exc:
        return {
            "status": "degraded",
            "step": "po-card",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": "po-store-unanswered",
            "reason": f"the PO store did not answer for the card's turn: {exc}",
        }
    latest = runtime.reader.show(ref)
    if latest != current or (superseded is not None and superseded(ref)):
        return _episode_outcome(runtime, current, record, records, payload)
    if known is not None or queued is not None:
        delivery = known if known is not None else queued
        submission.session_id = delivery.session_id
        if answer:
            submission.owner_submitted = True
        else:
            submission.submitted = True
            submission.seq = known.seq if known is not None else None
            record.state = PO_SUBMITTED
        runtime.save_records(payload, records)
    if set_aside is not None:
        subject = "owner's answer" if answer else "card's input"
        return _block(runtime, current, records, payload, record.attempt_id,
                      f"the PO service set the {subject} aside and will not run it: "
                      f"{set_aside.get('reason') or 'no reason recorded'}")
    if turn is not None and turn.state in {COMPLETED, INTERRUPTED}:
        if turn.state == INTERRUPTED and answer:
            return _waiting(record, ref, "po-card-owner-answer-turn-ended",
                            "the recorded owner answer reached the PO, but its turn was interrupted; "
                            "a new owner comment requires a new explicit handover for a new question")
        return _block(runtime, current, records, payload, record.attempt_id,
                      f"PO turn {turn.session_id}/{turn.seq} ended {turn.state} without completing the card")
    failed = turn is not None and turn.state == FAILED
    if episode is not None and (failed or time.time() - datetime.fromisoformat(
            episode["occurred_at"]).timestamp() >= 30 * 60):
        from ummanu.tasks import TaskError
        reason = ("PO execution failed without its required response" if failed
                  else "PO ownership episode has no required response after 30 minutes")
        try:
            runtime.writer.escalate_po_card(actor=runtime.owner, reference=ref,
                                           episode=episode["event_id"], reason=reason)
        except TaskError as exc:
            if exc.code != "attention_resolved":
                raise
    if turn is not None:
        action = "po-card-owner-answer-turn-ended" if answer and failed else (
            "po-card-owner-answered" if answer else "po-card-turn-failed" if failed else "po-card-turn-running")
        return _waiting(record, ref, action, "PO execution failed; unresolved episode escalated" if failed
                        else f"PO turn {turn.session_id}/{turn.seq} runs")
    if known is not None or queued is not None:
        return _waiting(record, ref, "po-card-owner-answered" if answer else "po-card-queued",
                        "the input waits in the PO queue")
    return None


def _await_owner(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Deliver the board-recorded answer once, retaining its epoch and originating session.

    Released owner comments with a still-open mark are first accepted through the audited
    compatibility writer. The answer's stable event ID owns every retry and lost-record recovery.
    """
    ref = task["ref"]
    current = runtime.reader.show(ref)
    if current != task:
        return advance_po_card(runtime, current, records, payload, record.attempt_id)
    submission = record.po_submission
    answer = attention_record(task, OWNER_ANSWER)
    mark = waiting_owner(task) or {}
    if answer is None:
        try:
            answered = owner_answer_event_ids(runtime.audit.events(ref))
            from ummanu.tasks import TaskError
            for event_id in reversed(answered):
                try:
                    runtime.writer.accept_owner_comment(actor=runtime.owner, reference=ref, event_id=event_id)
                except TaskError as exc:
                    if exc.code == "empty_owner_answer":
                        continue
                    raise
                task = runtime.reader.show(ref)
                answer = attention_record(task, OWNER_ANSWER)
                break
        except Exception as exc:  # noqa: BLE001 - recovery is retried, never guessed by a read
            return {**_waiting(record, ref, "po-owner-answer-unread", f"owner answer recovery unavailable: {exc}"),
                    "status": "degraded"}
    if answer is None:
        return _waiting(record, ref, "po-card-waiting-owner", f"handed to the owner: {mark.get('reason') or ''}")
    outcome = _episode_outcome(runtime, task, record, records, payload)
    if outcome is not None:
        return outcome
    mark = answer["mark"]
    request_id = owner_answer_request_id(ref, answer["event_id"])
    if submission.owner_request_id != request_id:
        # The handover's session is authoritative even if a dispatcher record was rebuilt.
        submission.session_id = answer.get("po_session") or submission.session_id
        if not submission.session_id:
            # Released handovers could omit their session. Read only the initial request's
            # session identity; its ended turn cannot decide the answer episode's outcome.
            try:
                initial = runtime.po.request(submission.submit_request_id)
            except PoStoreError as exc:
                return _unanswered(runtime, task, record, records, payload, "owner answer session", exc)
            if initial is not None:
                submission.session_id = initial.session_id
            elif submission.sprint_ref:
                try:
                    resolved = runtime.po.sprint_session(sprint_ref=submission.sprint_ref,
                                                        request_id=submission.session_request_id)
                    submission.session_id = str(resolved["session_id"])
                except ServiceRefused as exc:
                    if exc.code in _UNANSWERED_CODES:
                        return _unanswered(runtime, task, record, records, payload, "owner answer session", exc)
                    return _refused(runtime, task, record, records, payload, "owner answer session", exc)
                except (SessionClosed, SessionNotFound) as exc:
                    return _refused(runtime, task, record, records, payload, "owner answer session", exc)
                except (ServiceUnavailable, OutcomeUnknown, PoStoreError) as exc:
                    return _unanswered(runtime, task, record, records, payload, "owner answer session", exc)
                except PoServiceError as exc:
                    return _refused(runtime, task, record, records, payload, "owner answer session", exc)
        submission.owner_event_id = answer["event_id"]
        submission.owner_request_id = request_id
        submission.owner_text = render_owner_answer_input(task, submission, mark,
            answer.get("comments") or [{"created_at": answer["at"], "body": answer["quotation"]}])
        submission.owner_submitted = False
        runtime.save_records(payload, records)
    submission.owner_submitted = False
    step = "owner answer"
    try:
        runtime.po.submit(
            session_id=submission.session_id,
            text=submission.owner_text,
            request_id=request_id,
            source=DISPATCHER_SOURCE,
            card=po_card_facts(task, submission, input=OWNER_ANSWER_INPUT),
        )
    except (ServiceUnavailable, OutcomeUnknown) as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except ServiceRefused as exc:
        if exc.code in _UNANSWERED_CODES:
            return _unanswered(runtime, task, record, records, payload, step, exc)
        return _refused(runtime, task, record, records, payload, step, exc)
    except RequestConflict as exc:
        outcome = _episode_outcome(runtime, task, record, records, payload)
        if outcome is not None:
            return outcome
        return _refused(runtime, task, record, records, payload, step, exc)
    except (SessionClosed, SessionNotFound) as exc:
        return _refused(runtime, task, record, records, payload, step, exc)
    except PoStoreError as exc:
        return _unanswered(runtime, task, record, records, payload, step, exc)
    except PoServiceError as exc:
        return _refused(runtime, task, record, records, payload, step, exc)
    submission.owner_submitted = True
    submission.unanswered = 0
    submission.last_error = ""
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": "po-owner-answer-submitted",
        "po_session": submission.session_id,
        "po_request_id": request_id,
        "owner_event_id": submission.owner_event_id,
    }


def _waiting(record: DispatcherRecord, ref: str, action: str, reason: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": action,
        "reason": reason,
    }


def _unanswered(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    step: str,
    exc: Exception,
) -> dict[str, Any]:
    """The service did not answer: the same request is repeated next tick, and the card waits."""
    submission = record.po_submission
    submission.unanswered += 1
    submission.last_error = f"{step}: {type(exc).__name__}: {exc}"[:500]
    runtime.save_records(payload, records)
    return {
        "status": "degraded",
        "step": "po-card",
        "pilot_ref": task["ref"],
        "attempt_id": record.attempt_id,
        "action": "po-service-unanswered",
        "reason": f"the PO service did not answer the {step}; it is repeated next tick with the same "
        f"request id: {exc}",
        "unanswered": submission.unanswered,
    }


def _refused(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    step: str,
    exc: Exception,
) -> dict[str, Any]:
    """The service refused the resolve or the submit outright: nothing will run the card."""
    record.po_submission.last_error = f"{step}: {type(exc).__name__}: {exc}"[:500]
    return _block(
        runtime,
        task,
        records,
        payload,
        record.attempt_id,
        f"the PO service refused the {step} of this card: {exc}",
    )


def _block(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    reason: str,
) -> dict[str, Any]:
    ref = task["ref"]
    current = runtime.reader.show(ref)
    record = records.get(ref)
    superseded = getattr(runtime.writer, "_card_superseded", None)
    if (current.get("closed") or (task.get("state") == "in_progress" and current.get("state") != "in_progress")
            or (superseded is not None and superseded(ref))):
        records.pop(ref, None)
        runtime.save_records(payload, records)
        return _closed(current, attempt_id, record)
    if record is not None and (waiting_owner(current) is not None or
            attention_record(current, OWNER_ANSWER) != attention_record(task, OWNER_ANSWER)):
        return _await_owner(runtime, current, record, records, payload)
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="blocked",
        reason=reason,
        request_id=_attempt_request_id(attempt_id, PO_BLOCKED_ACTION, ref),
        terminal_taxonomy=normalize_terminal_taxonomy(
            disposition="blocked", blocked_reason="other"
        ).to_record(),
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": "po-card",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": PO_BLOCKED_ACTION,
        "reason": reason,
    }


def _closed(task: dict[str, Any], attempt_id: str, record: DispatcherRecord | None) -> dict[str, Any]:
    """A decision/operation card that left In progress: its dispatcher record, if any, is closed."""
    return {
        "status": "ok",
        "step": "po-card",
        "pilot_ref": task["ref"],
        "attempt_id": record.attempt_id if record is not None else attempt_id,
        "action": "po-card-closed",
        "state": str(task.get("state") or ""),
        "completion": completion_state(task),
    }


def completion_state(task: dict[str, Any]) -> str:
    """`recorded` when the card carries its PO completion record, else what it lacks."""
    missing = missing_completion_evidence(task)
    return f"missing [{missing}]" if missing else "recorded"


__all__ = [
    "DISPATCHER_SOURCE",
    "PO_BLOCKED_ACTION",
    "PO_COMPLETE_ACTION",
    "PO_HANDOVER_ACTION",
    "PO_OWNER_ANSWER_ACTION",
    "PO_SESSION_ACTION",
    "PO_SUBMITTED",
    "PO_SUBMITTING",
    "PO_SUBMIT_ACTION",
    "PoChannel",
    "ServicePoChannel",
    "advance_po_card",
    "claim_po_card",
    "complete_command",
    "completion_state",
    "handover_command",
    "owner_answer_request_id",
    "po_card_facts",
    "render_owner_answer_input",
    "render_po_card_input",
]
