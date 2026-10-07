"""Wait cards: the dispatcher advances them once per tick, with no head (secretary-1790).

A `wait` card (`board/wait_card.py`) waits for one fact: a GitHub Actions run concluding, another card
reaching one of a named set of states, or a point in time. When the dispatcher claims a Ready one it
cuts no workspace, launches nothing and runs no broad check or Git preflight: the card moves to In
progress and each tick after that observes the target once.

- A GitHub run is only read, `GET repos/{repo}/actions/runs/{id}` through the gate's `_gh_api`, the
  same one read per tick the CI gate makes. Nothing here starts, reruns or dispatches a workflow.
- Everything the wait knows is the card's own `wait_state` field, rewritten only when it changed. A
  new dispatcher process reads it back and continues; there is no watcher to lose.
- The first terminal fact is frozen into `wait_state.result` before anything is delivered, and it
  is never overwritten: `target_reached` (a run's conclusion, `failure` included; the card state
  reached; the time), `cancelled` (`task cancel`), `deadline_passed`, or `source_unreachable` (a
  definitive 404/410 or no access, a card that does not exist, or transient errors lasting the
  wait's transient window). `_settle` is the one place that freezes it, and the one place the
  deadline is enforced: at or past the deadline only `deadline_passed` is frozen, unless the source's
  own timestamp puts the target at or before it.

Delivery is keyed by (card, address, frozen result) and recorded on the card only after the
receiving side accepted it; a delivery repeated after a crash carries the same key, and the receiving
side makes it a no-op:

- `po-session:<id>`: one input through the PO service (`source: dispatcher`) under a request id
  derived from that key, which the service deduplicates. A service that does not answer postpones
  the delivery to the next tick. A session that is closed or missing gets one successor (the sprint's
  resolver for the sprint's own session, `create_session` with its CLI, model and effort otherwise),
  and the input goes there: no result is given up.
- `dependents`: one dispatcher comment on every card whose `blocked_by` names the wait card, under a
  request id derived from the key; on an outcome other than `target_reached`, a Ready dependent is
  also Blocked with the outcome as its reason. Until then the claim pass leaves such a card in Ready
  (:func:`pending_wait_blockers`).
- `card:<ref>` (only on a wait the dispatcher created for a code card's e2e run, secretary-1795): one
  dispatcher comment on that card under a request id derived from the key. The card's own e2e stage
  (`dispatch/e2e_stage.py`) reads the frozen result off this wait card and acts on it; the comment is
  what the card's history shows of it. A card that no longer exists takes nothing and gives the
  delivery up; any other board error postpones it.
- `observer`: the terminal move itself, last: Done for `target_reached`, Blocked for every other
  outcome, with the result as the move's comment. The observer's wake on Done/Blocked is the delivery.

A wait card's Blocked, and a dependent's, carry `wait_outcome` in their transition data: an outcome,
not a pipeline restart, so the sprint budget does not charge them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ummanu.board import wait_card
from ummanu.board.completion_evidence import is_wait
from ummanu.board.production_rights import WAIT_KIND, WAIT_OUTCOME_INPUT, card_facts
from ummanu.board.terminal_taxonomy import normalize_terminal_taxonomy
from ummanu.board.tick_snapshot import select_cards
from ummanu.board.wait_card import (
    ACCEPTED,
    CANCELLED,
    DEADLINE_PASSED,
    DEPENDENTS,
    SOURCE_UNREACHABLE,
    TARGET_CARD,
    TARGET_REACHED,
    TARGET_RUN,
    WaitSpec,
    WaitSpecError,
    WaitState,
    parse_utc,
    utc_text,
)
from ummanu.dispatch.gate import _HTTP_STATUS_RE, GateTransportError, _gh_api
from ummanu.dispatch.helpers import _worker_id
from ummanu.dispatch.po_delivery import deliver, open_successor
from ummanu.dispatch.state import (
    DispatcherRecord,
    attempt_request_id as _attempt_request_id,
    new_attempt_id as _new_attempt_id,
    record_attempt as _record_attempt,
    request_token,
)
from ummanu.dispatch.types import HostError
from ummanu.tasks import TaskError, recorded_card_transition

WAIT_STEP = "wait-card"
#: Request-id actions of a delivery, each under `delivery_request_id(<card>, <action>, <address>, <key>)`.
PO_DELIVERY = "po"
PO_SUCCESSOR = "po-successor"
DEPENDENT_COMMENT = "dependent-comment"
DEPENDENT_BLOCKED = "dependent-blocked"
CARD_COMMENT = "card-comment"
TERMINAL_MOVE = "terminal"
WAIT_MALFORMED_ACTION = "wait-malformed-blocked"
#: The fields of a run the wait reads, and the one status that ends it.
_RUN_JQ = "{status, conclusion, html_url, created_at, updated_at, run_started_at}"
_RUN_COMPLETED = "completed"
#: The cards a wait's outcome can still reach: every column but Done.
_DEPENDENT_STATES = {"issues", "ready", "in_progress", "validate", "assessment", "blocked"}


def utcnow() -> datetime:
    """The dispatcher's clock for waits; tests replace it."""
    return datetime.now(UTC)


