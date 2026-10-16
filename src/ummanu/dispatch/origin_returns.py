"""Delegated cards: a card returns its result to the PO session that cut it (secretary-1792).

A card created inside a PO turn carries its origin (`board/po_origin.py`). Each of its transitions into
Done or Blocked owes that session one input, through the path every
dispatcher result takes to a PO session (`dispatch/po_delivery.py`): a closed or missing origin session
gets a successor, a service that does not answer postpones the delivery to the next tick, and nothing is
given up.

What is owed is written down when it becomes owed. The store writes one row of the origin-return
outbox (`board/origin_outbox.py`, table `origin_returns`) in the transaction that commits the
transition's audit event (`SqlTaskAudit.append`): the move, its event and the obligation stand or fall
together, whoever made the move, and archive, reopen or any later move touch no row. Nothing here scans
cards or infers from a card's present column what it owes.

:func:`pending_returns` is the one rule and the only thing that decides what is returned: the
undelivered outbox rows, oldest first, one indexed query per tick. Each row is made on its own: its
event is fetched from the card's audit by its event id (an archived card's audit included) and rendered
from that event (the column it entered, when, its result), never from the card's present state; then
the submit (idempotent by request id) and, only when the session took it, the row is marked delivered.
The row is the only delivery record. The first row of a card that does not complete stops that card
for the tick, and its later rows wait; other cards go on. A crash between the submit and the mark
repeats the submit next tick under the same request id, a no-op for the service. A marked row is never
selected again.

Two exceptions. A wait card owes no row (the store does not write one): it delivers through its own
return addresses. A Done whose completion transition records the PO session that ran `task complete`
(`po_session`, from that turn's `UMMANU_PO_SESSION`), where that session is the origin or one of its
recorded successors, was completed in a turn of the origin's line, which already has the result: its row
is marked `skipped`, with no input. Anything else is delivered: a completion by another
session, one that ran outside a PO turn, any other Done, and every Blocked. The session the dispatcher
handed the card to (`po_return.executor`) proves nothing and is not asked.
"""

from __future__ import annotations

import re
from typing import Any

from ummanu.board import po_origin as origin_field
from ummanu.board.origin_outbox import DELIVERED, SKIPPED, OutboxRow, outbox_for
from ummanu.board.production_rights import DELEGATED_RESULT_INPUT, card_facts
from ummanu.dispatch.po_delivery import deliver, open_successor
from ummanu.dispatch.state import request_token
from ummanu.kernel.errors import TaskError
from ummanu.tasks import PO_SESSION_KEY, recorded_card_transition

STEP = "origin-return"
#: Request-id actions: `dispatcher-origin-return-<card>-<event id>` for the input, and
#: `dispatcher-origin-successor-<card>-<closed session>` for a successor of the origin's line.
RETURN_ACTION = "origin-return"
SUCCESSOR_ACTION = "origin-successor"
#: The longest result an input quotes; the rest is on the card.
RESULT_LIMIT = 6000
_LINK_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull/\d+|actions/runs/\d+(?:/job/\d+)?)")
_REPORT_MARKERS = ("report:done", "report:blocked", "report:partial")


def return_request_id(reference: str, event_id: str) -> str:
    """The input's request id: the card and the terminal transition's event, nothing else."""
    return "-".join(request_token(part) for part in ("dispatcher", RETURN_ACTION, reference, event_id))


def successor_request_id(reference: str, closed: str) -> str:
    """The request id that opens the successor of `closed` in this card's origin line."""
    return "-".join(request_token(part) for part in ("dispatcher", SUCCESSOR_ACTION, reference, closed))


def record_return_state(runtime: Any, reference: str, state: origin_field.ReturnState) -> None:
    runtime.writer.record_po_return(
        role="dispatcher", actor=runtime.owner, reference=reference, state=state.text()
    )


def succeed_origin(
    runtime: Any, task: dict[str, Any], origin: dict[str, str], state: origin_field.ReturnState, closed: str
) -> tuple[str, str]:
    """The session that succeeds `closed` in the card's origin line: `(session, "")`, or `("", why)`.

    One successor per closed session, recorded in `po_return.successors` (route before the call, id
    right after), shared by the out-of-sprint execution of a decision/operation card and by every
    result returned to the origin, so neither opens a second one.
    """
    ref = task["ref"]
    record = state.successors.setdefault(closed, {})
    session, why = open_successor(
        runtime,
        reference=ref,
        sprint_ref=str(task.get("sprint") or ""),
        closed=closed,
        record=record,
        persist=lambda: record_return_state(runtime, ref, state),
        request_id=successor_request_id(ref, closed),
    )
    if not record:
        state.successors.pop(closed, None)
    return session, why


