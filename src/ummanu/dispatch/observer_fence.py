"""The pre-advance observer fence: a sprint's cards stop before they move, not after.

A declared observer is load-bearing, so this runs before records are reconciled or cards
advanced, and it is pure: it reads the sprint board, the head registry and the observer records,
decides which sprints cannot currently be observed, and returns that set. It launches nothing,
probes nothing and mutates no card.

What a fence excludes is the affected sprint's own work: its reservations, the projects of its
linked cards, and the cards themselves. A card with no sprint at all is nobody's to fence.

`{kind: none}` passes: a sprint that declares no observer is not a sprint whose observer is
missing.

A sprint board that cannot be read is not a healthy sprint. The two boards are separate board
reads and fail separately, so the fence keeps a durable snapshot of each open sprint's
reservations and falls back to it rather than letting a blind tick advance those cards.

The fence clears on confirmed adoption, and confirmed means a record for that sprint naming
exactly the declared profile, with a pid on disk that is alive. A pid that has not been written
yet is not adoption — the lifecycle grace window exists to decide whether to relaunch a head, not
to release another role's cards.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ummanu.board.tick_snapshot import select_cards, select_sprints
from ummanu.dispatch.observer import (
    DRAIN_DEFERRED_REASON,
    ObserverRecord,
    commit_event,
    load_observers,
    observer_alive,
    observer_decision,
    stage_event,
)
from ummanu.dispatch.state import now_rfc3339
from ummanu.dispatch.types import HostError
from ummanu.sprint_observer import KIND_NONE, ObserverMetadataError
from ummanu.tasks import TaskError

EVENT_FENCED = "observer_fence_raised"
EVENT_CLEARED = "observer_fence_cleared"

FENCE_STATE = "observer_fence"
# Each open sprint's reserved projects as of the last pass that could read the sprint board. It is
# what a blind pass fences from, so an unreadable declaration stops the same work a dead observer
# would rather than waving it through.
FENCE_SNAPSHOT = "observer_fence_snapshot"
# How many fence episodes each sprint has had. It is the identity of the current episode, and the
# only reason it exists is that two episodes of one sprint must not share a request id: the audit
# dedupes on that id, so a second fence raised for the same reason used to be swallowed into the
# first whenever both fell in the same wall-clock second — losing the record of a sprint that could
# not be observed, and making the test that checks a reappeared sprint's second lifecycle depend on
# where a second boundary landed. A counter answers it without a clock, and it is the whole answer:
# it advances only with the durable state, so a tick that dies after committing the event and before
# saving its state mints the same id again on the retry and is deduped on purpose. That retry is the
# one case the id is load-bearing for, and it is why no timestamp may enter the id — a clock reading
# taken again on the retry is a *different* id exactly when the two passes straddle a second.
FENCE_EPISODES = "observer_fence_episodes"

# Why the sprint cannot be observed right now. `observer_*` reasons come from the metadata itself
# and are corruption; the rest describe a declared head that is not there.
REASON_NO_RECORD = "observer_not_launched"
# The head was launched but has not written its pid, so nothing has confirmed it came up. Distinct
# from a dead pid: the repair is to wait a tick, not to look at a head that failed.
REASON_NOT_ADOPTED = "observer_not_adopted"
REASON_DEAD = "observer_head_dead"
REASON_MISMATCH = "observer_head_mismatch"
REASON_DEFERRED = "observer_launch_deferred"
REASON_ABANDONED = "observer_handle_abandoned"
REASON_BOARD_UNAVAILABLE = "sprint_board_unavailable"

# A pause claims nothing new and launches no observer, so under one a launch that is merely waiting
# for the resume is not a failure: the sprint stays fenced, with the outcome status `deferred`
# instead of `critical`. A launch that was due and failed is still critical.
PAUSED_LAUNCH_MODES = frozenset({"drain", "freeze"})
STATUS_DEFERRED = "deferred"


def observer_fence(runtime: Any, payload: dict[str, Any], *, pause_mode: str = "") -> dict[str, Any]:
    """Decide which sprints are fenced this tick, before any card is touched.

    Returns the fenced sprint refs, the projects those sprints hold, the card refs to leave alone,
    and the outcomes to report.

    A sprint board that cannot be read fences the sprints it last saw: the tick can perfectly well
    read the cards of a sprint whose declaration it cannot read, and advancing those cards is exactly
    "mutating a card whose declared observer is unavailable". Every successful pass records each open
    sprint's reservations in the durable production state, so the blind tick fences the same projects
    the last sighted one would have.
    """
    snapshot = _snapshot(payload)
    try:
        open_sprints = {
            str(sprint.get("ref") or ""): sprint
            for sprint in select_sprints(runtime.sprints, statuses={"open"})
            if str(sprint.get("ref") or "")
        }
    except (TaskError, HostError) as exc:
        return _blind_fence(runtime, payload, snapshot, getattr(exc, "message", str(exc)))
    observers = load_observers(payload)
    state = payload.get(FENCE_STATE)
    state = dict(state) if isinstance(state, dict) else {}

    fenced: dict[str, dict[str, Any]] = {}
    outcomes: list[dict[str, Any]] = []
    for ref in sorted(open_sprints):
        verdict = _sprint_verdict(runtime, open_sprints[ref], observers.get(ref), pause_mode=pause_mode)
        if verdict is None:
            outcomes.extend(_clear(runtime, state, ref))
            continue
        fenced[ref] = verdict
        outcomes.append(_raise(runtime, payload, state, open_sprints[ref], verdict))

    for ref in sorted(set(state) - set(fenced)):
        # A sprint that closed or vanished while fenced: the fence has nothing left to hold.
        outcomes.extend(_clear(runtime, state, ref))

    if state:
        payload[FENCE_STATE] = state
    else:
        payload.pop(FENCE_STATE, None)

    # Refreshed on every pass that could read the board, so the next one that cannot has something
    # true to fall back on rather than a guess.
    _put_snapshot(payload, open_sprints)

    projects: set[str] = set()
    for ref in fenced:
        projects |= _sprint_projects(open_sprints[ref])
    return {
        "sprints": set(fenced),
        "projects": projects,
        "refs": _fenced_card_refs(runtime, set(fenced), projects),
        "outcomes": outcomes,
    }


def _blind_fence(
    runtime: Any,
    payload: dict[str, Any],
    snapshot: dict[str, list[str]],
    reason: str,
) -> dict[str, Any]:
    """Fence every open sprint the last successful pass saw, plus every sprint-linked card.

    Two sources, because neither alone is complete: the snapshot names the reservations, and the live
    card metadata names the sprint each card is linked to, which catches a sprint opened since. A tick
    with no snapshot still fences every sprint-linked card; cards belonging to no sprint keep running.
    """
    sprints = set(snapshot)
    projects = {project for projects in snapshot.values() for project in projects}
    refs = _fenced_card_refs(runtime, sprints, projects)
    linked = _sprint_linked_cards(runtime)
    # Keys are card refs and values are the sprints they name: the fenced sets are keyed by
    # sprint, so the sprint each card belongs to is what joins them, not the card's own ref.
    fenced_sprints = sprints | set(linked.values())
    return {
        "sprints": fenced_sprints,
        "projects": projects,
        "refs": refs | set(linked),
        # Not a fence the durable log carries a reason for: it is one tick's read failure, it
        # clears by itself as soon as the board answers, and writing an event per tick of a
        # board outage would bury the fences that are about an actual observer.
        "outcomes": [
            {
                "status": "critical",
                "step": "observer-fence",
                "action": REASON_BOARD_UNAVAILABLE,
                "reason": (
                    "the sprint board could not be read, so no declared observer could be checked; "
                    f"every sprint-held project is fenced this tick: {reason}"
                ),
                "sprints": sorted(fenced_sprints),
                "projects": sorted(projects),
            }
        ],
    }


def _sprint_linked_cards(runtime: Any) -> dict[str, str]:
    """Every card that names a sprint, from the Pipeline board. Card ref -> sprint ref.

    Raises rather than answering `{}`: this is the blind path's only view of which cards belong to a
    sprint, and an empty answer would read as "no card belongs to one".
    """
    cards = select_cards(runtime.reader)
    return {
        str(card.get("ref") or ""): str(card.get("sprint") or "")
        for card in cards
        if str(card.get("ref") or "") and str(card.get("sprint") or "")
    }


def _snapshot(payload: dict[str, Any]) -> dict[str, list[str]]:
    raw = payload.get(FENCE_SNAPSHOT)
    if not isinstance(raw, dict):
        return {}
    return {
        str(ref): [str(project) for project in projects if str(project)]
        for ref, projects in raw.items()
        if str(ref) and isinstance(projects, list)
    }


def _put_snapshot(payload: dict[str, Any], open_sprints: dict[str, dict[str, Any]]) -> None:
    snapshot = {ref: sorted(_sprint_projects(sprint)) for ref, sprint in sorted(open_sprints.items())}
    if snapshot:
        payload[FENCE_SNAPSHOT] = snapshot
    else:
        payload.pop(FENCE_SNAPSHOT, None)


def _sprint_verdict(
    runtime: Any,
    sprint: dict[str, Any],
    record: ObserverRecord | None,
    *,
    pause_mode: str = "",
) -> dict[str, Any] | None:
    """Why this sprint is fenced, or None when it is free to run.

    `critical: False` marks a launch the pause is holding back: no record yet, or the deferral the
    drain itself wrote. The reason stays the same one an unpaused tick reports.
    """
    try:
        decision = observer_decision(runtime, sprint)
    except ObserverMetadataError as exc:
        return {"reason": exc.reason, "message": exc.message, "head": ""}
    if decision["kind"] == KIND_NONE:
        return None
    head = str(decision["head"])
    paused = pause_mode in PAUSED_LAUNCH_MODES
    if record is None:
        if paused:
            return {
                "reason": REASON_NO_RECORD,
                "message": (
                    f"declared observer {head} has not been launched: the pipeline is paused "
                    f"({pause_mode}), so its launch waits for the resume"
                ),
                "head": head,
                "critical": False,
            }
        return {
            "reason": REASON_NO_RECORD,
            "message": f"declared observer {head} has not been launched",
            "head": head,
        }
    if record.head != head:
        # A blank head is a record that never got one, and it is a mismatch like any other: what
        # the fence needs is a record naming *this* profile, not merely one that does not
        # contradict it.
        return {
            "reason": REASON_MISMATCH,
            "message": (
                f"declared observer {head} is not the running head {record.head or '(none recorded)'}"
            ),
            "head": head,
        }
    if record.abandoned_handle:
        return {
            "reason": REASON_ABANDONED,
            "message": f"declared observer {head} has an abandoned terminal from a failed bring-up",
            "head": head,
        }
    if record.state == "deferred" and paused and record.deferred_reason == DRAIN_DEFERRED_REASON:
        return {
            "reason": REASON_DEFERRED,
            "message": (
                f"declared observer {head} is not up: {record.deferred_reason}, so its launch waits "
                "for the resume"
            ),
            "head": head,
            "critical": False,
        }
    if record.state in {"deferred", "pending", "launching"}:
        return {
            "reason": REASON_DEFERRED,
            "message": (f"declared observer {head} is not up: {record.deferred_reason or record.state}"),
            "head": head,
        }
    liveness = observer_alive(record)
    if not liveness["pid_known"]:
        # The lifecycle grace window reads a head that has not written its pid yet as alive, and
        # that is right for deciding whether to relaunch it. It is not proof of adoption, and this
        # is the check that releases card mutations: the head may have died before it ever reached
        # the observer prompt. The fence holds until the pid is on disk, which is the "confirmed
        # adoption, normally on a later tick" this check exists to mean.
        return {
            "reason": REASON_NOT_ADOPTED,
            "message": (
                f"declared observer {head} has not written its pid yet, so its adoption is "
                f"unconfirmed ({liveness['reason']})"
            ),
            "head": head,
        }
    if not liveness["alive"]:
        return {
            "reason": REASON_DEAD,
            "message": f"declared observer {head} is not running ({liveness['reason']})",
            "head": head,
        }
    return None


def _raise(
    runtime: Any,
    payload: dict[str, Any],
    state: dict[str, Any],
    sprint: dict[str, Any],
    verdict: dict[str, Any],
) -> dict[str, Any]:
    """Open or keep the fence on one sprint, writing the fact once per reason.

    Critical, unless the verdict is a launch a pause holds back: then `deferred`, and the cards are
    fenced all the same.
    """
    ref = str(sprint.get("ref") or "")
    status = "critical" if verdict.get("critical", True) else STATUS_DEFERRED
    previous = state.get(ref)
    previous = previous if isinstance(previous, dict) else None
    if previous is not None and previous.get("reason") == verdict["reason"]:
        return {
            "status": status,
            "step": "observer-fence",
            "sprint": ref,
            "action": "observer-fenced",
            "observer_reason": verdict["reason"],
            "reason": verdict["message"],
            "since": previous.get("since"),
            "projects": sorted(_sprint_projects(sprint)),
        }
    since = now_rfc3339()
    episode = _next_episode(payload, ref)
    request_id = _fence_request_id(ref, str(verdict["reason"]), _episode_stamp(since, episode))
    # Durable before the cards are held back, in the order every other dispatcher effect uses: the
    # log carries why a sprint stopped even if this tick does not live to save its state.
    event = stage_event(
        runtime,
        EVENT_FENCED,
        ref,
        request_id,
        {
            "observer_reason": verdict["reason"],
            "head": verdict.get("head") or "",
            "projects": sorted(_sprint_projects(sprint)),
            "message": verdict["message"],
        },
        outcome=status,
    )
    audited = commit_event(runtime, event)
    state[ref] = {
        "reason": verdict["reason"],
        "since": since,
        "episode": episode,
        "head": verdict.get("head") or "",
        "request_id": request_id,
    }
    outcome = {
        "status": status,
        "step": "observer-fence",
        "sprint": ref,
        "action": "observer-fenced",
        "observer_reason": verdict["reason"],
        "reason": verdict["message"],
        "since": since,
        "projects": sorted(_sprint_projects(sprint)),
    }
    if not audited:
        outcome["audit"] = "pending"
    return outcome


def _clear(runtime: Any, state: dict[str, Any], ref: str) -> list[dict[str, Any]]:
    """Drop a fence whose sprint has an adopted observer again. Nothing when it was never up."""
    previous = state.pop(ref, None)
    if not isinstance(previous, dict):
        return []
    request_id = _fence_request_id(
        ref,
        "cleared",
        _episode_stamp(str(previous.get("since") or ""), previous.get("episode")),
    )
    event = stage_event(
        runtime,
        EVENT_CLEARED,
        ref,
        request_id,
        {"observer_reason": previous.get("reason"), "since": previous.get("since")},
    )
    audited = commit_event(runtime, event)
    outcome = {
        "status": "ok",
        "step": "observer-fence",
        "sprint": ref,
        "action": "observer-fence-cleared",
        "observer_reason": previous.get("reason"),
        "since": previous.get("since"),
    }
    if not audited:
        outcome["audit"] = "pending"
    return [outcome]


def _next_episode(payload: dict[str, Any], ref: str) -> int:
    """The number of this sprint's fence episode, advanced durably as the fence goes up."""
    counters = payload.get(FENCE_EPISODES)
    counters = dict(counters) if isinstance(counters, dict) else {}
    try:
        current = int(counters.get(ref) or 0)
    except (TypeError, ValueError):
        current = 0
    counters[ref] = current + 1
    payload[FENCE_EPISODES] = counters
    return current + 1