class _Unreachable(Exception):
    """The source answered that the target does not exist or is not accessible."""


class _Transient(Exception):
    """The source did not answer this time (network, 5xx, rate limit): retried next tick."""


def delivery_request_id(reference: str, action: str, address: str, key: str) -> str:
    """The request id of one delivery: the card, the action, the address and the frozen result."""
    return "-".join(request_token(part) for part in ("dispatcher", "wait", action, reference, address, key))


def pending_wait_blockers(
    runtime: Any, task: dict[str, Any], cache: dict[str, Any] | None = None
) -> list[str]:
    """The wait cards named by this card's `blocked_by` that are still waiting (Ready or In progress).

    Only wait-card blockers hold a card here; a blocker of any other kind, or one that cannot be
    read, is left to the claim's own predecessor rule, as before.
    """
    cache = {} if cache is None else cache
    pending: list[str] = []
    for ref in _blocker_refs(task):
        if ref not in cache:
            try:
                cache[ref] = runtime.reader.show(ref)
            except TaskError:
                cache[ref] = None
        blocker = cache[ref]
        if blocker is not None and is_wait(blocker) and blocker.get("state") in {"ready", "in_progress"}:
            pending.append(ref)
    return pending


def _blocker_refs(task: dict[str, Any]) -> list[str]:
    return [part.strip() for part in str(task.get("blocked_by") or "").split(",") if part.strip()]


