"""The read half of the sprint surface: the sprint catalogue, sprint state and listing, comment
delivery, and what a close decided. See docs/PROTOCOLS.md, "Opening and watching a sprint".

Every value is read from the source that owns it: products/issues from `ProductIssueStore`,
projects from `registered_projects` plus `sprints/active-repositories.json`, head profiles from
`installed_heads` (observer eligibility via `check_observer_profile`), a close from its committed
audit event, the sprint from `SprintReader`, and observer liveness from `observer_snapshot`.

- `sprint_list` and `sprint_state` are one read (`_read_once`) with two framings and identical
  `work` sections; each source is read once for all sprints, never per sprint.
- Sources (`installation`, `sprints`, `cards`, `journal`, `liveness`) fail apart. The journal is its
  own source so an unreadable `board/events.ndjson` cannot blank the sprint rows.
- Sections are assembled by `SprintSections` through `ummanu.webproto.section`, which attributes
  each answer to its source and substitutes the declared no-claim shape on refusal.
- A closed sprint has no current card (`public_current_task`); a stopped one keeps it with
  `live` false. Either way the observer is `ended` and checks are `not_applicable`.
- An invalid installation config is a refused source, not a refused operation, when an explicit
  data directory is given.
- Reads write nothing (`SprintReader.list(create=False)`, never `show`, which creates the board).
- Liveness and comment delivery only read the dispatcher's records and cursors; nothing is launched
  or scheduled. Delivery is a batch fact, and never acceptance (:data:`ACCEPTANCE_ISSUE`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu.board.backend import PRODUCT_ISSUE, SPRINT, board_client
from ummanu.board.completion_evidence import is_po_executed
from ummanu.board.e2e_record import AFTER_MERGE, AM_COVERED, AM_PENDING, e2e_state
from ummanu.board.owner_handover import OWNER_ESCALATION, attention_record, waiting_owner
from ummanu.board.wait_card import RESULT_READY as WAIT_RESULT_READY
from ummanu.board.wait_card import TARGET_CARD, TARGET_RUN, TARGET_TIME
from ummanu.board.wait_card import WAITING as WAIT_WAITING
from ummanu.config import InstanceReport, validate_instance
from ummanu.dispatch.headless import headless_cards
from ummanu.dispatch.observer import (
    DeliveryStage,
    ObserverDelivery,
    delivery_evidence_summary,
    observer_snapshot,
)
from ummanu.head_registry import HeadRegistryConfigError, installed_heads, installed_pair
from ummanu.product_issues import ProductIssueStore, registered_projects
from ummanu.sprint_close import CLOSE_NOT_DONE
from ummanu.sprint_observer import (
    EXECUTOR_FIELDS,
    EXECUTOR_PINNED,
    NONE_SPELLING,
    OBSERVER_FIELD,
    ObserverMetadataError,
    check_observer_profile,
    head_choice,
    installed_head_profiles,
    stored_executors,
)
from ummanu.sprints import (
    SPRINT_CLOSED,
    SPRINT_STATUSES,
    SPRINT_TERMINAL_STATUSES,
    SprintReader,
    active_sprint_projects,
    audit_traversal,
    public_current_task,
    require_active_sprint_projects,
    sprint_guard_index_initialized,
)
from ummanu.tasks import TaskError, recorded_card_transition, task_audit_for
from ummanu.webproto import sources
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.errors import (
    InstallationUnavailable,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from ummanu.webproto.section import Reading, Rule, Section, SectionSet, SourceSet, read_source, render, rule

SCHEMA_VERSION = 1

#: The sprint document's sources, in the order a refusal is attributed.
SOURCE_INSTALLATION = "installation"
SOURCE_SPRINTS = "sprints"
SOURCE_CARDS = "cards"
SOURCE_JOURNAL = "journal"
SOURCE_LIVENESS = "liveness"
#: The reserved-project index, `sprints/active-repositories.json`: its own source because it fails
#: on its own, and it alone says whether the installation still holds a sprint's projects.
SOURCE_RESERVATIONS = "reservations"

#: The catalogue's sources.
SOURCE_CATALOGUE = "catalogue"
SOURCE_REGISTRY = "registry"
SOURCE_HEADS = "heads"

#: A sprint observer's state. `stopped` (raised, now gone) and `not_declared` (`--observer none`)
#: are never folded into `not_started`.
OBSERVER_NOT_STARTED = "not_started"
OBSERVER_RUNNING = "running"
OBSERVER_UNAVAILABLE = "unavailable"
OBSERVER_STOPPED = "stopped"
OBSERVER_NOT_DECLARED = "not_declared"
#: A closed or stopped sprint: the tick stops its observer and drops the record, so no record means
#: gone, not coming.
OBSERVER_ENDED = "ended"

OBSERVER_LAUNCH_STATES = (
    OBSERVER_NOT_STARTED,
    OBSERVER_RUNNING,
    OBSERVER_UNAVAILABLE,
    OBSERVER_STOPPED,
    OBSERVER_NOT_DECLARED,
    OBSERVER_ENDED,
)

#: Mandatory-check state of the current card. `unknown` (no dispatcher record names the card) is
#: never folded into `not_green`.
CHECKS_GREEN = "green"
CHECKS_NOT_GREEN = "not_green"
CHECKS_UNKNOWN = "unknown"
CHECKS_NOT_APPLICABLE = "not_applicable"

CHECK_STATES = (CHECKS_GREEN, CHECKS_NOT_GREEN, CHECKS_UNKNOWN, CHECKS_NOT_APPLICABLE)

#: Whether the journal dates the current card's board state. `recorded` carries a moment; `absent`
#: (no transition on the journal) is never a zero age; `not_applicable` is no current card or an
#: ended sprint's card; `unknown` is the journal or Pipeline listing unreadable.
TRANSITION_RECORDED = "recorded"
TRANSITION_ABSENT = "absent"
TRANSITION_NOT_APPLICABLE = "not_applicable"
TRANSITION_UNKNOWN = "unknown"

TRANSITION_STATES = (
    TRANSITION_RECORDED,
    TRANSITION_ABSENT,
    TRANSITION_NOT_APPLICABLE,
    TRANSITION_UNKNOWN,
)

#: Where a sprint stands: something is being worked on, something is waited for, something is
#: blocked, the sprint has ended, or the source that would say could not be read.
WAITING_WORKING = "working"
WAITING_WAITING = "waiting"
WAITING_BLOCKED = "blocked"
WAITING_ENDED = "ended"
WAITING_UNKNOWN = "unknown"

WAITING_STATES = (WAITING_WORKING, WAITING_WAITING, WAITING_BLOCKED, WAITING_ENDED, WAITING_UNKNOWN)

#: What a sprint waits for, one entry per waiting card (`work.waiting_on`): a run (wait card, e2e
#: run not answered, after-merge coverage or queue), the owner (handover, e2e budget decision), or
#: the PO (decision/operation card In progress and not handed over).
WAITING_ON_RUN = "run"
WAITING_ON_OWNER = "owner"
WAITING_ON_PO = "po"
WAITING_ON_DEPENDENCY = "dependency"
WAITING_ON_OBSERVER = "observer"
WAITING_ON_KINDS = (WAITING_ON_RUN, WAITING_ON_OWNER, WAITING_ON_PO, WAITING_ON_DEPENDENCY, WAITING_ON_OBSERVER)

#: The e2e run states (`E2eRun.status`) in which the run has not answered yet.
E2E_IN_FLIGHT = frozenset({"dispatching", "identifying", "wait_card_pending", "waiting"})
#: The detail of a merged card queued for its project's next after-merge e2e run (no run covers it yet).
AFTER_MERGE_QUEUED = "queued for the next after-merge run"

#: How a sprint row declares its observer; `malformed` is never read as `absent`.
OBSERVER_DECLARED = "declared"
OBSERVER_ABSENT = "absent"
OBSERVER_MALFORMED = "malformed"
#: The sprint board was unreadable. Not `absent`: the dispatcher state cannot prove a sprint
#: declared no observer.
OBSERVER_UNKNOWN = "unknown"

OBSERVER_DECLARATION_STATES = (
    OBSERVER_DECLARED,
    OBSERVER_ABSENT,
    OBSERVER_MALFORMED,
    OBSERVER_UNKNOWN,
)

#: Whether the journal holds one sprint comment; `absent` and `unknown` (unreadable) stay distinct.
COMMENT_SAVED = "saved"
COMMENT_ABSENT = "absent"
COMMENT_UNKNOWN = "unknown"

COMMENT_STATES = (COMMENT_SAVED, COMMENT_ABSENT, COMMENT_UNKNOWN)

#: Where a saved comment stands in the observer's technical delivery. Delivery is a batch fact:
#: at or before `acknowledged_through` the batch was acknowledged; after it the comment is in the
#: current or a later batch at that batch's stage. None of these states means acceptance
#: (:data:`ACCEPTANCE_ISSUE`, :data:`ACCEPTANCE_NOTICE`).
DELIVERY_SAVED = "saved"
DELIVERY_WAITING = "waiting"
DELIVERY_HANDED_OVER = "handed_over"
DELIVERY_ERROR = "error"
#: No readable production state, no observer record for the sprint, or a cursor the audit cannot
#: place. Never folded into the other states.
DELIVERY_UNKNOWN = "unknown"
#: A comment on a closed or stopped sprint: its observer is stopped and its record dropped, so no
#: batch will ever carry it.
DELIVERY_NOT_DELIVERABLE = "not_deliverable"

DELIVERY_STATES = (
    DELIVERY_SAVED,
    DELIVERY_WAITING,
    DELIVERY_HANDED_OVER,
    DELIVERY_ERROR,
    DELIVERY_NOT_DELIVERABLE,
    DELIVERY_UNKNOWN,
)

#: Stages of a fixed batch (sent or being retried); `idle` and `waiting_for_idle` have no
#: `through_event`.
_ACTIVE_STAGES = (DeliveryStage.DELIVERY_INTENT, DeliveryStage.AWAITING_ACK, DeliveryStage.RETRY_DEFERRED)

#: Stated in the answer itself so `handed_over` is not read as acceptance.
ACCEPTANCE_ISSUE = "issue:cf5c9f03ee0f92d3d347"
ACCEPTANCE_NOTICE = (
    "Delivery is not acceptance. Nothing in this document says the sprint's observer read this "
    "comment, agreed with it, or took it into account: what is established here is that the comment "
    "is saved and where the dispatcher's own delivery machinery has got it to. A semantic "
    f"acknowledgement is deferred by the owner and tracked as {ACCEPTANCE_ISSUE}."
)

#: Whether the journal holds the close; `absent` and `unknown` (unreadable) stay distinct.
CLOSE_RECORDED = "recorded"
CLOSE_ABSENT = "absent"
CLOSE_UNKNOWN = "unknown"

CLOSE_STATES = (CLOSE_RECORDED, CLOSE_ABSENT, CLOSE_UNKNOWN)

@dataclass(frozen=True, slots=True)
class _Production:
    """The dispatcher's production state, read once and classified once for the whole document."""

    payload: dict[str, Any]
    #: `observer_snapshot`'s rows keyed by sprint, computed once rather than once per sprint.
    observers: dict[str, dict[str, Any]]

    def record(self, card: str) -> dict[str, Any] | None:
        """The dispatcher's record for one card, or `None` when it holds none for it."""
        records = self.payload.get("records")
        record = records.get(card) if isinstance(records, dict) else None
        return record if isinstance(record, dict) else None