def event_key(event: dict[str, Any]) -> str:
    """The key of one audited transition: its event id (its request id for a record that names none)."""
    return str(event.get("event_id") or event.get("request_id") or "")


def pending_returns(outbox: Any) -> list[OutboxRow]:
    """The undelivered outbox rows, oldest first: the one rule, and the only thing that decides what is returned."""
    return list(outbox.pending())


def reconcile_origin_returns(runtime: Any) -> list[dict[str, Any]]:
    """Make every owed origin return, oldest first; one tick's pass over the outbox, with no card scan."""
    outbox = outbox_for(getattr(runtime.reader, "client", None))
    if outbox is None:
        return []
    outcomes: list[dict[str, Any]] = []
    stopped: set[str] = set()
    cards: dict[str, tuple[dict[str, Any], origin_field.ReturnState, list[dict[str, Any]]]] = {}
    for row in pending_returns(outbox):
        if row.task_ref in stopped:
            continue
        try:
            outcome = _return_row(runtime, outbox, row, cards)
        except TaskError as exc:
            outcome = _outcome(
                row.task_ref,
                "origin-return-unread",
                status="degraded",
                event_id=row.event_id,
                reason=f"{exc.code}: {exc.message}",
            )
        outcomes.append(outcome)
        if outcome["status"] != "ok":
            # The card's later rows wait for the next tick; none is made ahead of this one.
            stopped.add(row.task_ref)
    return outcomes


def _return_row(
    runtime: Any,
    outbox: Any,
    row: OutboxRow,
    cards: dict[str, tuple[dict[str, Any], origin_field.ReturnState, list[dict[str, Any]]]],
) -> dict[str, Any]:
    """One owed return: the skip, or the submit and the mark, in that order."""
    ref, key, column = row.task_ref, row.event_id, row.target_state
    if ref not in cards:
        # Read once a tick per card: `show` finds an archived card, `events` its whole audit.
        task = runtime.reader.show(ref)
        cards[ref] = (task, origin_field.return_state(task), runtime.audit.events(ref))
    task, state, events = cards[ref]
    origin = origin_field.po_origin(task)
    index = next((position for position, event in enumerate(events) if event_key(event) == key), None)
    if origin is None or index is None:
        return _outcome(
            ref,
            "origin-return-unreadable",
            status="degraded",
            state=column,
            event_id=key,
            reason="the card carries no origin"
            if origin is None
            else "the card's audit does not hold the event",
        )
    terminal = events[index]
    # Evaluated on this event: the PO session whose turn ran `task complete`, as the event records it.
    completer = str(_data(terminal).get(PO_SESSION_KEY) or "") if column == "done" else ""
    if completer and origin_field.in_line(completer, origin["session"], state):
        outbox.mark(row.id, status=SKIPPED, session=completer, po_request_id="")
        return _outcome(ref, "origin-return-skipped", state=column, event_id=key, session=completer)
    request_id = return_request_id(ref, key)
    status, detail, received = deliver(
        runtime,
        session_id=origin_field.line_head(origin["session"], state),
        text=render_origin_input(task, origin, events, index),
        request_id=request_id,
        card=card_facts(
            card_ref=ref,
            kind=str(task.get("type") or ""),
            touches_production=None,
            sprint_ref=str(task.get("sprint") or ""),
            input=DELEGATED_RESULT_INPUT,
        ),
        successor=lambda closed: succeed_origin(runtime, task, origin, state, closed),
    )
    if status is None:
        return _outcome(
            ref,
            "origin-return-postponed",
            status="degraded",
            state=column,
            event_id=key,
            reason="the origin PO session did not take the card's result; it is repeated next tick under "
            f"the same request id: {detail}",
        )
    # Marked only after the session took it: a crash before this line repeats the submit under the
    # same request id, which the service makes a no-op.
    outbox.mark(row.id, status=DELIVERED, session=received, po_request_id=request_id)
    return _outcome(
        ref, "origin-returned", state=column, event_id=key, session=received, po_request_id=request_id
    )


def _data(event: dict[str, Any]) -> dict[str, Any]:
    for name in ("data", "payload"):
        value = event.get(name)
        if isinstance(value, dict):
            return value
    return {}