def claim_wait_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Claim a Ready wait card; no workspace, no head, no preflight. It then advances in this tick."""
    ref = task["ref"]
    claim_id = _attempt_request_id(attempt_id, "claim", ref)
    if runtime.audit.committed_event(claim_id) is not None:
        # A wait card back in Ready (moved by hand) is claimed again under a new attempt.
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
    return advance_wait_card(runtime, runtime.reader.show(ref), records, payload, attempt_id)


def advance_wait_card(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """One tick of a wait card, whatever column it stands in. It keeps no dispatcher record."""
    ref = task["ref"]
    column = str(task.get("state") or "")
    if column == "ready":
        return claim_wait_card(runtime, task, records, payload, attempt_id)
    if records.pop(ref, None) is not None:
        runtime.save_records(payload, records)
    if column != "in_progress":
        return _outcome(ref, attempt_id, "wait-closed", state=column, wait=wait_card.wait_view(task))
    spec = wait_card.wait_spec(task)
    if spec is None:
        reason = "the wait card carries no well-formed wait spec, so there is nothing to wait for"
        runtime.writer.move(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            target="blocked",
            reason=reason,
            request_id=_attempt_request_id(attempt_id, WAIT_MALFORMED_ACTION, ref),
            terminal_taxonomy=normalize_terminal_taxonomy(
                disposition="blocked", blocked_reason="other"
            ).to_record(),
        )
        return {**_outcome(ref, attempt_id, WAIT_MALFORMED_ACTION, reason=reason), "status": "blocked"}
    now = utcnow()
    known = wait_card.wait_state(task)
    state = WaitState.from_json(known.to_json())
    if not state.since:
        state.since = utc_text(now)
    if state.result is None:
        _observe(runtime, task, spec, state, now)
    if state.text() != known.text():
        _record(runtime, ref, state)
    if state.result is None:
        return _outcome(
            ref,
            attempt_id,
            "wait-waiting",
            observation=state.observation,
            error=state.error,
            deadline=spec.deadline,
        )
    return _deliver(runtime, task, spec, state, attempt_id)


def _record(runtime: Any, ref: str, state: WaitState) -> None:
    runtime.writer.record_wait_state(
        role="dispatcher", actor=runtime.owner, reference=ref, state=state.text()
    )


# --- observation ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """A terminal fact one observation saw. Only `_settle` decides whether it is frozen."""

    outcome: str
    fact: dict[str, Any]
    summary: str
    evidence: str = ""
    # When the source itself says the target happened: a completed run's `updated_at`, the time of
    # the card's transition into the state it reached, the target time. The one proof that admits a
    # `target_reached` observed after the deadline.
    happened_at: str = ""


def _observe(runtime: Any, task: dict[str, Any], spec: WaitSpec, state: WaitState, now: datetime) -> None:
    """Observe the target once. Whatever it saw goes to `_settle`, the one place a result is frozen."""
    _settle(state, spec, _look(runtime, task, spec, state, now), now)


def _settle(state: WaitState, spec: WaitSpec, candidate: _Candidate | None, now: datetime) -> None:
    """Freeze the wait's result, or nothing: the one place a result is frozen and the deadline enforced.

    A frozen result stands. Before the deadline the candidate, if any, is frozen as it is. At or past
    the deadline the only outcome that can be frozen is `deadline_passed`, unless the candidate is
    `target_reached` and the source's own timestamp puts the target at or before the deadline: a
    cancel, a 404/410/403 or a transient window that ran out, observed after the deadline, is
    `deadline_passed`, never its own outcome.
    """
    if state.result is not None:
        return
    deadline = parse_utc(spec.deadline, "deadline")
    if now < deadline:
        if candidate is not None:
            _freeze(state, candidate, now)
        return
    if (
        candidate is not None
        and candidate.outcome == TARGET_REACHED
        and _not_after(candidate.happened_at, deadline)
    ):
        _freeze(state, candidate, now)
        return
    last = f"; last observation: {state.observation}" if state.observation else ""
    seen = f"; seen after it: {candidate.summary}" if candidate is not None else ""
    _freeze(
        state,
        _Candidate(
            DEADLINE_PASSED,
            {
                "deadline": spec.deadline,
                "last_observation": state.observation,
                "last_error": state.error,
                "seen_after_deadline": candidate.outcome if candidate is not None else "",
            },
            f"the deadline {spec.deadline} passed with no result{last}{seen}",
            spec.target.link,
        ),
        now,
    )


def _not_after(happened_at: str, deadline: datetime) -> bool:
    try:
        return bool(happened_at) and parse_utc(happened_at, "happened_at") <= deadline
    except WaitSpecError:
        return False


def _freeze(state: WaitState, candidate: _Candidate, now: datetime) -> None:
    result = {
        "outcome": candidate.outcome,
        "fact": candidate.fact,
        "summary": candidate.summary,
        "evidence": candidate.evidence,
        "frozen_at": utc_text(now),
    }
    result["key"] = wait_card.result_key(result)
    state.result = result


def _look(
    runtime: Any, task: dict[str, Any], spec: WaitSpec, state: WaitState, now: datetime
) -> _Candidate | None:
    """What one observation saw: a terminal candidate, or None while the target is still pending."""
    cancel = wait_card.wait_cancel(task)
    if cancel is not None:
        return _Candidate(
            CANCELLED,
            {
                "cancelled_at": cancel["at"],
                "by": cancel["by"],
                "role": cancel["role"],
                "reason": cancel["reason"],
            },
            f"cancelled by {cancel['role']} {cancel['by']} at {cancel['at']}: {cancel['reason']}",
        )
    target = spec.target
    try:
        if target.kind == TARGET_RUN:
            candidate = _observe_run(runtime, spec, state, now)
        elif target.kind == TARGET_CARD:
            candidate = _observe_card(runtime, spec, state, now)
        else:
            candidate = _observe_time(spec, state, now)
    except _Unreachable as exc:
        return _Candidate(
            SOURCE_UNREACHABLE, {"error": str(exc)}, f"the source is unreachable: {exc}", target.link
        )
    except _Transient as exc:
        text = str(exc)[:500]
        if not state.error_since:
            state.error_since = utc_text(now)
        if state.error != text:
            state.error, state.error_at = text, utc_text(now)
        window_end = parse_utc(state.error_since, "error_since") + timedelta(
            seconds=spec.transient_window_seconds
        )
        if now < window_end:
            return None
        return _Candidate(
            SOURCE_UNREACHABLE,
            {"error": text, "since": state.error_since, "window_seconds": spec.transient_window_seconds},
            f"the source did not answer from {state.error_since} for "
            f"{_duration(spec.transient_window_seconds)}: {text}",
            target.link,
        )
    state.error = state.error_since = state.error_at = ""
    return candidate


def _observed(state: WaitState, text: str, now: datetime) -> None:
    """The observation, stamped when the dispatcher first saw it (so an unchanged one writes nothing)."""
    if state.observation != text:
        state.observation, state.observed_at = text, utc_text(now)


def _observe_time(spec: WaitSpec, state: WaitState, now: datetime) -> _Candidate | None:
    at = spec.target.at
    if now < parse_utc(at, "at"):
        _observed(state, f"waiting for {at}", now)
        return None
    _observed(state, f"{at} arrived", now)
    return _Candidate(TARGET_REACHED, {"at": at}, f"the time {at} arrived", "", happened_at=at)


def _observe_run(runtime: Any, spec: WaitSpec, state: WaitState, now: datetime) -> _Candidate | None:
    target = spec.target
    path = f"repos/{target.repo}/actions/runs/{target.run_id}"
    try:
        run = _gh_api(runtime.host, path, jq=_RUN_JQ)
    except GateTransportError as exc:
        raise _Transient(f"GitHub did not answer GET {path}: {exc}") from None
    except HostError as exc:
        raise _classified(path, str(exc)) from None
    if not isinstance(run, dict) or not run.get("status"):
        raise _Transient(f"GitHub answered GET {path} with no run status")
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    if status != _RUN_COMPLETED:
        _observed(state, f"run {target.repo}#{target.run_id} is {status}", now)
        return None
    fact = {
        "status": status,
        "conclusion": conclusion,
        "html_url": str(run.get("html_url") or target.link),
        "created_at": str(run.get("created_at") or ""),
        "run_started_at": str(run.get("run_started_at") or ""),
        "updated_at": str(run.get("updated_at") or ""),
    }
    summary = f"run {target.repo}#{target.run_id} concluded {conclusion or 'with no conclusion'}"
    _observed(state, summary, now)
    # A completed run's `updated_at` is its completion time: nothing updates a run after it completed
    # but a rerun, which makes it not completed again.
    return _Candidate(TARGET_REACHED, fact, summary, fact["html_url"], happened_at=fact["updated_at"])


def _classified(path: str, message: str) -> Exception:
    """A GitHub answer that is not a run: definitive for 404/410 and no access, transient otherwise."""
    match = _HTTP_STATUS_RE.search(message)
    code = (match.group(1) or match.group(2)) if match else ""
    limited = "rate limit" in message.lower()
    if code in {"404", "410"} or (code == "403" and not limited):
        return _Unreachable(f"GitHub answered GET {path} with HTTP {code}: {message}")
    return _Transient(f"GitHub answered GET {path}: {message}")


def _observe_card(runtime: Any, spec: WaitSpec, state: WaitState, now: datetime) -> _Candidate | None:
    target = spec.target
    try:
        card = runtime.reader.show(target.ref)
    except TaskError as exc:
        if exc.code == "not_found":
            raise _Unreachable(f"card {target.ref} does not exist") from None
        raise _Transient(f"card {target.ref} could not be read: {exc.message}") from None
    column = str(card.get("state") or "")
    if column not in target.states:
        _observed(state, f"{target.ref} is {column}", now)
        return None
    summary = f"{target.ref} reached {column}"
    _observed(state, summary, now)
    entered = _entered_at(runtime, target.ref, column)
    return _Candidate(
        TARGET_REACHED,
        {"ref": target.ref, "state": column, "entered_at": entered},
        summary,
        "",
        happened_at=entered,
    )


def _entered_at(runtime: Any, reference: str, column: str) -> str:
    """When the card's audit says it last moved into `column`; `""` when the audit does not say."""
    try:
        events = runtime.audit.events(reference)
    except TaskError:
        return ""
    entered = ""
    for event in events:
        moved = recorded_card_transition(event)
        if moved is not None and moved[1] == column:
            entered = str(event.get("occurred_at") or "")
    return entered