def _episode_stamp(since: str, episode: Any) -> str:
    """Identity of one fence episode, for the raise and its matching clear.

    The counter alone, without the timestamp beside it. `since` is minted fresh by every pass that
    raises, so a tick that committed the event and then died before saving its state would mint a
    different id on the retry and write a second `observer_fence_raised` for one episode. The counter
    is durable, so it is the same across such a retry and different across two real episodes.
    """
    try:
        number = int(episode)
    except (TypeError, ValueError):
        return since
    return f"#{number}" if number else since


def _fence_request_id(ref: str, reason: str, stamp: str) -> str:
    """`stamp` is `_episode_stamp`'s answer, never a raw clock reading: see why there."""
    digest = hashlib.sha256(f"{ref}\n{reason}\n{stamp}".encode()).hexdigest()[:32]
    return f"dispatcher-observer-fence-{digest}"


def _sprint_projects(sprint: dict[str, Any]) -> set[str]:
    return {str(project) for project in sprint.get("reservations") or [] if str(project)}


def _fenced_card_refs(runtime: Any, sprints: set[str], projects: set[str]) -> set[str]:
    """Every card the fenced sprints hold, by their link and by their reserved projects.

    Read once and in full rather than per sprint, because the set has to be complete: the tick hands
    it to reconciliation, and a card missing from here would have its heads settled while the fence
    is up. A read that fails is therefore not an empty set — it raises, and the tick ends fail-closed.
    """
    if not sprints and not projects:
        return set()
    cards = select_cards(runtime.reader)
    return {
        str(card.get("ref") or "")
        for card in cards
        if str(card.get("ref") or "")
        and (
            str(card.get("sprint") or "") in sprints
            or (str(card.get("project") or "") and str(card.get("project") or "") in projects)
        )
    }


def fenced_task(fence: dict[str, Any], task: dict[str, Any]) -> bool:
    """Whether one card belongs to a fenced sprint.

    Two ways in, because a card can be linked to the sprint by reference or merely sit in one of the
    projects that sprint reserved. Both are the fenced sprint's work.
    """
    if not fence.get("sprints"):
        return False
    if str(task.get("ref") or "") in fence.get("refs", set()):
        return True
    if str(task.get("sprint") or "") in fence.get("sprints", set()):
        return True
    project = str(task.get("project") or "")
    return bool(project) and project in fence.get("projects", set())