def _round_report(events: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    """The worker's last report before the terminal event, in the round the card last entered In progress."""
    report = None
    for event in events[:index]:
        moved = recorded_card_transition(event)
        if moved is not None and moved[1] == "in_progress":
            report = None
            continue
        if str(_data(event).get("marker") or "") in _REPORT_MARKERS:
            report = _data(event)
    return report


def _bounded(text: str) -> str:
    text = text.strip()
    if len(text) <= RESULT_LIMIT:
        return text
    return text[:RESULT_LIMIT].rstrip() + "\n\n(cut here; the whole text is on the card: `task show`)"


def event_column(event: dict[str, Any]) -> str:
    """The column an audited transition entered, `""` for a record that is no transition."""
    moved = recorded_card_transition(event)
    return moved[1] if moved is not None else ""


def _result_lines(events: list[dict[str, Any]], index: int) -> list[str]:
    """The result of the transition at `index`, from that transition and the audit before it only.

    From the transition's own data where it carries one: a completion record (`[completion:...]`),
    the decision it names, the Blocked reason and classification. Otherwise, for a Done, the worker's
    last done report before it in its round.
    """
    terminal = events[index]
    reason = str(terminal.get("reason") or "").strip()
    report = _round_report(events, index)
    decision = str(_data(terminal).get("decision") or "").strip()
    decided = [f"Decision: {decision}", ""] if decision else []
    if event_column(terminal) == "done":
        if reason.startswith("[completion:"):
            return [*decided, "## Completion record", "", _bounded(reason)]
        if (
            report is not None
            and report.get("marker") == "report:done"
            and str(report.get("body") or "").strip()
        ):
            return [*decided, "## The worker's done report", "", _bounded(str(report["body"]))]
        return [*decided, "## Completion record", "", _bounded(reason) or "(the Done move carries no record)"]
    classification = ""
    if report is not None and report.get("marker") == "report:blocked":
        classification = str(report.get("classification") or "")
    if not classification:
        taxonomy = _data(terminal).get("terminal_taxonomy")
        blocked = taxonomy.get("blocked_reason") if isinstance(taxonomy, dict) else None
        classification = f"board: {blocked}" if blocked else "unclassified"
    lines = ["## Why it is Blocked", "", _bounded(reason) or "(the Blocked move carries no reason)", ""]
    lines.append(f"Classification: {classification}")
    body = str(report.get("body") or "").strip() if report is not None else ""
    kind = str(report.get("marker") or "").removeprefix("report:") if report is not None else ""
    if kind in {"blocked", "partial"} and body and body != reason:
        lines += ["", f"## The worker's {kind} report", "", _bounded(body)]
    return lines


def _links(events: list[dict[str, Any]], index: int) -> list[str]:
    terminal = events[index]
    texts = [str(event.get("reason") or "") for event in events[: index + 1]]
    texts += [str(_data(event).get("body") or "") for event in events[: index + 1]]
    links = list(dict.fromkeys(match for text in texts for match in _LINK_RE.findall(text)))
    merge = _data(terminal).get("release_merge")
    lines = [f"- {link}" for link in links]
    if isinstance(merge, dict) and merge.get("merge_sha"):
        lines.append(f"- merged {merge['merge_sha']} onto {merge.get('base') or 'the base'}")
    return lines


def render_origin_input(
    task: dict[str, Any], origin: dict[str, str], events: list[dict[str, Any]], index: int
) -> str:
    """The one input a delegated card's terminal transition becomes in its origin session.

    Built from that transition and the audit before it, plus the card's identity (ref, kind, title,
    sprint), never from the card's present column: a card already reopened still returns the Done or
    Blocked it had. The same transition always renders the same text.
    """
    ref = str(task.get("ref") or "")
    kind = str(task.get("type") or "")
    settled = "Done" if event_column(events[index]) == "done" else "Blocked"
    request = f" (answering request {origin['request']})" if origin.get("request") else ""
    lines = [
        (
            f"Card {ref} ({kind}), which PO session {origin['session']} cut{request}, settled {settled}. "
            "This is its result, sent to this session once for this transition."
        ),
        "",
        f"Card: {ref} ({kind}): {task.get('title') or ''}",
        f"State: {settled}, at {events[index].get('occurred_at') or 'an unrecorded time'}",
        f"Sprint: {task.get('sprint') or 'none'}",
        "",
        *_result_lines(events, index),
    ]
    links = _links(events, index)
    if links:
        lines += ["", "## Links", "", *links]
    lines += [
        "",
        (
            "Decide what follows from it in this turn: answer the owner, or cut the next card. Keep the "
            "turn short; anything long-running becomes a card."
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _outcome(ref: str, action: str, *, status: str = "ok", **fields: Any) -> dict[str, Any]:
    return {"status": status, "step": STEP, "pilot_ref": ref, "action": action, **fields}


__all__ = [
    "RETURN_ACTION",
    "STEP",
    "SUCCESSOR_ACTION",
    "event_column",
    "event_key",
    "pending_returns",
    "reconcile_origin_returns",
    "record_return_state",
    "render_origin_input",
    "return_request_id",
    "succeed_origin",
    "successor_request_id",
]