def _duration(seconds: int) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m" if not rest else f"{seconds}s"


# --- delivery ------------------------------------------------------------------------------------


def _deliver(
    runtime: Any, task: dict[str, Any], spec: WaitSpec, state: WaitState, attempt_id: str
) -> dict[str, Any]:
    """Deliver the frozen result to every address still owed it; the terminal move comes last."""
    ref = task["ref"]
    result = state.result or {}
    postponed: list[str] = []
    for address in wait_card.pending_addresses(spec, state):
        received = ""
        if address == DEPENDENTS:
            status, detail = _deliver_dependents(runtime, task, result)
        elif wait_card.returned_card(address):
            status, detail = _deliver_card(runtime, task, address, result)
        else:
            status, detail, received = _deliver_po(runtime, task, spec, state, address)
        if status is None:
            postponed.append(f"{address}: {detail}")
            continue
        state.deliveries[address] = {
            "status": status,
            "at": utc_text(utcnow()),
            "detail": detail,
            # A PO address names the session that took the result, the addressed one or its successor.
            **({"session": received} if received else {}),
        }
        # Recorded only after the receiving side took it; a crash before this line repeats the
        # delivery under the same key, and the receiving side makes the repeat a no-op.
        _record(runtime, ref, state)
    if postponed:
        return {
            **_outcome(
                ref, attempt_id, "wait-delivery-postponed", outcome=result.get("outcome"), postponed=postponed
            ),
            "status": "degraded",
            "reason": "a return address did not take the wait's result; it is repeated next tick under the same "
            "key: " + "; ".join(postponed),
        }
    outcome = str(result.get("outcome") or "")
    reached = outcome == TARGET_REACHED
    record = render_outcome_record(ref, spec, state)
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="done" if reached else "blocked",
        reason=record,
        request_id=delivery_request_id(ref, TERMINAL_MOVE, wait_card.OBSERVER, str(result.get("key") or "")),
        **(
            {}
            if reached
            else {
                "terminal_taxonomy": normalize_terminal_taxonomy(
                    disposition="blocked", blocked_reason="other"
                ).to_record()
            }
        ),
        wait_outcome=outcome,
    )
    return _outcome(
        ref,
        attempt_id,
        "wait-target-reached" if reached else "wait-ended",
        outcome=outcome,
        summary=result.get("summary"),
        state="done" if reached else "blocked",
    )