#: One sprint's board row and status view, or `(None, None)` when the sprint board did not answer.
_Sprint = tuple[dict[str, Any] | None, dict[str, Any] | None]


class SprintSections(SectionSet):
    """Every section of every sprint document, and the only place a source is attributed to one.

    `SectionSet` wraps each public method so a section must return a decided `Section`. Rules are
    declarative and receive only the sources they name; nothing here reads a file.
    """

    # -- the watched sprint and the listing ------------------------------------------------

    def sprint(self, read: SourceSet) -> Section:
        """The sprint's own record, as a watching page reads it."""
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda sprint: None if sprint[0] is None else {"value": _sprint_value(sprint[0])},
            ),
            blank={"value": None},
            narrates=(),
        )

    def listing(self, read: SourceSet, items: Callable[[], list[dict[str, Any]]]) -> Section:
        """Every sprint, or `null` items (never `[]`) when the board did not answer."""
        return read.decide(
            rule(SOURCE_SPRINTS, lambda _sprints: {"items": items()}),
            blank={"items": None},
            narrates=(),
        )

    # -- what one sprint is doing ------------------------------------------------------------

    def current_task(self, read: SourceSet) -> Section:
        """The current card; a closed sprint has none, a stopped sprint's card has `live` false."""

        def from_row(sprint: _Sprint) -> dict[str, Any] | None:
            if sprint[0] is None:
                return None
            reference, status, current = _subject(sprint)
            terminal = status in SPRINT_TERMINAL_STATUSES
            if status == "closed":
                return {"ref": None, "live": False, "reason": f"{reference} is closed; it has no current card"}
            if current is None:
                return {
                    "ref": None,
                    "live": False,
                    "reason": (
                        f"{reference} ended with no current card"
                        if terminal
                        else f"{reference} has no current card: nobody has cut one for it"
                    ),
                }
            reason = (
                f"{reference} is {status}: {current} is the card it was on when it ended, "
                "not work in progress"
                if terminal
                else f"{reference} is open and its observer has {current} as the current card"
            )
            return {"ref": current, "live": not terminal, "reason": reason}

        return read.decide(
            rule(SOURCE_SPRINTS, from_row),
            blank={"ref": None, "live": False, "reason": None},
        )

    def current_card_state(self, read: SourceSet, current: Section, *, now: float) -> Section:
        """Where the current card stands (Pipeline listing) and since when (journal).

        Its own section so an unreadable journal cannot blank `current_task`. The moment is the
        card's last state transition (:func:`ummanu.tasks.recorded_card_transition`), never
        `updated_at`. A card that is not `current_task.live` answers `not_applicable`.
        """
        card = current.fields.get("ref")
        live = bool(current.fields.get("live"))

        def from_row(sprint: _Sprint) -> dict[str, Any] | None:
            reference, status, _current = _subject(sprint)
            if sprint[1] is None:
                return None
            if card is None:
                return {
                    **_STANDING_BLANK,
                    "transition": TRANSITION_NOT_APPLICABLE,
                    "reason": f"{reference} has no current card, so no card of it is standing anywhere",
                }
            if not live:
                return {
                    **_STANDING_BLANK,
                    "card": card,
                    "transition": TRANSITION_NOT_APPLICABLE,
                    "reason": (
                        f"{reference} is {status}: {card} is the card it ended on, so nothing about "
                        "where it stands is still running"
                    ),
                }
            return None

        def from_journal(
            sprint: _Sprint, linked: dict[str, list[dict[str, Any]]], events: list[dict[str, Any]]
        ) -> dict[str, Any] | None:
            if sprint[1] is None or card is None or not live:
                return None
            _reference, entry = _card_of(sprint, linked)
            state = str((entry or {}).get("state") or "") or None
            title = str((entry or {}).get("title") or "") or None
            event = _last_transition(events, card)
            if event is None:
                return {
                    "card": card,
                    "title": title,
                    "state": state,
                    "since": None,
                    "age_seconds": None,
                    "transition": TRANSITION_ABSENT,
                    "reason": (
                        f"the committed audit records no state transition of {card}, so nothing "
                        f"here says when it entered {state or 'the column it stands in'}"
                    ),
                }
            moment = str(event.get("occurred_at") or "") or None
            return {
                "card": card,
                "title": title,
                "state": state,
                "since": moment,
                "age_seconds": _elapsed(moment, now),
                "transition": TRANSITION_RECORDED,
                "reason": (
                    f"{card} stands in {state or 'a column the Pipeline listing does not name'} "
                    f"and the committed audit dates its last transition to {moment or 'no moment'}"
                ),
            }

        return read.decide(
            rule(SOURCE_SPRINTS, from_row),
            Rule(SOURCE_JOURNAL, (SOURCE_SPRINTS, SOURCE_CARDS, SOURCE_JOURNAL), from_journal),
            blank=dict(_STANDING_BLANK),
            # `card` names the subject (the row's own field), not a claim about where it stands.
            narrates=("reason", "card"),
            unresolved=lambda reading: {
                **_STANDING_BLANK,
                "card": card,
                "reason": reading.source.reason,
            },
        )

    def head_profiles(self, read: SourceSet) -> Section:
        """The head profile each role runs on, joined against the installed registry.

        Observer: the head the dispatcher's record names (`launched`), else the row's declared
        profile (`declared` / `none`). Worker and reviewer: the pinned profile (`pinned`), or
        `unpinned` with no profile, since the dispatcher then picks per card.
        """

        def roles(sprint: _Sprint, registry: dict[str, Any], launched: str) -> dict[str, Any] | None:
            row = sprint[0]
            if row is None:
                return None
            profiles = {str(item.get("id")): item for item in registry.get("items") or []}
            declared = _declared_observer(row)
            value = declared["value"] or {}
            if launched:
                observer = _head_profile(profiles, launched, "launched")
            elif declared["profile"]:
                observer = _head_profile(profiles, declared["profile"], "declared")
            else:
                observer = _head_profile(
                    profiles, None, "none" if value.get("kind") == "none" else "undeclared"
                )
            executors = row.get("executors") if isinstance(row.get("executors"), dict) else {}
            executors = executors or stored_executors({})
            found = {"observer": observer}
            for role in EXECUTOR_FIELDS:
                state = executors.get(role) if isinstance(executors.get(role), dict) else {}
                if state.get("state") == EXECUTOR_PINNED:
                    found[role] = _head_profile(profiles, str(state.get("profile") or "") or None, "pinned")
                else:
                    found[role] = _head_profile(profiles, None, str(state.get("state") or "unpinned"))
            return found

        def with_record(
            sprint: _Sprint, registry: dict[str, Any], production: _Production
        ) -> dict[str, Any] | None:
            reference, _status, _current = _subject(sprint)
            record = production.observers.get(reference) or {}
            return roles(sprint, registry, str(record.get("head") or ""))

        return read.decide(
            Rule(SOURCE_HEADS, (SOURCE_SPRINTS, SOURCE_HEADS, SOURCE_LIVENESS), with_record),
            Rule(
                SOURCE_HEADS,
                (SOURCE_SPRINTS, SOURCE_HEADS),
                lambda sprint, registry: roles(sprint, registry, ""),
            ),
            blank={"observer": None, "worker": None, "reviewer": None},
        )

    def decision(self, read: SourceSet, freshness: Section) -> Section:
        """The last observer decision on the sprint row, with the `freshness` section beside it."""
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda sprint: (
                    None
                    if sprint[1] is None
                    else {"entry": sprint[1].get("resume"), "freshness": freshness}
                ),
            ),
            blank={"entry": None, "freshness": freshness},
            narrates=(),
        )

    def freshness(self, read: SourceSet) -> Section:
        """How fresh the last observer decision is.

        A closed or stopped sprint is judged on its frozen row alone. An open sprint needs the
        Pipeline listing and the journal; without either this section alone is unavailable.
        """

        def frozen(sprint: _Sprint) -> dict[str, Any] | None:
            view = sprint[1]
            if view is None or str(view.get("status") or "") not in SPRINT_TERMINAL_STATUSES:
                return None
            return {"value": view.get("resume_freshness")}

        def judged(sprint: _Sprint, _linked: Any, _events: Any) -> dict[str, Any] | None:
            view = sprint[1]
            return None if view is None else {"value": view.get("resume_freshness")}

        return read.decide(
            rule(SOURCE_SPRINTS, frozen),
            Rule(SOURCE_JOURNAL, (SOURCE_SPRINTS, SOURCE_CARDS, SOURCE_JOURNAL), judged),
            blank={"value": None},
            narrates=(),
        )

    def cards(self, read: SourceSet) -> Section:
        """The sprint's cards by board state; `states` is `null`, never `{}`, when the listing failed."""
        return read.decide(
            Rule(
                SOURCE_CARDS,
                (SOURCE_SPRINTS, SOURCE_CARDS),
                lambda sprint, _linked: (
                    None if sprint[1] is None else {"states": sprint[1].get("cards") or {}}
                ),
            ),
            blank={"states": None},
            narrates=(),
        )

    def degraded_cards(self, read: SourceSet) -> Section:
        """This sprint's cards standing in an active column with no worker anything can name."""
        return read.decide(
            Rule(
                SOURCE_LIVENESS,
                (SOURCE_SPRINTS, SOURCE_LIVENESS),
                lambda sprint, _production: (
                    None if sprint[1] is None else {"items": sprint[1].get("degraded_cards")}
                ),
            ),
            blank={"items": None},
            narrates=(),
        )

    def checks(self, read: SourceSet) -> Section:
        """The current card's mandatory checks, as the dispatcher's production record has them.

        `gate_state` is `green` only for the card's current code state; nothing is re-run. An ended
        sprint or a missing current card is `not_applicable` under `sprints`, unaffected by an
        unreadable dispatcher state.
        """

        def from_row(sprint: _Sprint) -> dict[str, Any] | None:
            reference, status, current = _subject(sprint)
            if sprint[1] is None:
                return None
            if status in SPRINT_TERMINAL_STATUSES:
                return {
                    "card": current,
                    "gate": None,
                    "state": CHECKS_NOT_APPLICABLE,
                    "reason": f"{reference} is {status}: no card of it is running checks",
                }
            if current is None:
                return {
                    "card": None,
                    "gate": None,
                    "state": CHECKS_NOT_APPLICABLE,
                    "reason": f"{reference} has no current card, so no card's checks are due",
                }
            return None

        def from_dispatcher(sprint: _Sprint, production: _Production) -> dict[str, Any] | None:
            _reference, _status, current = _subject(sprint)
            if sprint[1] is None or current is None:
                return None
            record = production.record(current)
            if record is None:
                return {
                    "card": current,
                    "gate": None,
                    "state": CHECKS_UNKNOWN,
                    "reason": (
                        f"the dispatcher holds no record for {current}, so nothing here says "
                        "whether its mandatory checks have passed"
                    ),
                }
            gate = _gate(record)
            if gate["state"] == "green":
                return {
                    "card": current,
                    "gate": gate,
                    "state": CHECKS_GREEN,
                    "reason": (
                        "the mechanical gate is green for "
                        f"{gate['attested_sha'] or 'the recorded candidate'}"
                    ),
                }
            return {
                "card": current,
                "gate": gate,
                "state": CHECKS_NOT_GREEN,
                "reason": _not_green_reason(gate, current),
            }

        return read.decide(
            rule(SOURCE_SPRINTS, from_row),
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), from_dispatcher),
            blank={"card": None, "gate": None, "state": CHECKS_UNKNOWN, "reason": None},
            # `card` names the subject (the row's own field), not a claim about its checks.
            narrates=("reason", "card"),
            unresolved=lambda reading: {
                "card": _current_of(read),
                "gate": None,
                "state": CHECKS_UNKNOWN,
                "reason": reading.source.reason,
            },
        )

    def waiting(self, read: SourceSet) -> Section:
        """Where this sprint stands, decided from already-read sources in this order:

        * the row alone: `ended` (closed), `blocked` (stopped), `waiting` (no current card);
        * the Pipeline listing: `blocked` for a current card in Blocked, before the dispatcher;
        * a `decision`/`operation` card In progress runs no head: `waiting` on the PO, or on the owner
          once handed over;
        * the dispatcher: `working`, degraded `blocked`, or no record. A column alone is not evidence a
          head runs (`docs/OPERATIONS.md`);
        * otherwise the board's `waiting` for Ready, Issues or Done.

        `card` is the sprint's current card, or null.
        """

        def from_row(sprint: _Sprint) -> dict[str, Any] | None:
            reference, status, current = _subject(sprint)
            if sprint[1] is None:
                return None
            if status == "closed":
                return {
                    "state": WAITING_ENDED,
                    "reason": f"{reference} is closed: nothing is waiting on it",
                    "card": current,
                }
            if status == "stopped":
                stopped = sprint[1].get("stop_reason") or "no reason recorded"
                return {"state": WAITING_BLOCKED, "reason": f"{reference} was stopped: {stopped}", "card": current}
            if current is None:
                return {
                    "state": WAITING_WAITING,
                    "reason": f"{reference} has no current card: nobody has cut one for it",
                    "card": None,
                }
            return None

        def board_holds_it(sprint: _Sprint, linked: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
            """A card the board holds in Blocked is blocked, and no record makes it less so."""
            current, card = _card_of(sprint, linked)
            settled = _board_wait(current, card)
            return None if settled is None or settled[0] != WAITING_BLOCKED else _said(settled, current)

        def with_the_po(sprint: _Sprint, linked: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
            """An open decision/operation card: the PO has it, or the owner does."""
            current, card = _card_of(sprint, linked)
            settled = _po_card_wait(current, card)
            return None if settled is None else _said(settled, current)

        def dispatcher_holds_it(sprint: _Sprint, production: _Production) -> dict[str, Any] | None:
            _reference, _status, current = _subject(sprint)
            if sprint[1] is None or current is None:
                return None
            degraded = (sprint[1].get("degraded_cards") or {}).get(current)
            if degraded is not None:
                return {
                    "state": WAITING_BLOCKED,
                    "reason": (
                        f"{current} stands in an active column with no worker the dispatcher can "
                        f"name ({degraded.get('state') or 'no record state'})"
                    ),
                    "card": current,
                }
            record = production.record(current)
            if record is None:
                return None
            return {
                "state": WAITING_WORKING,
                "reason": f"the dispatcher record for {current} is {record.get('state') or 'unnamed'!s}",
                "card": current,
            }

        def board_settled(sprint: _Sprint, linked: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
            current, card = _card_of(sprint, linked)
            settled = _board_wait(current, card)
            return None if settled is None else _said(settled, current)

        def nothing_claimed(sprint: _Sprint, _production: _Production) -> dict[str, Any] | None:
            _reference, _status, current = _subject(sprint)
            if sprint[1] is None or current is None:
                return None
            return {
                "state": WAITING_WAITING,
                "reason": (
                    f"the dispatcher holds no record for {current}: nothing of it has been claimed yet"
                ),
                "card": current,
            }

        return read.decide(
            rule(SOURCE_SPRINTS, from_row),
            Rule(SOURCE_CARDS, (SOURCE_SPRINTS, SOURCE_CARDS), board_holds_it),
            Rule(SOURCE_CARDS, (SOURCE_SPRINTS, SOURCE_CARDS), with_the_po),
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), dispatcher_holds_it),
            Rule(SOURCE_CARDS, (SOURCE_SPRINTS, SOURCE_CARDS), board_settled),
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), nothing_claimed),
            blank={"state": WAITING_UNKNOWN, "reason": None, "card": None},
            narrates=("reason", "card"),
            unresolved=lambda reading: {
                "state": WAITING_UNKNOWN,
                "reason": _unsettled_reason(read, reading.source.reason),
                "card": _current_of(read),
            },
        )

    # -- the observer ------------------------------------------------------------------------

    def declaration(self, read: SourceSet) -> Section:
        """What the sprint row declares; `unknown` (never `absent`) when the board was unreadable."""
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda sprint: None if sprint[0] is None else _declared_observer(sprint[0]),
            ),
            blank={"state": OBSERVER_UNKNOWN, "value": None, "profile": None},
            narrates=(),
        )

    def launch(self, read: SourceSet) -> Section:
        """Whether an observer is up, from the production state read against the sprint row.

        Without the row the dispatcher's silence means nothing, so the section is unavailable rather
        than `not_started`.
        """

        def from_dispatcher(sprint: _Sprint, production: _Production) -> dict[str, Any] | None:
            row, _view = sprint
            if row is None:
                return None
            reference, status, _current = _subject(sprint)
            observer = production.observers.get(reference)
            state, reason = _launch_state(_declared_observer(row), observer, status)
            return {
                "state": state,
                "reason": reason,
                "record": None if observer is None else _observer_record(observer),
            }

        return read.decide(
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), from_dispatcher),
            blank={"state": OBSERVER_UNAVAILABLE, "reason": None, "record": None},
            unresolved=lambda reading: {
                "state": OBSERVER_UNAVAILABLE,
                "reason": reading.source.reason or "the source that would say could not be read",
                "record": None,
            },
        )

    # -- one comment, and what happened to it ------------------------------------------------

    def comment(self, read: SourceSet, reference: str, comment_id: str) -> Section:
        """Whether the journal holds this comment on this sprint; `unknown`, never `absent`, if unreadable."""

        def from_journal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            event = _comment_event(events, reference, comment_id)
            if event is None:
                return {
                    "id": comment_id,
                    "state": COMMENT_ABSENT,
                    "occurred_at": None,
                    "role": None,
                    "reason": (
                        f"the committed audit holds no comment {comment_id} on {reference}"
                    ),
                }
            actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
            return {
                "id": comment_id,
                "state": COMMENT_SAVED,
                "occurred_at": str(event.get("occurred_at") or "") or None,
                "role": str(actor.get("role") or "") or None,
                "reason": f"the committed audit holds this comment on {reference}",
            }

        return read.decide(
            rule(SOURCE_JOURNAL, from_journal),
            blank={
                "id": None,
                "state": COMMENT_UNKNOWN,
                "occurred_at": None,
                "role": None,
                "reason": None,
            },
            # `id` is which comment the answer would have been about, not a claim about it.
            narrates=("reason", "id"),
            unresolved=lambda reading: {
                "id": comment_id,
                "state": COMMENT_UNKNOWN,
                "occurred_at": None,
                "role": None,
                "reason": reading.source.reason,
            },
        )

    def delivery(self, read: SourceSet, reference: str, comment_id: str) -> Section:
        """Where the dispatcher's delivery machinery has got this comment to; read-only.

        A comment absent from the journal is settled without the cursors; an ended sprint is
        `not_deliverable`; otherwise the comment's journal position is placed against the
        production-state cursors. Not a semantic acknowledgement (:data:`ACCEPTANCE_NOTICE`).
        """

        def not_in_the_journal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            if _comment_event(events, reference, comment_id) is not None:
                return None
            return {
                "state": DELIVERY_UNKNOWN,
                "reason": (
                    f"the committed audit holds no comment {comment_id} on {reference}, so there is "
                    "nothing here to place against the observer's delivery cursors"
                ),
                "batch": None,
            }

        def sprint_has_ended(
            sprint: _Sprint, events: list[dict[str, Any]]
        ) -> dict[str, Any] | None:
            _ref, status, _current = _subject(sprint)
            if sprint[0] is None or status not in SPRINT_TERMINAL_STATUSES:
                return None
            return {
                "state": DELIVERY_NOT_DELIVERABLE,
                "reason": (
                    f"{reference} is {status}, so no delivery batch will carry this comment: the "
                    "production tick stops the observer of a sprint that is no longer open and drops "
                    "its record. The comment is saved on the committed audit, which is what a PO "
                    "adding the outcome after the fact is doing"
                ),
                "batch": None,
            }

        def from_dispatcher(
            events: list[dict[str, Any]], production: _Production
        ) -> dict[str, Any] | None:
            row = production.observers.get(reference)
            carried = (row or {}).get("delivery")
            delivery = (
                ObserverDelivery.from_json(carried) if isinstance(carried, dict) else None
            )
            state, reason = _delivery_state(
                events, _event_position(events, comment_id), delivery, reference=reference
            )
            return {
                "state": state,
                "reason": reason,
                "batch": None if delivery is None else _delivery_batch(delivery),
            }

        return read.decide(
            Rule(SOURCE_JOURNAL, (SOURCE_JOURNAL,), not_in_the_journal),
            # Before the dispatcher: an ended sprint's record is dropped, so the cursors would wrongly
            # answer `unknown`.
            Rule(SOURCE_SPRINTS, (SOURCE_SPRINTS, SOURCE_JOURNAL), sprint_has_ended),
            Rule(SOURCE_LIVENESS, (SOURCE_JOURNAL, SOURCE_LIVENESS), from_dispatcher),
            blank={"state": DELIVERY_UNKNOWN, "reason": None, "batch": None},
        )

    # -- one close, and what it decided ------------------------------------------------------

    def close(self, read: SourceSet, reference: str, event_id: str) -> Section:
        """What the journal records this close decided, copied from its event and never re-decided.

        An unreadable journal leaves `unknown`, never `absent`. Closed is not done: the document
        carries :data:`ummanu.sprint_close.CLOSE_NOT_DONE` beside this section.
        """

        def from_journal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            event = _close_event(events, reference, event_id)
            if event is None:
                return {
                    **_CLOSE_BLANK,
                    "id": event_id,
                    "state": CLOSE_ABSENT,
                    "reason": f"the committed audit holds no close {event_id} of {reference}",
                }
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
            actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
            return {
                "id": event_id,
                "state": CLOSE_RECORDED,
                "occurred_at": str(event.get("occurred_at") or "") or None,
                "closed_by": str(actor.get("id") or "") or None,
                "closing_reason": str(payload.get("reason") or "") or None,
                "issue_decisions": list(decisions.get("issues") or []),
                "closed_issues": list(payload.get("closed_issues") or []),
                "card_dispositions": list(decisions.get("cards") or []),
                "archived_tasks": list(payload.get("archived_tasks") or []),
                "disposed_tasks": list(payload.get("disposed_tasks") or []),
                "closeout": _closeout_of(payload.get("closeout")),
                "reason": f"the committed audit holds this close of {reference}",
            }

        return read.decide(
            rule(SOURCE_JOURNAL, from_journal),
            blank=dict(_CLOSE_BLANK),
            # `id` names the close asked about, not a claim about it.
            narrates=("reason", "id"),
            unresolved=lambda reading: {
                **_CLOSE_BLANK,
                "id": event_id,
                "reason": reading.source.reason,
            },
        )

    def reservations(self, read: SourceSet, reference: str) -> Section:
        """Which of the sprint's declared reservations the reserved-project index still holds for it.

        Needs both the row and the index; an unreadable index is never folded into "released".
        """

        def from_index(sprint: _Sprint, index: dict[str, list[str]]) -> dict[str, Any] | None:
            row = sprint[0]
            if row is None:
                return None
            declared = [str(project) for project in row.get("reservations") or []]
            held = [project for project in declared if reference in (index.get(project) or [])]
            released = [project for project in declared if project not in held]
            return {
                "declared": declared,
                "released": released,
                "held": held,
                "reason": (
                    f"the reserved-project index still holds {', '.join(held)} for {reference}"
                    if held
                    else f"the reserved-project index holds no project for {reference}"
                ),
            }

        return read.decide(
            Rule(SOURCE_RESERVATIONS, (SOURCE_SPRINTS, SOURCE_RESERVATIONS), from_index),
            blank={"declared": None, "released": None, "held": None, "reason": None},
        )

    # -- the catalogue -----------------------------------------------------------------------

    def products(self, read: SourceSet) -> Section:
        """The products a sprint may be opened on, off the board that owns them."""
        return read.decide(
            rule(SOURCE_CATALOGUE, lambda catalogue: {"items": catalogue[0]}),
            blank={"items": None},
            narrates=(),
        )

    def issues(self, read: SourceSet) -> Section:
        """The issues a create will admit -- the open ones, each carrying the product that owns it."""
        return read.decide(
            rule(SOURCE_CATALOGUE, lambda catalogue: {"items": catalogue[1]}),
            blank={"items": None},
            narrates=(),
        )

    def projects(self, read: SourceSet) -> Section:
        """The registered projects, with the open sprint holding each one where one does."""
        return read.decide(
            rule(SOURCE_REGISTRY, lambda registry: {"items": registry}),
            blank={"items": None},
            narrates=(),
        )

    def heads(self, read: SourceSet) -> Section:
        """The head profiles this installation runs off, as a sprint may name them."""
        return read.decide(
            rule(SOURCE_HEADS, lambda registry: registry),
            blank={
                "items": None,
                # `none` (no observer) is not a profile id but is offered so clients need not know the
                # word. Product vocabulary, so present whether or not the registry answered.
                "observer": {"none": NONE_SPELLING, "default": None},
                "role_defaults": {},
                "executor_roles": list(EXECUTOR_FIELDS),
            },
            narrates=(),
        )


