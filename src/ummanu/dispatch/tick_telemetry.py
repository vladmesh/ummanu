"""Bounded, read-only interpretation of the production tick's durable measurements."""

from __future__ import annotations

import contextlib
import contextvars
import math
import time
from collections.abc import Iterator
from typing import Any

TICK_TELEMETRY_RECENT_KEPT = 100
TICK_P95_MIN_SAMPLES = 20
TICK_P95_THRESHOLD_MS = 300_000
# Reject corrupt measurements beyond a year, including integers too large to convert to float.
MAX_DURATION_MS = 366 * 24 * 60 * 60 * 1000
#: The durable writes a tick made, counted at their publication seams once they succeeded.
TICK_COUNTERS = ("save_records", "cleanup_intent_writes", "cleanup_bytes_written", "production_state_saves")
#: The reconcile pass as exclusive sub-phases; their sum is the aggregate reconcile cost.
RECONCILE_PHASES = ("fence", "reconcile_production", "advance_active")
#: Per-card advance details kept on the last and unhealthy entries, slowest first; never in the ring.
TICK_CARDS_KEPT = 10
#: Head handoff stages (`runtime.head.handoff`) read back per card.
TICK_CARD_HANDOFFS_KEPT = 12
MAX_COUNTER = 2**53
#: Where one card's advance spent its time (`tick_stage`), each millisecond in exactly one stage: a
#: stage nested in another is taken out of its parent. A closed vocabulary, bounded per card:
#:   board_read      the fresh card read the advance starts from
#:   ingress         re-establishing and polling a run's provider event source
#:   launch          settling a launch intent, launching or recovering a head
#:   report          the worker report, its continuation, and the review verdict
#:   headless        deciding whether the worker has a head at all
#:   comments        delivering comments to a live worker
#:   gate            the mechanical gate before a review
#:   observation     the host's status of a waited-on head (heartbeat, terminal, provider, /proc)
#:   provider        the persisted provider failure that outranks a stall reading
#:   vitality        reducing the vitality episode and commenting a verdict change
#:   lifecycle       the wait decision (wait, nudge, recover, escalate) and a verdict parked unreviewed
#:   flush           a record flush (`save_records`) outside the parts below
#:   flush_card_read the card reads the cleanup projection of a flush needs
#:   flush_identity  the workspace identity of the cleanup projection
#:   flush_journal   publishing a changed cleanup projection into the journal
#:   flush_lock      waiting for the ownership lock, in the flush and in its journal publication
#:   flush_write     putting the records into the production state and writing it
#: The card's `ms` less all of them is `unclassified`, measured, never dropped.
CARD_STAGES = (
    "board_read", "ingress", "launch", "report", "headless", "comments", "gate", "observation",
    "provider", "vitality", "lifecycle", "flush", "flush_card_read", "flush_identity", "flush_journal",
    "flush_lock", "flush_write",
)
CARD_STAGE_REMAINDER = "unclassified"

_COUNTERS: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar("tick_counters", default=None)
_STAGES: contextvars.ContextVar[tuple[dict[str, float], list[list[float]]] | None] = contextvars.ContextVar(
    "card_stages", default=None
)


@contextlib.contextmanager
def tick_counting() -> Iterator[dict[str, int]]:
    """Count this tick's writes; outside it `tick_count` is a no-op."""
    counters = {name: 0 for name in TICK_COUNTERS}
    token = _COUNTERS.set(counters)
    try:
        yield counters
    finally:
        _COUNTERS.reset(token)


def tick_count(name: str, amount: int = 1) -> None:
    counters = _COUNTERS.get()
    if counters is not None:
        counters[name] = counters.get(name, 0) + amount


def tick_counter_values() -> dict[str, int] | None:
    counters = _COUNTERS.get()
    return None if counters is None else {name: int(counters.get(name, 0)) for name in TICK_COUNTERS}


@contextlib.contextmanager
def card_staging() -> Iterator[dict[str, float]]:
    """Accumulate one card's exclusive stage milliseconds; outside it `tick_stage` is a no-op."""
    stages: dict[str, float] = {}
    token = _STAGES.set((stages, []))
    try:
        yield stages
    finally:
        _STAGES.reset(token)