def _deliver_po(
    runtime: Any, task: dict[str, Any], spec: WaitSpec, state: WaitState, address: str
) -> tuple[str | None, str, str]:
    """One input to the named PO session, or to its successor: `(status, detail, received_by)`.

    `(None, why, "")` postpones it to the next tick; nothing here gives a result up. The path is the
    one every dispatcher result takes to a PO session (`dispatch/po_delivery.py`): a session that is
    closed or missing gets one successor (`_successor`), recorded on the card before anything is
    submitted to it, and the input goes there under the same delivery request id.
    """
    ref = task["ref"]
    result = state.result or {}
    key = str(result.get("key") or "")
    session_id = (state.successors.get(address) or {}).get("session") or address[
        len(wait_card.PO_SESSION_PREFIX) :
    ]
    return deliver(
        runtime,
        session_id=session_id,
        text=render_po_input(ref, spec, result),
        request_id=delivery_request_id(ref, PO_DELIVERY, address, key),
        card=card_facts(
            card_ref=ref,
            kind=WAIT_KIND,
            touches_production=None,
            sprint_ref=str(task.get("sprint") or ""),
            input=WAIT_OUTCOME_INPUT,
        ),
        successor=lambda closed: _successor(runtime, task, state, address, closed, key),
    )


def _successor(
    runtime: Any, task: dict[str, Any], state: WaitState, address: str, closed: str, key: str
) -> tuple[str, str]:
    """The one session that succeeds `closed` for this delivery: `(session, "")`, or `("", why)`.

    `open_successor` routes it: the sprint's own session through `sprint_session`, any other through
    `create_session` with the closed session's CLI, model and effort. The route is recorded in
    `wait_state.successors` before the call and the session right after it, and the request id is
    derived from the delivery key and the closed session, so a repeat after a crash takes the same
    route and opens no second successor.
    """
    ref = task["ref"]
    record = state.successors.setdefault(address, {})
    session, why = open_successor(
        runtime,
        reference=ref,
        sprint_ref=str(task.get("sprint") or ""),
        closed=closed,
        record=record,
        persist=lambda: _record(runtime, ref, state),
        request_id=delivery_request_id(ref, PO_SUCCESSOR, closed, key),
    )
    if not record:
        # No route was recorded (the sprint could not be read): nothing to keep.
        state.successors.pop(address, None)
    return session, why


