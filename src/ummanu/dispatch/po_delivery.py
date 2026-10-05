"""One dispatcher input to a PO session, or to the session that succeeds it (secretary-1790, secretary-1792).

The one path a result reaches a PO session by: a wait card's outcome (`dispatch/wait_cards.py`), a
delegated card's result returned to its origin (`dispatch/origin_returns.py`), and the successor an
out-of-sprint decision/operation card is submitted to when its origin session is closed
(`dispatch/po_cards.py`).

- The input goes through `PoService.submit`, `source: dispatcher`, under a request id the caller
  derives from what is delivered; the service deduplicates it, and a `request_conflict` on an id the
  service already holds for that session (a turn, or an input still queued) is the earlier submit.
- A session that is closed or missing gets one successor (:func:`open_successor`) and the input goes
  there under the same request id: a closed session reserves nothing, so the id is still free. The
  sprint's own recorded session is succeeded through `sprint_session`, any other through
  `create_session` with the closed session's CLI, model and effort (`PoChannel.successor_choice`).
  The route is written to the caller's record before the call and the successor's id right after,
  under a request id the caller derives, so a repeat after a crash opens no second successor.
- Anything else the service answers, or its silence, postpones the delivery: nothing here gives a
  result up.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ummanu.po.client import PoServiceError, ServiceRefused
from ummanu.po.store import PoRequest, PoStoreError, RequestConflict, SessionClosed, SessionNotFound
from ummanu.tasks import TaskError

#: The source the PO service records for a dispatcher input (`ummanu.po.queue.SOURCES`).
DISPATCHER_SOURCE = "dispatcher"
#: The only status a delivery is recorded with: the receiving side took it.
ACCEPTED = "accepted"
#: How a closed or missing session is succeeded: the sprint's resolver, or a plain new session.
SPRINT_ROUTE = "sprint_session"
CREATE_ROUTE = "create_session"
#: Successors one delivery may open in one tick when each is closed again before it takes the input.
SUCCESSORS_PER_TICK = 2


def already_submitted(runtime: Any, request_id: str, session_id: str) -> dict[str, Any] | None:
    """What a request id this record owns already is at the service, or None when it is not ours.

    A turn in `po_requests`, or an input still pending in the queue for the same session: both are
    the input this record submitted before it lost its answer, whatever the text says now.
    """
    try:
        known: PoRequest | None = runtime.po.request(request_id)
        if known is not None:
            return {"seq": known.seq, "queued": known.seq is None}
        queued = runtime.po.queued(request_id)
    except PoStoreError:
        return None
    if queued is not None and queued.session_id == session_id:
        return {"seq": None, "queued": True}
    return None


def deliver(
    runtime: Any,
    *,
    session_id: str,
    text: str,
    request_id: str,
    card: dict[str, Any],
    successor: Callable[[str], tuple[str, str]],
) -> tuple[str | None, str, str]:
    """One input to `session_id`, or to its successor: `(status, detail, received_by)`.

    `(None, why, "")` postpones it to the next tick. `successor(closed)` answers `(session, "")` with
    the session that succeeds a closed or missing one, or `("", why)` when it cannot open one yet.
    """
    for _ in range(SUCCESSORS_PER_TICK + 1):
        try:
            runtime.po.submit(
                session_id=session_id, text=text, request_id=request_id, source=DISPATCHER_SOURCE, card=card
            )
        except (SessionClosed, SessionNotFound):
            following, why = successor(session_id)
            if not following:
                return None, why, ""
            session_id = following
            continue
        except RequestConflict as exc:
            # This key already carries an input: the one an earlier tick submitted before it lost the answer.
            if already_submitted(runtime, request_id, session_id) is not None:
                return ACCEPTED, request_id, session_id
            return None, f"request id {request_id} is bound to another input: {exc}", ""
        except ServiceRefused as exc:
            return None, f"the PO service refused it ({exc.code}): {exc}", ""
        except (PoServiceError, PoStoreError) as exc:
            return None, f"{type(exc).__name__}: {exc}", ""
        return ACCEPTED, request_id, session_id
    return None, f"every successor of the session opened this tick was closed again (last {session_id})", ""


def is_sprint_session(runtime: Any, sprint_ref: str, session_id: str) -> bool:
    """Whether `session_id` is the recorded PO session of the open sprint `sprint_ref`."""
    if not sprint_ref:
        return False
    sprint = runtime.sprints.show(sprint_ref, include_cards=False)
    return sprint.get("status") == "open" and str(sprint.get("po_session") or "") == session_id


def open_successor(
    runtime: Any,
    *,
    reference: str,
    sprint_ref: str,
    closed: str,
    record: dict[str, str],
    persist: Callable[[], None],
    request_id: str,
    freeze_choice: bool = False,
) -> tuple[str, str]:
    """The one session that succeeds `closed`: `(session, "")`, or `("", why)`.

    `record` is the caller's durable record of this succession, `{replaces, via, session}`, and
    `persist` writes it: the route before the call, the session right after it. A record that already
    replaces `closed` keeps its route, so a repeat after a crash takes the same one; the service
    answers the repeated `request_id` with the session it already opened.
    """
    if record.get("replaces") != closed:
        try:
            via = SPRINT_ROUTE if is_sprint_session(runtime, sprint_ref, closed) else CREATE_ROUTE
        except TaskError as exc:
            return (
                "",
                f"the sprint of {reference} cannot be read to route the successor of {closed}: {exc.message}",
            )
        record.clear()
        record.update({"replaces": closed, "via": via, "session": ""})
        persist()
    try:
        if record["via"] == SPRINT_ROUTE:
            answer = runtime.po.sprint_session(sprint_ref=sprint_ref, request_id=request_id)
        else:
            choice = ((record["cli"], record["model"], record["effort"])
                      if all(record.get(key) for key in ("cli", "model", "effort"))
                      else runtime.po.successor_choice(closed))
            if choice is None:
                return (
                    "",
                    f"PO session {closed} is closed and this installation offers no model for a successor",
                )
            cli, model, effort = choice
            if freeze_choice and not record.get("cli"):
                record.update({"cli": cli, "model": model, "effort": effort})
                persist()
                cli, model, effort = record["cli"], record["model"], record["effort"]
            answer = runtime.po.create_session(cli=cli, model=model, effort=effort, request_id=request_id)
    except (PoServiceError, PoStoreError) as exc:
        return "", f"the successor of closed PO session {closed} is not open yet: {type(exc).__name__}: {exc}"
    session = str(answer.get("session_id") or "")
    if not session:
        return "", f"the PO service named no successor of {closed}"
    record["session"] = session
    persist()
    return session, ""


__all__ = [
    "ACCEPTED",
    "CREATE_ROUTE",
    "DISPATCHER_SOURCE",
    "SPRINT_ROUTE",
    "SUCCESSORS_PER_TICK",
    "already_submitted",
    "deliver",
    "is_sprint_session",
    "open_successor",
]
