"""Production prompt handoffs: the tick's shared allowance, the operation that spends it, the cursor.

A prompt for an agent's composer is typed, waited on until its echo turn closes, submitted, and
seen to start a turn (`LocalPtyHeadRuntime._deliver_prompt`). Interactive callers wait for all of
it. The production dispatcher cannot: one reconcile pass serves every active card, and the settle,
echo and confirmation waits (`PROMPT_SETTLE_SECONDS`, `SUBMIT_CONFIRM_SECONDS`) summed over cards
were most of a slow tick (sprint:1484, ummanu-140, ummanu-142).

So a production handoff is resumable instead:

- The caller makes a `PromptHandoff` durable before the first effect: the journal sequence the head
  had reached (`floor`) and when the handoff was opened (`began_at`). Everything the handoff did is
  then read back from the head itself: the supervisor's `status` (a line or an Enter still being
  written) and, above the floor, its journal (the typed line, every submit, the output of the turn a
  submit opened). Nothing that evidence already proves is done twice.
- Every supervisor request the handoff makes (connect, `status`, admission, following an admitted
  write, settle, echo, submit, confirmation, a fatal close) runs inside one `HandoffOperation`. Its
  deadline is the card's share of the tick's `HandoffBudget`; the supervisor client is given it and
  recomputes what is left before every connect, send and receive of a framed exchange, attempting
  nothing once it is gone; and the time it really took is charged, so the tick's handoffs cost at
  most the allowance however many cards it serves. An allowance that runs out leaves the handoff pending at its stage; an admitted write is
  never cancelled by it, and the next tick continues the same handoff.
- The allowance is shared fairly: each card gets what is left divided by the cards not yet served
  (`HandoffBudget.card`), so a slow first card can never leave a later one nothing.
- Without an installed budget nothing changes: interactive callers keep the blocking flow.

Each operation leaves bounded telemetry entries (stage, actual milliseconds, the allowance it was
given, outcome) on the budget, which the production tick attaches to the card that caused them.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

#: How long, in total, one production tick may spend on head handoffs, across all of its cards.
HANDOFF_WAIT_BUDGET_SECONDS = 4.0
#: An effect (the line or an Enter) is started only with this much of the card's share left: the
#: admission answer and the start of the write must fit, or the effect waits for the next tick. It
#: only withholds an effect; it never grants time the share does not have.
HANDOFF_EFFECT_RESERVE_SECONDS = 0.5
#: Stage entries one tick keeps; the rest are counted, not kept.
HANDOFF_STAGES_KEPT = 32

#: Where a pending handoff stands. Settle: nothing typed yet, the head has not been seen quiet.
HANDOFF_SETTLE = "settle"
#: The line is in the composer, or still being written into it; no submit was sent.
HANDOFF_TYPED = "typed"
#: A submit was accepted (or is still being written) and its turn has not yet shown it took the prompt.
HANDOFF_SUBMITTED = "submitted"
HANDOFF_STAGES = (HANDOFF_SETTLE, HANDOFF_TYPED, HANDOFF_SUBMITTED)

#: The evidence key a pending handoff's stage travels under, on receipts and host errors alike.
HANDOFF_STAGE_KEY = "handoff_stage"

#: What a caller may learn of a handoff before acting on its head (`handoff_started`).
HANDOFF_NOT_STARTED = "none"
HANDOFF_STARTED = "started"
HANDOFF_UNKNOWN = "unknown"
#: The allowance ran out, or the supervisor did not answer within it: nothing is decided this tick.
HANDOFF_DEFERRED = "deferred"


@dataclass(frozen=True)
class PromptHandoff:
    """What a caller made durable before a production handoff's first effect.

    `floor` is the head's journal sequence before anything of this handoff was written: records
    above it belong to the handoff, records at or below it to whatever came before. `began_at` is
    the wall-clock time the caller opened it (the supervisor journals wall-clock time too), which
    bounds the settle wait the same way `PROMPT_SETTLE_SECONDS` bounds the blocking flow.
    """

    floor: int
    began_at: float

    def to_json(self) -> dict[str, Any]:
        return {"floor": self.floor, "began_at": self.began_at}

    @classmethod
    def from_json(cls, value: Any) -> PromptHandoff | None:
        if not isinstance(value, Mapping):
            return None
        floor, began_at = value.get("floor"), value.get("began_at")
        if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
            return None
        if isinstance(began_at, bool) or not isinstance(began_at, (int, float)) or began_at <= 0:
            return None
        return cls(floor=floor, began_at=float(began_at))


class HandoffBudget:
    """The seconds one tick may spend on head handoffs, how they are shared, and what each stage did.

    Everything an operation took is charged, waiting and supervisor requests alike. `cards` is how
    many cards the pass serves; `card()` gives each its share of what is left. The clock is monotonic
    and injectable, so a test drives the budget with a fake one.
    """

    def __init__(
        self, seconds: float, *, cards: int = 1, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.seconds = max(0.0, float(seconds))
        self.clock = clock
        self.cards = max(1, int(cards))
        self.served = 0
        self.spent = 0.0
        self.stages: list[dict[str, Any]] = []
        self.dropped = 0
        # The `spent` value at which the card being served has used its share; None between cards.
        self._share_ends_at: float | None = None
        self._operation: HandoffOperation | None = None

    def allowance(self) -> float:
        """How much is left for the rest of this tick."""
        return max(0.0, self.seconds - self.spent)

    def remaining(self) -> float:
        """How much the card being served may still spend: its share, never more than is left."""
        left = self.allowance()
        if self._share_ends_at is None:
            return left
        return max(0.0, min(left, self._share_ends_at - self.spent))

    @contextlib.contextmanager
    def card(self) -> Iterator[float]:
        """Serve one card: its share is what is left over the cards not yet served, this one included."""
        share = self.allowance() / max(1, self.cards - self.served)
        self.served += 1
        previous = self._share_ends_at
        self._share_ends_at = self.spent + share
        try:
            yield share
        finally:
            self._share_ends_at = previous

    def charge(self, seconds: float) -> None:
        self.spent += max(0.0, float(seconds))

    def note(self, subject: str, stage: str, ms: float, outcome: str, *, allowed_ms: float | None = None) -> None:
        if len(self.stages) >= HANDOFF_STAGES_KEPT:
            self.dropped += 1
            return
        entry: dict[str, Any] = {
            "subject": subject[:80],
            "stage": stage,
            "ms": round(max(0.0, ms), 3),
            "outcome": outcome,
        }
        if allowed_ms is not None:
            entry["allowed_ms"] = round(max(0.0, allowed_ms), 3)
        self.stages.append(entry)

    @contextlib.contextmanager
    def operation(self, subject: str) -> Iterator[HandoffOperation]:
        """One handoff call: a deadline cut from the card's share, and its real cost charged once.

        Nested calls (a floor read inside a delivery, say) join the operation already running, so
        nothing is charged twice and nothing gets a second deadline.
        """
        if self._operation is not None:
            yield self._operation
            return
        operation = HandoffOperation(self, subject)
        self._operation = operation
        try:
            yield operation
        finally:
            self._operation = None
            self.charge(self.clock() - operation.started)


class HandoffOperation:
    """One handoff call's deadline, and the stage entries it leaves.

    `remaining()` is what every socket bound and every wait inside the call is cut to. It reaches
    zero, and stays there, once the card's share is used.
    """

    def __init__(self, budget: HandoffBudget, subject: str) -> None:
        self.budget = budget
        self.subject = subject
        self.started = budget.clock()
        self.allowed = budget.remaining()
        self.deadline = self.started + self.allowed

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.budget.clock())

    def clock(self) -> float:
        return self.budget.clock()

    def note(self, stage: str, began: float, outcome: str) -> None:
        """Leave one stage entry: what it actually took, and what it was allowed when it began."""
        self.budget.note(
            self.subject,
            stage,
            (self.budget.clock() - began) * 1000.0,
            outcome,
            allowed_ms=max(0.0, self.deadline - began) * 1000.0,
        )


_BUDGET: contextvars.ContextVar[HandoffBudget | None] = contextvars.ContextVar("handoff_budget", default=None)


@contextlib.contextmanager
def handoff_budget(
    seconds: float = HANDOFF_WAIT_BUDGET_SECONDS,
    *,
    cards: int = 1,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[HandoffBudget]:
    """Make production handoffs resumable for the duration, sharing `seconds` over `cards` cards."""
    budget = HandoffBudget(seconds, cards=cards, clock=clock)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


@contextlib.contextmanager
def handoff_card() -> Iterator[None]:
    """Serve one card of the production pass its fair share; nothing outside a production pass."""
    budget = _BUDGET.get()
    if budget is None:
        yield
        return
    with budget.card():
        yield


def active_budget() -> HandoffBudget | None:
    """The budget of the production pass being served, or None for every other caller."""
    return _BUDGET.get()


def handoff_stages_since(mark: int) -> list[dict[str, Any]]:
    """The stage entries noted after `mark` entries, for the card that caused them."""
    budget = _BUDGET.get()
    return [] if budget is None else list(budget.stages[mark:])


def handoff_stage_mark() -> int:
    budget = _BUDGET.get()
    return 0 if budget is None else len(budget.stages)


def handoff_pending_stage(carrier: Any) -> str:
    """The stage a pending production handoff stopped at, or "" for anything else.

    Read off a receipt, an exception's `evidence`, or the evidence itself, as a dict or a typed
    record. Only the three pending stages count: an empty or unknown value is not pending.
    """
    stage = getattr(carrier, HANDOFF_STAGE_KEY, None)
    if not stage:
        evidence = getattr(carrier, "evidence", carrier)
        if hasattr(evidence, "to_json"):
            evidence = evidence.to_json()
        if isinstance(evidence, Mapping):
            stage = evidence.get(HANDOFF_STAGE_KEY)
        else:
            stage = getattr(evidence, HANDOFF_STAGE_KEY, "")
    stage = str(stage or "")
    return stage if stage in HANDOFF_STAGES else ""