def _deliver_dependents(runtime: Any, task: dict[str, Any], result: dict[str, Any]) -> tuple[str | None, str]:
    """One comment on each card held by this wait; on another outcome, a Ready one is Blocked too."""
    ref = task["ref"]
    key = str(result.get("key") or "")
    outcome = str(result.get("outcome") or "")
    try:
        cards = select_cards(runtime.reader, states=set(_DEPENDENT_STATES))
        dependents = [card for card in cards if ref in _blocker_refs(card) and card.get("ref") != ref]
        for dependent in sorted(dependents, key=lambda card: str(card.get("ref") or "")):
            other = str(dependent["ref"])
            dependent = runtime.reader.show(other)
            if ref not in _blocker_refs(dependent) or dependent.get("state") not in _DEPENDENT_STATES:
                continue
            body = render_dependent_comment(ref, result)
            runtime.writer.comment(
                role="dispatcher",
                actor=runtime.owner,
                reference=other,
                body=body,
                request_id=delivery_request_id(ref, DEPENDENT_COMMENT, other, key),
            )
            if outcome != TARGET_REACHED and dependent.get("state") == "ready":
                runtime.writer.move(
                    role="dispatcher",
                    actor=runtime.owner,
                    reference=other,
                    target="blocked",
                    reason=f"blocked by wait card {ref}, which ended {outcome}: {result.get('summary') or ''}",
                    request_id=delivery_request_id(ref, DEPENDENT_BLOCKED, other, key),
                    terminal_taxonomy=normalize_terminal_taxonomy(
                        disposition="blocked", blocked_reason="other"
                    ).to_record(),
                    wait_outcome=outcome,
                )
    except TaskError as exc:
        return None, f"{exc.code}: {exc.message}"
    return ACCEPTED, ", ".join(str(card["ref"]) for card in dependents) or "(none)"


def _deliver_card(
    runtime: Any, task: dict[str, Any], address: str, result: dict[str, Any]
) -> tuple[str | None, str]:
    """One comment on the one card the address names; that card's own stage consumes the result."""
    ref = task["ref"]
    other = wait_card.returned_card(address)
    try:
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=other,
            body=render_card_comment(ref, result),
            request_id=delivery_request_id(ref, CARD_COMMENT, address, str(result.get("key") or "")),
        )
    except TaskError as exc:
        if exc.code == "not_found":
            return ACCEPTED, f"{other} does not exist; nothing took the result"
        return None, f"{exc.code}: {exc.message}"
    return ACCEPTED, other


