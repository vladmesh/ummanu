"""Production prompt handoffs: the tick's shared wait budget and the cursor a handoff resumes from.

A prompt for an agent's composer is typed, waited on until its echo turn closes, submitted, and
seen to start a turn (`LocalPtyHeadRuntime._deliver_prompt`). Interactive callers wait for all of
it. The production dispatcher cannot: one reconcile pass serves every active card, and the settle,
echo and confirmation waits (`PROMPT_SETTLE_SECONDS`, `SUBMIT_CONFIRM_SECONDS`) summed over cards
were most of a slow tick (sprint:1484, ummanu-140).

So a production handoff is resumable instead:

- The caller makes a `PromptHandoff` durable before the first effect: the journal sequence the head
  had reached (`floor`) and when the handoff was opened (`began_at`). Everything the handoff did is
  then read back from the head's own journal above that floor: the typed line, every submit, and
  the output of the turn a submit opened. Nothing the journal already proves is done twice.
- Every stage is decided by one observation. A stage whose condition is not yet true may wait only
  out of the `HandoffBudget` the production tick installs around its advance pass; that budget is
  shared by every card of the tick, so a tick never sums one card's wait with another's. A stage the
  budget cannot finish is reported pending, and the next tick continues the same handoff.
- Without an installed budget nothing changes: interactive callers keep the blocking flow.

Each stage leaves a bounded telemetry entry (stage, milliseconds, outcome) on the budget, which the
production tick attaches to the card that caused it.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

#: How long, in total, one production tick may wait on head handoffs, across all of its cards.
HANDOFF_WAIT_BUDGET_SECONDS = 4.0
#: Stage entries one tick keeps; the rest are counted, not kept.
HANDOFF_STAGES_KEPT = 32

#: Where a pending handoff stands. Settle: nothing typed yet, the head has not been seen quiet.
HANDOFF_SETTLE = "settle"
#: The line is in the composer; its echo turn has not closed, so no submit was sent.
HANDOFF_TYPED = "typed"
#: A submit was accepted and the turn it opened has not yet shown that it took the prompt.
HANDOFF_SUBMITTED = "submitted"
HANDOFF_STAGES = (HANDOFF_SETTLE, HANDOFF_TYPED, HANDOFF_SUBMITTED)

#: The evidence key a pending handoff's stage travels under, on receipts and host errors alike.
HANDOFF_STAGE_KEY = "handoff_stage"


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
    """The seconds one tick may spend waiting on head handoffs, and what each stage did.

    Only time spent waiting is charged; a stage decided by its first observation costs nothing.
    The clock is monotonic and injectable, so a test drives the budget with a fake one.
    """

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = max(0.0, float(seconds))
        self.clock = clock
        self.spent = 0.0
        self.stages: list[dict[str, Any]] = []
        self.dropped = 0

    def allowance(self) -> float:
        """How much waiting is left for the rest of this tick."""
        return max(0.0, self.seconds - self.spent)

    def charge(self, seconds: float) -> None:
        self.spent += max(0.0, float(seconds))

    def note(self, subject: str, stage: str, ms: float, outcome: str) -> None:
        if len(self.stages) >= HANDOFF_STAGES_KEPT:
            self.dropped += 1
            return
        self.stages.append(
            {"subject": subject[:80], "stage": stage, "ms": round(max(0.0, ms), 3), "outcome": outcome}
        )


_BUDGET: contextvars.ContextVar[HandoffBudget | None] = contextvars.ContextVar("handoff_budget", default=None)


@contextlib.contextmanager
def handoff_budget(
    seconds: float = HANDOFF_WAIT_BUDGET_SECONDS, *, clock: Callable[[], float] = time.monotonic
) -> Iterator[HandoffBudget]:
    """Make production handoffs resumable for the duration, sharing `seconds` of waiting."""
    budget = HandoffBudget(seconds, clock=clock)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


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