@contextlib.contextmanager
def tick_stage(name: str) -> Iterator[None]:
    """Time a stage of the card being advanced, exclusive of the stages nested in it.

    The time of a stage interrupted by an exception is kept, and the exception passes unchanged.
    Outside a card, or for a name outside `CARD_STAGES`, nothing is read or kept.
    """
    held = _STAGES.get()
    if held is None or name not in CARD_STAGES:
        yield
        return
    stages, stack = held
    frame = [time.perf_counter(), 0.0]
    stack.append(frame)
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - frame[0]) * 1000.0
        stack.pop()
        stages[name] = stages.get(name, 0.0) + max(0.0, elapsed - frame[1])
        if stack:
            stack[-1][1] += elapsed


@contextlib.contextmanager
def tick_stage_entering(name: str, manager: contextlib.AbstractContextManager[Any]) -> Iterator[None]:
    """Enter `manager` (a lock) with its entry timed as stage `name`; the body is not part of it."""
    with contextlib.ExitStack() as stack:
        with tick_stage(name):
            stack.enter_context(manager)
        yield


def stage_breakdown(stages: dict[str, float], card_ms: float) -> dict[str, float]:
    """The card's stages rounded, with `unclassified` the rest of its inclusive `card_ms`.

    The stages and the remainder sum to `card_ms` within 0.001 ms per stage: rounding that would
    overshoot is taken off the largest stage, as the tick's phases do it.
    """
    breakdown = {name: round(stages[name], 3) for name in CARD_STAGES if name in stages}
    excess = round(sum(breakdown.values()) - card_ms, 3)
    if excess > 0 and breakdown:
        largest = max(breakdown, key=lambda name: breakdown[name])
        breakdown[largest] = round(max(0.0, breakdown[largest] - excess), 3)
    breakdown[CARD_STAGE_REMAINDER] = round(max(0.0, card_ms - sum(breakdown.values())), 3)
    return breakdown


def card_stage_ms(value: Any) -> dict[str, float] | None:
    """A card's recorded stages: known names with valid durations, or None when none survive."""
    if not isinstance(value, dict):
        return None
    stages = {
        name: measured
        for name in (*CARD_STAGES, CARD_STAGE_REMAINDER)
        if (measured := duration_ms(value.get(name))) is not None
    }
    return stages or None


def counter_values(value: Any) -> dict[str, int] | None:
    """Accept only the named nonnegative integer counters; anything else is dropped."""
    if not isinstance(value, dict):
        return None
    return {
        name: raw
        for name in TICK_COUNTERS
        if not isinstance(raw := value.get(name), bool) and isinstance(raw, int) and 0 <= raw <= MAX_COUNTER
    }


def reconcile_ms(phases: dict[str, float] | None) -> float | None:
    """The aggregate reconcile cost, from its sub-phases or the single phase older ticks recorded."""
    if not phases:
        return None
    names = [name for name in ("reconcile", *RECONCILE_PHASES) if name in phases]
    return round(sum(phases[name] for name in names), 3) if names else None