def _result_lines(spec: WaitSpec, result: dict[str, Any]) -> list[str]:
    lines = [
        f"Target: {spec.target.describe()}",
        f"Outcome: {result.get('outcome')}",
        f"Result: {result.get('summary') or ''}",
    ]
    fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
    if spec.target.kind == TARGET_RUN and result.get("outcome") == TARGET_REACHED:
        lines.append(f"Conclusion: {fact.get('conclusion') or 'none'}")
        for name in ("created_at", "run_started_at", "updated_at"):
            if fact.get(name):
                lines.append(f"{name}: {fact[name]}")
    if result.get("evidence"):
        lines.append(f"Evidence: {result['evidence']}")
    return lines


def render_po_input(reference: str, spec: WaitSpec, result: dict[str, Any]) -> str:
    """The one input a wait's result becomes in a PO session; the same result always renders the same."""
    lines = [
        f"Wait card {reference} ended: {result.get('outcome')}. You named this session as a return address.",
        "",
        f"Card: {reference}",
        *_result_lines(spec, result),
        "",
        "Nothing else is sent for this wait. Decide what follows from it in this turn, or cut a card for it.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_dependent_comment(reference: str, result: dict[str, Any]) -> str:
    outcome = str(result.get("outcome") or "")
    follows = (
        "This card is claimable now."
        if outcome == TARGET_REACHED
        else "This card is Blocked with that outcome as its reason."
    )
    lines = [
        f"[wait:{outcome}] {reference}",
        "",
        f"The wait card this card is blocked by ended {outcome}: {result.get('summary') or ''}",
    ]
    if result.get("evidence"):
        lines.append(f"Evidence: {result['evidence']}")
    return "\n".join([*lines, "", follows]) + "\n"


def render_card_comment(reference: str, result: dict[str, Any]) -> str:
    """The comment a `card:<ref>` address gets: the result, for the card's own stage to act on."""
    lines = [
        f"[wait:{result.get('outcome')}] {reference}",
        "",
        (
            f"The wait card {reference} this card named as its e2e wait ended {result.get('outcome')}: "
            f"{result.get('summary') or ''}"
        ),
    ]
    if result.get("evidence"):
        lines.append(f"Evidence: {result['evidence']}")
    return "\n".join([*lines, "", "The dispatcher's e2e stage acts on this result on this card."]) + "\n"


def render_outcome_record(reference: str, spec: WaitSpec, state: WaitState) -> str:
    """The wait card's completion comment, carried by its terminal move."""
    result = state.result or {}
    lines = [f"[wait:{result.get('outcome')}]", "", *_result_lines(spec, result), ""]
    lines.append("Delivered:")
    for address in spec.returns:
        if address == wait_card.OBSERVER:
            lines.append(f"- {address}: this comment and the card's terminal column")
            continue
        record = state.deliveries.get(address) or {}
        taken = f", taken by PO session {record['session']}" if record.get("session") else ""
        lines.append(
            f"- {address}: {record.get('status') or 'pending'} ({record.get('detail') or ''}{taken})"
        )
    return "\n".join(lines).rstrip() + "\n"


def _outcome(ref: str, attempt_id: str, action: str, **fields: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "step": WAIT_STEP,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
        **fields,
    }


__all__ = [
    "CARD_COMMENT",
    "DEPENDENT_BLOCKED",
    "DEPENDENT_COMMENT",
    "PO_DELIVERY",
    "TERMINAL_MOVE",
    "WAIT_STEP",
    "advance_wait_card",
    "claim_wait_card",
    "delivery_request_id",
    "pending_wait_blockers",
    "render_card_comment",
    "render_dependent_comment",
    "render_outcome_record",
    "render_po_input",
    "utcnow",
]