#: Stateless; one instance serves every call and enumeration.
SECTIONS = SprintSections()


class SprintReadLayer(ProtocolBoundary):
    """One installation's sprints, read with no knowledge of who is asking.

    Construction does no I/O; each read resolves the instance, board and registry when called.
    `board_client` is a seam for tests or transports, not a mode.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        owner_events: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self.owner_events = owner_events
        self._clock = clock

    # -- shared plumbing -------------------------------------------------------------------

    def report(self) -> InstanceReport:
        """The validated installation, or `InstallationUnavailable`; used only to resolve the data dir."""
        report, refused = self._installation(now=self._clock())
        if report is None:
            raise InstallationUnavailable(str(refused.source.reason))
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _installation(self, *, now: float) -> tuple[InstanceReport | None, Reading]:
        """The installation config as a source, and the refusal only it can force.

        An invalid config refuses only what it owns (data plane location, budget thresholds); with an
        explicit data directory the rest is still answered. Without one the operation is refused.
        """
        report = validate_instance(self.instance)
        if report.ok and report.data_dir is not None:
            return report, Reading(SOURCE_INSTALLATION, sources.available(now), report)
        reason = (
            "this instance config does not validate: "
            + "; ".join(str(error) for error in report.errors[:5])
            if report.errors
            else "this instance config names no data directory"
        )
        if self._data_dir is None:
            raise InstallationUnavailable(reason)
        return None, Reading(
            SOURCE_INSTALLATION,
            sources.unavailable(reason, now=now, evidence=report.instance_path),
            None,
        )

    # -- operations ------------------------------------------------------------------------

    def sprint_options(self) -> dict[str, Any]:
        """What a sprint can be built from, from the sources that own it.

        Issues are the admissible ones (open, each carrying its owning product), so a client cannot
        assemble a create `_check_ownership` would refuse. Every entry has a `label` beside its id.
        """
        now = self._clock()
        report, installation = self._installation(now=now)
        data_dir = self.data_dir(report)
        read = SourceSet(
            [
                installation,
                self._catalogue(data_dir, now=now),
                self._registry(data_dir, now=now),
                self._head_profiles(now=now),
            ]
        )
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_options",
                "observed_at": sources.isoformat(now),
                "products": SECTIONS.products(read),
                "issues": SECTIONS.issues(read),
                "projects": SECTIONS.projects(read),
                "heads": SECTIONS.heads(read),
                "installation": read.mark(SOURCE_INSTALLATION),
            }
        )

    def sprint_list(self, *, statuses: Sequence[str] | None = None) -> dict[str, Any]:
        """Every sprint and what it is doing; the same `_read_once` and sections as :meth:`sprint_state`.

        `statuses` (`open`, `closed`, `stopped`) filters after the one board pass, at no extra cost.
        """
        now = self._clock()
        wanted = _wanted_statuses(statuses)
        report, installation = self._installation(now=now)
        read = self._read_once(
            report, installation, self.data_dir(report), now=now, listing=frozenset(wanted)
        )

        def items() -> list[dict[str, Any]]:
            rows, views = read.value(SOURCE_SPRINTS)
            return [
                {
                    **_identity(row, view),
                    **self._work(read.replacing(SOURCE_SPRINTS, (row, view)), now=now),
                    "observer": self._observer(read.replacing(SOURCE_SPRINTS, (row, view))),
                }
                for row, view in zip(rows, views, strict=True)
                if not wanted or str(view["status"]) in wanted
            ]

        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_list",
                "observed_at": sources.isoformat(now),
                "filter": {"statuses": sorted(wanted)},
                "sprints": SECTIONS.listing(read, items),
                # Source marks for the document too, so an unreadable board is distinguishable from
                # an installation with no sprints.
                **self._marks(read),
            }
        )

    def sprint_state(self, ref: str) -> dict[str, Any]:
        """One sprint, its observer liveness, and its `work` (the same object a listing item carries).

        Sprint fields and liveness fail apart: an unreadable dispatcher state is reported as unknown
        liveness, not as an observer that is down.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a sprint reference is required")
        report, installation = self._installation(now=now)
        read = self._read_once(report, installation, self.data_dir(report), now=now)
        row, view = _find(read, reference)
        if row is None and read.answered(SOURCE_SPRINTS):
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        sprint = read.replacing(SOURCE_SPRINTS, (row, view))
        return render(
            {
                # First key, as `ummanu sprint status` prints it; null when the board did not answer.
                "status": str((row or {}).get("status") or "") or None,
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint",
                "observed_at": sources.isoformat(now),
                "ref": reference,
                "sprint": SECTIONS.sprint(sprint),
                "observer": self._observer(sprint),
                "work": self._work(sprint, now=now),
                **self._marks(read),
            }
        )

    def sprint_comment_delivery(self, ref: str, comment_id: str) -> dict[str, Any]:
        """What happened to one saved comment, from durable state only; writes and wakes nothing.

        `comment` is whether it is on the committed audit, `delivery` where the observer delivery
        machinery has got it to, and `acceptance` states that neither means the observer accepted
        it, naming the deferred issue.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a sprint reference is required")
        identifier = str(comment_id or "")
        if not identifier:
            raise ValidationRefused(
                "reading what happened to a comment needs the identifier the write answered with"
            )
        report, installation = self._installation(now=now)
        read = self._read_once(report, installation, self.data_dir(report), now=now)
        row, view = _find(read, reference)
        if row is None and read.answered(SOURCE_SPRINTS):
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        # Narrowed to this sprint first: `delivery` depends on whether this sprint has ended.
        read = read.replacing(SOURCE_SPRINTS, (row, view))
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_comment_delivery",
                "observed_at": sources.isoformat(now),
                "ref": reference,
                "comment_id": identifier,
                "comment": SECTIONS.comment(read, reference, identifier),
                "delivery": SECTIONS.delivery(read, reference, identifier),
                # Not a section: a fixed statement of what this answer does not mean.
                "acceptance": {"established": False, "issue": ACCEPTANCE_ISSUE, "reason": ACCEPTANCE_NOTICE},
                **self._marks(read),
            }
        )

    def sprint_close_result(self, ref: str, event_id: str) -> dict[str, Any]:
        """What one close decided and left behind; the read half of `SprintOperationLayer.sprint_close`.

        `close` (committed audit), `reservations` (reserved-project index) and `sprint` (board) fail
        apart. `definition_of_done` is a fixed statement that closing never means the goal was reached.
        Writes nothing.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a sprint reference is required")
        identifier = str(event_id or "")
        if not identifier:
            raise ValidationRefused(
                "reading what a close decided needs the identifier the close answered with"
            )
        report, installation = self._installation(now=now)
        read = self._read_once(report, installation, self.data_dir(report), now=now)
        row, view = _find(read, reference)
        if row is None and read.answered(SOURCE_SPRINTS):
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        sprint = read.replacing(SOURCE_SPRINTS, (row, view))
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_close_result",
                "observed_at": sources.isoformat(now),
                "ref": reference,
                "event_id": identifier,
                "close": SECTIONS.close(sprint, reference, identifier),
                "reservations": SECTIONS.reservations(sprint, reference),
                "sprint": SECTIONS.sprint(sprint),
                "definition_of_done": {"satisfied": False, "reason": CLOSE_NOT_DONE},
                **self._marks(read),
            }
        )

    # -- assembly ----------------------------------------------------------------------------

    def _work(self, sprint: SourceSet, *, now: float) -> dict[str, Any]:
        """What one sprint is doing, in the sections a listing and a watched page both carry."""
        current = SECTIONS.current_task(sprint)
        return {
            "current_task": current,
            "current_card_state": SECTIONS.current_card_state(sprint, current, now=now),
            "decision": SECTIONS.decision(sprint, SECTIONS.freshness(sprint)),
            "cards": SECTIONS.cards(sprint),
            "degraded_cards": SECTIONS.degraded_cards(sprint),
            "checks": SECTIONS.checks(sprint),
            "waiting": SECTIONS.waiting(sprint),
            "waiting_on": _waiting_on(sprint),
            "attention": self._attention(sprint),
            "head_profiles": SECTIONS.head_profiles(sprint),
        }

    def _attention(self, sprint: SourceSet) -> dict[str, Any]:
        """Only this sprint's open human-wait events, from the bell's same board reading."""
        if self.owner_events is None or not sprint.answered(SOURCE_SPRINTS) or not sprint.answered(SOURCE_CARDS):
            return {"state": "unknown", "event_ids": [], "reason": "human wait source is unavailable"}
        row, _view = sprint.value(SOURCE_SPRINTS)
        if not row or row.get("status") != "open":
            return {"state": "none", "event_ids": [], "reason": None}
        snapshot = self.owner_events.snapshot()
        if snapshot["state"] != "available":
            return {"state": "unknown", "event_ids": [], "reason": snapshot["reason"]}
        identifiers = [wait["event_id"] for wait in snapshot["human_waits"] if wait["sprint_ref"] == row["ref"]]
        return {"state": "waiting" if identifiers else "none", "event_ids": identifiers, "reason": None}

    def _observer(self, sprint: SourceSet) -> dict[str, Any]:
        """The sprint's declaration and its observer's liveness (`observer_snapshot`), as two sections."""
        return {"declared": SECTIONS.declaration(sprint), "launch": SECTIONS.launch(sprint)}

    def _marks(self, read: SourceSet) -> dict[str, Any]:
        """The availability of every source of the document, said once for the document."""
        return {
            key: read.mark(key)
            for key in (SOURCE_CARDS, SOURCE_JOURNAL, SOURCE_LIVENESS, SOURCE_INSTALLATION)
        }

    # -- the sources -------------------------------------------------------------------------

    def _read_once(
        self,
        report: InstanceReport | None,
        installation: Reading,
        data_dir: Path,
        *,
        now: float,
        listing: frozenset[str] | None = None,
    ) -> SourceSet:
        """Every source a sprint document is built from, read once each, never once per sprint.

        Sources fail apart and each failure marks only the sections it feeds. The journal is read here,
        not inside `status_views`, so an unreadable `board/events.ndjson` cannot fail the sprint board.

        `listing` is the listing's status filter; with it the journal is read as a slice covering only
        kept non-terminal sprints (`_journal_references`). Other documents read the whole journal,
        since comment delivery is placed by position in the whole stream.

        Uses `SprintReader.list(create=False)`, never `show` (which creates the board).
        """
        client = self._client()
        liveness = self._production(data_dir, now=now)
        reader = SprintReader(client, data_dir=data_dir, thresholds=_thresholds(report))
        cards = self._linked_cards(reader, data_dir, now=now)
        linked = cards.value if cards.answered else {}
        production: _Production | None = liveness.value if liveness.answered else None
        rows: list[dict[str, Any]] | None = None
        refused: Exception | None = None
        try:
            rows = reader.list(statuses=set(listing or ()), create=False)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            refused = exc
        journal = self._journal(
            data_dir,
            client=client,
            now=now,
            # With no rows the slice is empty; the journal's mark then comes from the store's probe.
            references=None if listing is None else _journal_references(rows or [], linked),
        )
        try:
            if rows is None:
                raise refused or LookupError("the sprint board answered nothing")
            # Status-view rules stay in `SprintReader`; it gets the journal handed in, so it cannot
            # fail for the journal's reasons.
            views = reader.status_views(
                rows,
                linked,
                observers=production.observers if production is not None else {},
                headless=headless_cards(production.payload if production is not None else {}),
                audit=audit_traversal(journal.value if journal.answered else []),
            )
            sprints = Reading(SOURCE_SPRINTS, sources.available(now), (rows, views))
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            sprints = Reading(
                SOURCE_SPRINTS,
                sources.unavailable(
                    f"the sprint board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ),
                None,
            )
        return SourceSet(
            [
                installation,
                sprints,
                cards,
                journal,
                liveness,
                self._reservations(data_dir, now=now),
                # Last: only `head_profiles` uses it, so its refusal is never attributed elsewhere.
                self._head_profiles(now=now),
            ]
        )

    def _reservations(self, data_dir: Path, *, now: float) -> Reading:
        """The reserved-project index, read once; refused rather than answered `{}`.

        `{}` is safe for the write guard but would wrongly report every reservation as released here.
        """
        path = data_dir / "sprints" / "active-repositories.json"
        try:
            index = require_active_sprint_projects(data_dir)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return Reading(
                SOURCE_RESERVATIONS,
                sources.unavailable(
                    f"the reserved-project index could not be read: {_reason(exc)}",
                    now=now,
                    evidence=path,
                ),
                None,
            )
        return Reading(SOURCE_RESERVATIONS, sources.available(now), index)

    def _production(self, data_dir: Path, *, now: float) -> Reading:
        """The dispatcher's production state, read and classified once; a refusal is never "nothing runs"."""
        path = data_dir / "dispatcher" / "production-state.json"

        def produce() -> _Production:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("the dispatcher production state is not an object")
            return _Production(payload, _observer_rows(payload))

        return read_source(
            SOURCE_LIVENESS,
            produce,
            refusal=lambda exc: f"the dispatcher production state could not be read: {_reason(exc)}",
            now=now,
            evidence=path,
        )

    def _journal(
        self,
        data_dir: Path,
        *,
        client: Any,
        now: float,
        references: set[str] | None = None,
    ) -> Reading:
        """The committed audit via `task_audit_for` (`docs/BOARD_STORE.md` §7.3), or a slice of it.

        With `references` the store filters to those refs. An empty slice still goes through the
        store's bounded probe, so the journal's mark is always backed by a real attempt.
        """
        try:
            audit = task_audit_for(client)
            events = audit.events() if references is None else audit.events(references=references)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return Reading(
                SOURCE_JOURNAL,
                sources.unavailable(
                    f"the committed audit journal could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "events.ndjson",
                ),
                None,
            )
        return Reading(SOURCE_JOURNAL, sources.available(now), events)

    def _linked_cards(self, reader: SprintReader, data_dir: Path, *, now: float) -> Reading:
        """Every sprint's cards, in one Pipeline listing, or the reason there are none to show."""
        try:
            linked = reader.linked_cards()
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return Reading(
                SOURCE_CARDS,
                sources.unavailable(
                    f"the Pipeline board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ),
                None,
            )
        return Reading(SOURCE_CARDS, sources.available(now), linked)

    def _catalogue(self, data_dir: Path, *, now: float) -> Reading:
        """Products and issues from one `catalogue` pass; one failure marks both sections alike."""
        try:
            store = ProductIssueStore(self._client(), data_dir=data_dir, instance=self._instance_dir())
            # `include_closed=False` matches `_check_ownership`, which refuses closed issues.
            raw_products, raw_issues = store.catalogue(include_closed=False)
            products = [
                {
                    "id": str(product.get("id") or ""),
                    "label": str(product.get("title") or "") or str(product.get("id") or ""),
                    "ref": str(product.get("ref") or ""),
                    "projects": [str(project) for project in product.get("projects") or []],
                }
                for product in raw_products
                if str(product.get("id") or "")
            ]
            issues = [
                {
                    "ref": str(issue.get("ref") or ""),
                    "label": str(issue.get("title") or "") or str(issue.get("ref") or ""),
                    "product": str(issue.get("product") or ""),
                    "kind": str(issue.get("kind") or "") or None,
                    "priority": str(issue.get("priority") or "") or None,
                }
                for issue in raw_issues
                if str(issue.get("ref") or "")
            ]
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return Reading(
                SOURCE_CATALOGUE,
                sources.unavailable(
                    f"the board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ),
                None,
            )
        return Reading(
            SOURCE_CATALOGUE,
            sources.available(now),
            (
                sorted(products, key=lambda item: item["id"]),
                sorted(issues, key=lambda item: item["ref"]),
            ),
        )

    def _registry(self, data_dir: Path, *, now: float) -> Reading:
        """The registered projects and which open sprint holds each, from the sources a create
        refuses against (`registered_projects`, the guard index).
        """
        try:
            registered = sorted(registered_projects(self.instance))
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return Reading(
                SOURCE_REGISTRY,
                sources.unavailable(
                    f"the project registry could not be read: {_reason(exc)}",
                    now=now,
                    evidence=self._instance_dir() / "projects",
                ),
                None,
            )
        # The guard index reads unreadable as empty ("held by nobody"), so ask whether it is
        # established first; `reserved_by` is null when it is not.
        reserved_known = sprint_guard_index_initialized(data_dir)
        held = active_sprint_projects(data_dir) if reserved_known else {}
        return Reading(
            SOURCE_REGISTRY,
            sources.available(now),
            [
                {
                    "id": project,
                    "label": project,
                    "reserved_by": (sorted(held.get(project, [])) if reserved_known else None),
                }
                for project in registered
            ],
        )

    def _head_profiles(self, *, now: float) -> Reading:
        """The installed head profiles; observer eligibility asked of `check_observer_profile`."""
        try:
            registry = installed_heads(self.instance)
            eligible = installed_head_profiles(self.instance)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return Reading(
                SOURCE_HEADS,
                sources.unavailable(
                    f"the head registry could not be read: {_reason(exc)}",
                    now=now,
                    evidence=self._head_registry_evidence(),
                ),
                None,
            )
        profiles = registry.get("profiles") or {}
        role_defaults = {
            str(role): str(profile)
            for role, profile in (registry.get("role_defaults") or {}).items()
            if isinstance(profile, str) and profile
        }
        defaults_by_profile: dict[str, list[str]] = {}
        for role, profile in sorted(role_defaults.items()):
            defaults_by_profile.setdefault(profile, []).append(role)
        items = []
        for profile_id in sorted(profiles):
            entry = profiles[profile_id] if isinstance(profiles[profile_id], dict) else {}
            observer_ok, observer_reason = _observer_eligibility(profile_id, eligible)
            items.append(
                {
                    "id": profile_id,
                    "label": _profile_label(profile_id, entry),
                    "adapter": str(entry.get("adapter") or "") or None,
                    "model": str(entry.get("model") or "") or None,
                    "effort": str(entry.get("effort") or "") or None,
                    "resource": str(entry.get("resource") or "") or None,
                    "observer": observer_ok,
                    "observer_reason": observer_reason,
                    "role_default_for": defaults_by_profile.get(profile_id, []),
                }
            )
        return Reading(
            SOURCE_HEADS,
            sources.available(now),
            {
                "items": items,
                "observer": {"none": NONE_SPELLING, "default": role_defaults.get("observer")},
                "role_defaults": role_defaults,
                "executor_roles": list(EXECUTOR_FIELDS),
            },
        )

    # -- plumbing --------------------------------------------------------------------------

    def _client(self) -> Any:
        """One client for both boards this layer reads: sprints and Product/Issue."""
        try:
            return self._board_client or board_client(
                self._instance_dir(), serves=(SPRINT, PRODUCT_ISSUE)
            )
        except TaskError as exc:
            raise RuntimeUnavailable(f"the sprint board is not usable: {exc.message}") from exc

    def _head_registry_evidence(self) -> Path:
        """The snapshot a reader would have read, or `instance.yaml` when even its place is unknown."""
        try:
            return installed_pair(self.instance).snapshot
        except HeadRegistryConfigError:
            return self._instance_dir() / "instance.yaml"

    def _instance_dir(self) -> Path:
        return self.instance.parent if self.instance.is_file() else self.instance


def _find(read: SourceSet, reference: str) -> _Sprint:
    """One sprint's row and status view, or `(None, None)` when the board did not answer."""
    if not read.answered(SOURCE_SPRINTS):
        return None, None
    rows, views = read.value(SOURCE_SPRINTS)
    for row, view in zip(rows, views, strict=True):
        if str(row.get("ref") or "") == reference:
            return row, view
    return None, None


def _subject(sprint: _Sprint) -> tuple[str, str, str | None]:
    """Which sprint a section is about: ref, status, and the `public_current_task` (none if closed)."""
    row, view = sprint
    held = view or row or {}
    status = str(held.get("status") or "")
    return (
        str(held.get("ref") or ""),
        status,
        public_current_task(status, str(held.get("current_task") or "") or None),
    )


def _current_of(read: SourceSet) -> str | None:
    """The current card of the sprint in front of these sources, when the board answered at all."""
    if not read.answered(SOURCE_SPRINTS):
        return None
    return _subject(read.value(SOURCE_SPRINTS))[2]


def _card_of(
    sprint: _Sprint, linked: dict[str, list[dict[str, Any]]]
) -> tuple[str | None, dict[str, Any] | None]:
    """The sprint's current card as the Pipeline listing has it, or `None` when it holds none."""
    reference, _status, current = _subject(sprint)
    if sprint[1] is None or current is None:
        return current, None
    return current, next(
        (
            entry
            for entry in linked.get(reference) or []
            if isinstance(entry, dict) and str(entry.get("ref") or "") == current
        ),
        None,
    )


def _said(settled: tuple[str, str], card: str | None) -> dict[str, Any]:
    return {"state": settled[0], "reason": settled[1], "card": card}


def _journal_references(
    rows: list[dict[str, Any]], linked: dict[str, list[dict[str, Any]]]
) -> set[str]:
    """The refs a non-terminal sprint's sections consult: its own, its current card's, its linked cards'.

    Closed and stopped sprints are judged on their own record and add nothing.
    """
    references: set[str] = set()
    for row in rows:
        if str(row.get("status") or "") in SPRINT_TERMINAL_STATUSES:
            continue
        reference = str(row.get("ref") or "")
        references.add(reference)
        references.add(str(row.get("current_task") or ""))
        references.update(
            str(card.get("ref") or "")
            for card in linked.get(reference) or []
            if isinstance(card, dict)
        )
    references.discard("")
    return references


def _wanted_statuses(statuses: Sequence[str] | None) -> set[str]:
    """The status filter; an unknown status is a `validation` refusal, never an empty listing."""
    wanted = {str(status) for status in statuses or ()}
    unknown = sorted(wanted - SPRINT_STATUSES)
    if unknown:
        raise ValidationRefused(
            f"unknown sprint statuses: {', '.join(unknown)}; this product has "
            + ", ".join(sorted(SPRINT_STATUSES))
        )
    return wanted


def _identity(row: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    """Which sprint this is, and what it was opened for, from the status view that already has it."""
    return {
        "ref": str(view.get("ref") or ""),
        "goal": str(view.get("goal") or ""),
        "status": str(view.get("status") or ""),
        "product": view.get("product"),
        "issues": view.get("issues"),
        "reservations": view.get("reservations"),
        "repositories": row.get("repositories") or [],
        "executors": view.get("executors") or stored_executors({}),
        "po_session": view.get("po_session"),
        "allowed_productions": list(view.get("allowed_productions") or []),
        "owner_decisions": list(view.get("owner_decisions") or []),
        "local_run_exceptions": view.get("local_run_exceptions", []),
        "budget": view.get("budget"),
    }


def _thresholds(report: InstanceReport | None) -> dict[str, int] | None:
    """The installation's budget thresholds, or `None` (product defaults) when the config is invalid."""
    instance = report.instance if report is not None else None
    thresholds = instance.get("sprint_budget") if isinstance(instance, dict) else None
    return thresholds if isinstance(thresholds, dict) else None


def _observer_rows(production: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The dispatcher's observer rows, keyed by sprint, classified once for the whole document."""
    if production is None:
        return {}
    return {
        str(row.get("sprint") or ""): row
        for row in observer_snapshot(production)
        if str(row.get("sprint") or "")
    }


def _gate(record: dict[str, Any]) -> dict[str, Any]:
    """The mechanical gate fields of one dispatcher record, copied so internals are not a contract."""
    attestation = record.get("gate_attestation")
    attestation = attestation if isinstance(attestation, dict) else {}
    return {
        "state": str(record.get("gate_state") or "") or None,
        # `validated_sha` is the attested candidate; `base_sha` is what it was validated against.
        "attested_sha": str(attestation.get("validated_sha") or "") or None,
        "base_sha": str(attestation.get("base_sha") or "") or None,
        "gate_mode": str(attestation.get("gate_mode") or "") or None,
        "pending_since": record.get("gate_pending_since") or None,
        "transport_error": str(record.get("gate_transport_error") or "") or None,
        "record_state": str(record.get("state") or "") or None,
    }


def _not_green_reason(gate: dict[str, Any], card: str) -> str:
    """Why the current card's gate is not green, from the record's own evidence and no other."""
    if gate["transport_error"]:
        return f"the gate backend did not answer for {card}: {gate['transport_error']}"
    if gate["pending_since"]:
        return f"the gate for {card} is waiting on a run that has not finished"
    return (
        f"the mechanical gate has not passed for the current code state of {card} "
        f"(the dispatcher record is {gate['record_state'] or 'unnamed'})"
    )


#: Current-card board states that settle where a sprint stands on their own: `blocked` (held, with
#: its reason) and columns where nothing runs. Active columns are absent because a column is not
#: evidence a head is behind it (see `degraded_cards`).
_BOARD_SETTLED_STATES = ("blocked", "ready", "issues", "done")


def _board_wait(reference: str, card: dict[str, Any] | None) -> tuple[str, str] | None:
    """Where the Pipeline listing alone puts the sprint, or `None`; independent of the dispatcher."""
    if card is None:
        return None
    state = str(card.get("state") or "")
    if state == "blocked":
        reason = str(card.get("blocked_by") or "") or "no reason recorded on the card"
        return WAITING_BLOCKED, f"{reference} stands in Blocked: {reason}"
    if state in ("ready", "issues"):
        return (
            WAITING_WAITING,
            f"{reference} stands in {state.replace('_', ' ')} and nothing has claimed it yet",
        )
    if state == "done":
        return (
            WAITING_WAITING,
            f"{reference} is done: the sprint is waiting for its observer to cut the next card",
        )
    return None


def _po_card_wait(reference: str | None, card: dict[str, Any] | None) -> tuple[str, str] | None:
    """Where an In progress `decision`/`operation` current card puts its sprint, or `None`.

    Such a card runs no head: the PO completes it or hands it to the owner (`task handover`).
    """
    if card is None or not is_po_executed(card) or str(card.get("state") or "") != "in_progress":
        return None
    kind = str(card.get("type") or "")
    mark = waiting_owner(card)
    if mark is not None:
        return WAITING_WAITING, f"{reference} ({kind}) is handed to the owner: {mark['reason']}"
    if escalation := attention_record(card, OWNER_ESCALATION):
        return WAITING_WAITING, f"{reference} ({kind}) has an unresolved owner escalation: {escalation['reason']}"
    return WAITING_WAITING, f"{reference} ({kind}) is with the PO"


def _waiting_on(read: SourceSet) -> list[dict[str, Any]] | None:
    """What the sprint waits for: `{kind, card, detail}` per waiting card, from the Pipeline listing only.

    Not a section (it is a list); its source is the `cards` mark. Null, never `[]`, when the sprint
    board or listing did not answer. A closed sprint waits for nothing.
    """
    if not (read.answered(SOURCE_SPRINTS) and read.answered(SOURCE_CARDS)):
        return None
    sprint = read.value(SOURCE_SPRINTS)
    if sprint[1] is None:
        return None
    reference, status, _current = _subject(sprint)
    if status == "closed":
        return []
    linked = read.value(SOURCE_CARDS)
    found: list[dict[str, Any]] = []
    cards = (linked.get(reference) if isinstance(linked, dict) else None) or []
    superseded = {str((card.get("workspace") or {}).get("supersedes") or "") for card in cards if isinstance(card, dict)}
    for card in cards:
        if isinstance(card, dict) and card.get("ref") not in superseded:
            found.extend(card_waits(card))
    return found


def card_waits(card: dict[str, Any]) -> list[dict[str, Any]]:
    """One card's `work.waiting_on` entries; `[]` when it waits for nothing.

    Tolerates missing, partial or legacy `wait`, `e2e` or handover marks. A done card waits only on
    its after-merge e2e: the covering run (for every covered card), the next run while queued, or a
    budget decision holding the batch. One run is reported once per card.
    """
    reference = str(card.get("ref") or "")
    state = str(card.get("state") or "")
    kind = str(card.get("type") or "")
    if not reference:
        return []
    live = state != "done" and not card.get("closed")
    found: list[dict[str, Any]] = []

    def said(what: str, detail: str) -> None:
        found.append({"kind": what, "card": reference, "detail": detail})

    wait = card.get("wait") if isinstance(card.get("wait"), dict) else {}
    if (
        live
        and state != "blocked"
        and kind == "wait"
        and wait.get("state") in (WAIT_WAITING, WAIT_RESULT_READY)
    ):
        said(WAITING_ON_RUN, wait_line(wait))
    e2e = card.get("e2e") if isinstance(card.get("e2e"), dict) else {}
    runs = [*(_list(e2e.get("runs")) if live else []), *_list(e2e.get("after_merge_runs"))]
    # Keys (run URL, dispatch id) of runs already reported, and of carried runs that answered.
    said_runs: set[str] = set()
    concluded: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        keys = {str(run.get(name) or "") for name in ("run", "dispatch_id")} - {""}
        if run.get("state") not in E2E_IN_FLIGHT:
            concluded |= keys
            continue
        if keys & said_runs:
            continue
        said_runs |= keys
        sha = str(run.get("sha") or "")[:12] or "an unrecorded SHA"
        said(
            WAITING_ON_RUN,
            f"e2e run {run.get('run') or '(not identified yet)'} on {sha}: {str(run.get('state')).replace('_', ' ')}",
        )
    covering = e2e_state(card).after_merge
    if covering is not None and covering.state == AM_COVERED:
        keys = {covering.run_url, covering.dispatch_id} - {""}
        if not keys & (said_runs | concluded):
            said_runs |= keys
            run_text = covering.run_url or f"{covering.dispatch_id or '?'} (not identified yet)"
            said(
                WAITING_ON_RUN,
                f"after-merge e2e run {run_text} carried by {covering.carrier or 'an unrecorded card'}, "
                f"covering merge {covering.merge_sha[:12]}",
            )
    elif covering is not None and covering.state == AM_PENDING:
        said(WAITING_ON_RUN, AFTER_MERGE_QUEUED)
    if e2e.get("mark") and (live or e2e.get("placement") == AFTER_MERGE):
        holder = str(e2e.get("waiting_on") or (covering.decision if covering else "") or "")
        said(WAITING_ON_DEPENDENCY, str(e2e.get("mark")))
        if holder:
            found[-1]["holder"] = holder
    if not live:
        return found
    mark = waiting_owner(card)
    if mark is not None:
        said(
            WAITING_ON_OWNER,
            f"{kind or 'card'} handed to the owner: {mark.get('reason') or 'no reason recorded'}",
        )
    elif escalation := attention_record(card, OWNER_ESCALATION):
        said(WAITING_ON_OWNER, str(escalation["reason"]))
    elif (route := e2e_state(card).hotfix_route) and (
        route.result["status"] == "settled"
        or (route.result["holder"] and route.result["status"] in {"waiting", "follow_up"})
    ):
        if route.result["holder"] and route.result["status"] in {"waiting", "follow_up"}:
            said(WAITING_ON_DEPENDENCY, route.result["reason"])
            found[-1]["holder"] = route.result["holder"]
    elif is_po_executed(card) and state == "in_progress":
        said(WAITING_ON_PO, f"{kind} card with the PO")
    elif state == "blocked" and kind != "wait" and not e2e_state(card).budget_decline:
        said(WAITING_ON_OBSERVER, "Blocked work returns to the sprint observer")
    return found


def wait_line(wait: dict[str, Any]) -> str:
    """A wait block in one line: its target, since when, its deadline, and a result waiting delivery."""
    target = wait.get("target") if isinstance(wait.get("target"), dict) else {}
    what = str(target.get("kind") or "")
    if what == TARGET_RUN:
        subject = str(target.get("link") or target.get("url") or "") or (
            f"GitHub run {target.get('repo') or '?'}#{target.get('run_id') or '?'}"
        )
    elif what == TARGET_CARD:
        states = " or ".join(str(item) for item in _list(target.get("states"))) or "an unrecorded state"
        subject = f"card {target.get('ref') or '?'} reaching {states}"
    elif what == TARGET_TIME:
        subject = f"the time {target.get('at') or '?'}"
    else:
        subject = "an unreadable target"
    line = f"waits for {subject}"
    if wait.get("waiting_since"):
        line += f" since {wait['waiting_since']}"
    if wait.get("deadline"):
        line += f", deadline {wait['deadline']}"
    if wait.get("state") == WAIT_RESULT_READY:
        line += ": result ready, delivery pending"
    return line


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unsettled_reason(read: SourceSet, refusal: str | None) -> str:
    """Why nothing settled this sprint, naming the card's known column so `unknown` stays narrow."""
    refused = refusal or "the source that would say could not be read"
    if not (read.answered(SOURCE_SPRINTS) and read.answered(SOURCE_CARDS)):
        return refused
    current, card = _card_of(read.value(SOURCE_SPRINTS), read.value(SOURCE_CARDS))
    if card is None:
        return refused
    state = str(card.get("state") or "") or "an unnamed column"
    return (
        f"{current} stands in {state.replace('_', ' ')}, which is not on its own evidence that "
        f"anything is running on it, and {refused}"
    )


#: The current-card standing that claims nothing; shared by every no-claim branch.
_STANDING_BLANK: dict[str, Any] = {
    "card": None,
    "title": None,
    "state": None,
    "since": None,
    "age_seconds": None,
    "transition": TRANSITION_UNKNOWN,
    "reason": None,
}


def _last_transition(events: list[dict[str, Any]], card: str) -> dict[str, Any] | None:
    """The last event that moved this card (:func:`ummanu.tasks.recorded_card_transition`).

    Walks the append-ordered journal backwards; only transitions count, never later comments or
    verdicts.
    """
    if not card:
        return None
    for event in reversed(events):
        if str(event.get("ref") or "") != card:
            continue
        if recorded_card_transition(event) is not None:
            return event
    return None


def _elapsed(moment: str | None, now: float) -> float | None:
    """Seconds since a journal moment, or `None` (never `0`) when it cannot be parsed."""
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    # The journal writes UTC; a naive moment is UTC, not host-local time.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, round(now - parsed.timestamp(), 3))


#: The close section that claims nothing; shared by every no-claim branch so none claims more.
_CLOSE_BLANK: dict[str, Any] = {
    "id": None,
    "state": CLOSE_UNKNOWN,
    "occurred_at": None,
    "closed_by": None,
    "closing_reason": None,
    "issue_decisions": None,
    "closed_issues": None,
    "card_dispositions": None,
    "archived_tasks": None,
    "disposed_tasks": None,
    "closeout": None,
    "reason": None,
}


def _close_event(
    events: list[dict[str, Any]], reference: str, event_id: str
) -> dict[str, Any] | None:
    """The committed close event of this sprint under this identifier, or nothing.

    Event id, ref and kind must all match, so one sprint is never answered with another's write.
    """
    if not event_id:
        return None
    for event in events:
        if (
            str(event.get("event_id") or "") == event_id
            and str(event.get("ref") or "") == reference
            and str(event.get("kind") or "") == SPRINT_CLOSED
        ):
            return event
    return None


def _closeout_of(plan: Any) -> dict[str, Any] | None:
    """The closeout a close recorded, as the answer carries it, or `None` when it wrote none."""
    if not isinstance(plan, dict) or not plan.get("document"):
        return None
    return {
        "document": str(plan.get("document") or ""),
        "commit": str(plan.get("commit") or "") or None,
        "written": bool(plan.get("written")),
    }


def _comment_event(
    events: list[dict[str, Any]], reference: str, comment_id: str
) -> dict[str, Any] | None:
    """The committed `commented` event of this sprint under this identifier, or nothing.

    Event id, ref and kind must all match, so one sprint is never answered with another's write.
    """
    if not comment_id:
        return None
    for event in events:
        if (
            str(event.get("event_id") or "") == comment_id
            and str(event.get("ref") or "") == reference
            and str(event.get("kind") or "") == "commented"
        ):
            return event
    return None


def _event_position(events: list[dict[str, Any]], event_id: str) -> int:
    """An event's index in the committed stream, or `-1`.

    The whole stream, never a sprint's slice: delivery cursors are installation-wide event ids.
    """
    if not event_id:
        return -1
    for index, event in enumerate(events):
        if str(event.get("event_id") or "") == event_id:
            return index
    return -1


def _delivery_state(
    events: list[dict[str, Any]],
    comment_at: int,
    delivery: ObserverDelivery | None,
    *,
    reference: str,
) -> tuple[str, str]:
    """One comment's technical delivery, from the dispatcher's existing cursors:

    * no observer record, or a cursor the audit cannot place: `unknown`;
    * `acknowledged_through` at or after the comment: `handed_over` (delivery, not acceptance);
    * `waiting_for_idle`: `waiting` (the owed batch carries every event after the acknowledged one);
    * `delivery_intent` / `awaiting_ack` with `through_event` at or after the comment: `waiting`;
    * `retry_deferred` over the same range: `error`, with the recorded failure;
    * otherwise (`idle`, or a batch fixed before the comment): `saved`.
    """
    if delivery is None:
        return DELIVERY_UNKNOWN, (
            f"the dispatcher's production state holds no observer record for {reference}, so nothing "
            "here says whether a delivery batch has carried this comment"
        )
    acknowledged = delivery.acknowledged_through
    if acknowledged:
        at = _event_position(events, acknowledged)
        if at < 0:
            return DELIVERY_UNKNOWN, (
                f"the acknowledged delivery cursor {acknowledged} names an event the committed audit "
                "does not hold, so this comment cannot be placed against it"
            )
        if at >= comment_at:
            return DELIVERY_HANDED_OVER, (
                f"the observer of {reference} acknowledged a delivery batch through {acknowledged}, "
                "which is this comment's event or a later one; that is technical delivery of the "
                "batch and not a statement that the comment was read, accepted or taken into account"
            )
    if delivery.stage == DeliveryStage.WAITING_FOR_IDLE:
        return DELIVERY_WAITING, (
            f"a delivery batch for {reference} is held until its observer head is idle: "
            f"{delivery.reason or 'no reason recorded'}"
        )
    if delivery.stage in _ACTIVE_STAGES and delivery.through_event:
        at = _event_position(events, delivery.through_event)
        if at < 0:
            return DELIVERY_UNKNOWN, (
                f"the active delivery cursor {delivery.through_event} names an event the committed "
                "audit does not hold, so this comment cannot be placed against it"
            )
        if at >= comment_at:
            if delivery.stage == DeliveryStage.RETRY_DEFERRED:
                return DELIVERY_ERROR, (
                    "the delivery batch carrying this comment failed and is deferred for retry: "
                    + (delivery.last_failure_reason or "no reason recorded")
                    + "; the dispatcher owns the redelivery"
                )
            return DELIVERY_WAITING, (
                f"the delivery batch carrying this comment is {delivery.stage.value} and has not "
                "been acknowledged"
            )
    return (
        DELIVERY_SAVED,
        f"this comment is saved and no delivery batch of {reference}'s observer carries it yet",
    )


def _delivery_batch(delivery: ObserverDelivery) -> dict[str, Any]:
    """The delivery record fields this answer stands on, copied (as `_gate` does) so internals are
    not a contract; `evidence` is the same `delivery_evidence_summary` line the head is given.
    """
    return {
        "stage": delivery.stage.value,
        "delivery_id": delivery.delivery_id or None,
        "through_event": delivery.through_event or None,
        "acknowledged_through": delivery.acknowledged_through or None,
        "acknowledged_delivery_id": delivery.acknowledged_delivery_id or None,
        "wake_attempts": delivery.wake_attempts,
        "wake_failures": delivery.wake_failures,
        "launch_delivery_failures": delivery.launch_delivery_failures,
        "last_failure_reason": delivery.last_failure_reason or None,
        "evidence": delivery_evidence_summary(delivery) or None,
    }


def _profile_label(profile_id: str, profile: dict[str, Any]) -> str:
    """A pickable label: adapter, pinned model (or "default model") and effort; the id stays separate."""
    parts = [str(profile.get("adapter") or "").strip() or profile_id]
    parts.append(str(profile.get("model") or "").strip() or "default model")
    effort = str(profile.get("effort") or "").strip()
    if effort:
        parts.append(f"{effort} effort")
    return " · ".join(parts)


def _head_profile(profiles: dict[str, dict[str, Any]], profile: str | None, via: str) -> dict[str, Any]:
    """One role's profile joined against the registry; an unregistered profile keeps its id with
    `registered` false.
    """
    entry = profiles.get(profile) if profile else None
    return {
        "profile": profile,
        "via": via,
        "registered": entry is not None,
        "label": entry.get("label") if entry else None,
        "adapter": entry.get("adapter") if entry else None,
        "model": entry.get("model") if entry else None,
        "effort": entry.get("effort") if entry else None,
    }


def _observer_eligibility(profile_id: str, eligible: set[str]) -> tuple[bool, str | None]:
    """Whether a sprint may declare this profile as its observer, decided by the create's own check."""
    try:
        check_observer_profile(head_choice(profile_id), eligible, subject="sprint")
    except ObserverMetadataError as exc:
        return False, exc.message
    return True, None


def _declared_observer(sprint: dict[str, Any] | None) -> dict[str, Any]:
    """The observer a sprint row declares, in the three states the row can be in."""
    if sprint is None or "observer" not in sprint:
        return {"state": OBSERVER_ABSENT, "value": None, "profile": None}
    value = sprint.get("observer")
    if not isinstance(value, dict):
        return {"state": OBSERVER_MALFORMED, "value": None, "profile": None}
    return {
        "state": OBSERVER_DECLARED,
        "value": value,
        "profile": str(value.get("profile") or "") or None,
    }


def _launch_state(
    declared: dict[str, Any], row: dict[str, Any] | None, status: str
) -> tuple[str, str]:
    """The observer's launch state, from the declaration, the sprint's status and the record.

    With no record, an ended sprint is `ended` (the tick stopped and dropped it), not `not_started`.
    """
    if row is None:
        if status in SPRINT_TERMINAL_STATUSES:
            return (
                OBSERVER_ENDED,
                f"this sprint is {status}: the tick stopped its observer and holds no record for it",
            )
        if declared["state"] == OBSERVER_DECLARED and (declared["value"] or {}).get("kind") == "none":
            return (
                OBSERVER_NOT_DECLARED,
                "this sprint declares no observer, so the tick raises none for it",
            )
        return (
            OBSERVER_NOT_STARTED,
            "the sprint is saved and the production tick holds no observer for it yet",
        )
    if bool(row.get("alive")):
        return OBSERVER_RUNNING, f"an observer head is up on {row.get('head') or 'an unnamed profile'}"
    return (
        OBSERVER_STOPPED,
        "the dispatcher holds an observer record whose head is not alive: "
        + str(row.get("heartbeat_state") or "no heartbeat evidence"),
    )


def _observer_record(row: dict[str, Any]) -> dict[str, Any]:
    """The part of the dispatcher's observer row a watching page reads.

    Keeps `delivery`: the observer skill copies `delivery_id` and `through_event` from it into the
    acknowledging resume. `deferred_reason` and the idle pair explain why a declared observer is not up.
    """
    delivery = row.get("delivery")
    return {
        "head": str(row.get("head") or "") or None,
        "delivery": delivery if isinstance(delivery, dict) else None,
        "deferred_reason": str(row.get("deferred_reason") or "") or None,
        "idle_since": row.get("idle_since") or None,
        "idle_reason": str(row.get("idle_reason") or "") or None,
        "state": str(row.get("state") or "") or None,
        "alive": bool(row.get("alive")),
        "pid_known": bool(row.get("pid_known")),
        "heartbeat_state": str(row.get("heartbeat_state") or "") or None,
        "bound": bool(row.get("bound")),
        "paused": bool(row.get("paused")),
        "launches": row.get("launches"),
        "last_action": str(row.get("last_action") or "") or None,
        "last_action_at": row.get("last_action_at"),
        "stopped_reason": str(row.get("stopped_reason") or "") or None,
    }


def _sprint_value(sprint: dict[str, Any] | None) -> dict[str, Any] | None:
    """One sprint as a watching page reads it: what it was opened with, and where it is now."""
    if sprint is None:
        return None
    executors = sprint.get("executors")
    status = str(sprint.get("status") or "")
    return {
        "ref": str(sprint.get("ref") or ""),
        "goal": str(sprint.get("goal") or ""),
        "definition_of_done": str(sprint.get("definition_of_done") or ""),
        "status": status,
        "product": sprint.get("product"),
        "issues": sprint.get("issues"),
        "reservations": sprint.get("reservations"),
        "repositories": sprint.get("repositories") or [],
        "current_task": public_current_task(status, sprint.get("current_task")),
        # Always both roles, each with a state; "nobody pinned" is a value, not a missing key.
        "executors": executors if isinstance(executors, dict) else stored_executors({}),
        # Null and empty for a sprint opened before these were recorded.
        "po_session": sprint.get("po_session"),
        "allowed_productions": list(sprint.get("allowed_productions") or []),
        "owner_decisions": list(sprint.get("owner_decisions") or []),
        "local_run_exceptions": sprint.get("local_run_exceptions", []),
        # The e2e run budget: `e2e: <used> of <budget>` and the cards that spent the runs.
        "e2e": sprint.get("e2e"),
        "resume": sprint.get("resume"),
        "budget": sprint.get("budget"),
        "audit": sprint.get("audit"),
    }


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


#: Includes `OBSERVER_FIELD` so callers need not know which module defines it.
__all__ = [
    "ACCEPTANCE_ISSUE",
    "ACCEPTANCE_NOTICE",
    "CHECKS_GREEN",
    "CHECKS_NOT_APPLICABLE",
    "CHECKS_NOT_GREEN",
    "CHECKS_UNKNOWN",
    "CHECK_STATES",
    "COMMENT_ABSENT",
    "COMMENT_SAVED",
    "COMMENT_STATES",
    "COMMENT_UNKNOWN",
    "DELIVERY_ERROR",
    "DELIVERY_HANDED_OVER",
    "DELIVERY_SAVED",
    "DELIVERY_STATES",
    "DELIVERY_UNKNOWN",
    "DELIVERY_WAITING",
    "OBSERVER_ABSENT",
    "OBSERVER_DECLARATION_STATES",
    "OBSERVER_DECLARED",
    "OBSERVER_ENDED",
    "OBSERVER_FIELD",
    "OBSERVER_LAUNCH_STATES",
    "OBSERVER_MALFORMED",
    "OBSERVER_NOT_DECLARED",
    "OBSERVER_NOT_STARTED",
    "OBSERVER_RUNNING",
    "OBSERVER_STOPPED",
    "OBSERVER_UNAVAILABLE",
    "OBSERVER_UNKNOWN",
    "SCHEMA_VERSION",
    "SOURCE_CARDS",
    "SOURCE_INSTALLATION",
    "SOURCE_JOURNAL",
    "SOURCE_LIVENESS",
    "SOURCE_SPRINTS",
    "TRANSITION_ABSENT",
    "TRANSITION_NOT_APPLICABLE",
    "TRANSITION_RECORDED",
    "TRANSITION_STATES",
    "TRANSITION_UNKNOWN",
    "WAITING_BLOCKED",
    "WAITING_ENDED",
    "WAITING_STATES",
    "WAITING_UNKNOWN",
    "WAITING_WAITING",
    "WAITING_WORKING",
    "SprintReadLayer",
    "SprintSections",
]