def card_details(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    cards = []
    for item in value[:TICK_CARDS_KEPT]:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            continue
        records = item.get("records")
        handoffs = handoff_stages(item.get("handoffs"))
        handoff_ms = duration_ms(item.get("handoff_ms"))
        stages = card_stage_ms(item.get("stages"))
        cards.append({"ref": item["ref"][:200], "ms": duration_ms(item.get("ms")),
                      "records": records if isinstance(records, int) and not isinstance(records, bool)
                      and 0 <= records <= MAX_COUNTER else None,
                      **(counter_values(item) or {}),
                      **({"handoff_ms": handoff_ms} if handoff_ms is not None else {}),
                      **({"handoffs": handoffs} if handoffs else {}),
                      # Absent on a card recorded before stages existed, or with none valid: unknown.
                      **({"stages": stages} if stages else {})})
    return cards


#: The counts of a cleanup replay's tick summary: due at selection, reserved attempts, attempts the
#: allowance cut short, due intents it did not admit, intents whose lifecycle lane was busy,
#: reservations lost to another owner, and intents whose eligibility the selection did not read
#: (it had its limit of due intents, or the allowance ended it).
CLEANUP_COUNTS = ("due", "attempted", "deferred", "skipped", "busy", "lost", "unread")
#: The journal publications of the invocation, by file class, and their bytes.
CLEANUP_WRITES = ("intent", "meta", "generated", "bytes")


def cleanup_summary(value: Any) -> dict[str, Any]:
    """A cleanup replay's tick summary (`CleanupOwner.last_replay`): named counts, bounded strings."""
    if not isinstance(value, dict):
        return {}
    summary: dict[str, Any] = {}
    for name in CLEANUP_COUNTS:
        raw = value.get(name)
        if not isinstance(raw, bool) and isinstance(raw, int) and 0 <= raw <= MAX_COUNTER:
            summary[name] = raw
    for name in ("allowance_ms", "spent_ms"):
        measured = duration_ms(value.get(name))
        if measured is not None:
            summary[name] = measured
    if isinstance(value.get("deferred_at"), str) and value["deferred_at"]:
        summary["deferred_at"] = value["deferred_at"][:200]
    if value.get("cursor") in ("advanced", "unchanged", "unwritten"):
        summary["cursor"] = value["cursor"]
    writes = value.get("writes")
    if isinstance(writes, dict):
        summary["writes"] = {name: raw for name in CLEANUP_WRITES
                             if not isinstance(raw := writes.get(name), bool) and isinstance(raw, int)
                             and 0 <= raw <= MAX_COUNTER}
    return summary


def handoff_stages(value: Any) -> list[dict[str, Any]]:
    """A card's head handoff stages: stage, subject, outcome and a valid duration, bounded."""
    if not isinstance(value, list):
        return []
    stages = []
    for item in value[:TICK_CARD_HANDOFFS_KEPT]:
        if not isinstance(item, dict) or not isinstance(item.get("stage"), str):
            continue
        allowed = duration_ms(item.get("allowed_ms"))
        stages.append({"stage": item["stage"][:20], "subject": str(item.get("subject") or "")[:80],
                       "ms": duration_ms(item.get("ms")), "outcome": str(item.get("outcome") or "")[:40],
                       # The allowance the stage began with: the nominal bound, beside what it took.
                       **({"allowed_ms": allowed} if allowed is not None else {})})
    return stages


def duration_ms(value: Any) -> float | None:
    """Accept only finite, nonnegative JSON numbers in the measurement domain."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= MAX_DURATION_MS:
        return None
    return float(value)


def phase_ms(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    return {
        name: measured
        for name, raw in value.items()
        if isinstance(name, str) and (measured := duration_ms(raw)) is not None
    }


def tick_statistics(production: dict[str, Any]) -> dict[str, Any]:
    """Nearest-rank p50/p95 over valid durations in the last 100 stored entries."""
    telemetry = production.get("tick_telemetry")
    recent = telemetry.get("recent") if isinstance(telemetry, dict) else None
    durations = sorted(
        measured
        for entry in (recent[-TICK_TELEMETRY_RECENT_KEPT:] if isinstance(recent, list) else [])
        if isinstance(entry, dict) and (measured := duration_ms(entry.get("duration_ms"))) is not None
    )
    count = len(durations)
    return {
        "sample_count": count,
        "p50_duration_ms": durations[math.ceil(count * 0.50) - 1] if count else None,
        "p95_duration_ms": durations[math.ceil(count * 0.95) - 1] if count else None,
    }


def tick_p95_finding(production: dict[str, Any]) -> dict[str, Any] | None:
    statistics = tick_statistics(production)
    count = statistics["sample_count"]
    p95 = statistics["p95_duration_ms"]
    if count < TICK_P95_MIN_SAMPLES or p95 <= TICK_P95_THRESHOLD_MS:
        return None
    return {
        "code": "dispatcher_tick_p95_slow",
        "severity": "red",
        "message": f"production tick p95 {p95:g} ms over {count} samples exceeds threshold {TICK_P95_THRESHOLD_MS} ms",
    }
