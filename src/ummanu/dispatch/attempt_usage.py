"""What one completed worker or review phase cost, read from the provider's own records.

The dispatcher already knows which run served a phase: the routing snapshot holds the launch
configuration and the lifecycle run holds the bound provider source. This module is the only place
that turns those two into a token account, and it is deliberately the whole adapter-specific part
of the feature — everything downstream of it is one typed board event.

Both providers write a journal that describes a *session*, not a phase, so the module reads a
session total and then subtracts the boundary the previous phase on that same session already made
durable. One rule for both adapters: a phase owns the usage after the previous authoritative terminal
boundary for its provider session and through its own terminal boundary, and a session nobody has
accounted for yet starts at zero. That is what keeps a retained worker — the same conversation
resumed for a second round — from being charged twice for its first round.

The two session shapes still differ in how the running total is obtained, and the difference matters:

* Codex writes cumulative ``token_count`` snapshots, but can reset those counters after a new user
  turn without changing the session or journal. The session total is the sum of each reset segment's
  last snapshot. Repeated snapshots within a segment still cost nothing.
* Claude writes one ``usage`` object per assistant message, so the session total is their sum. A
  streamed message is written more than once and a resumed session repeats earlier messages, so
  each message id contributes its last usage object exactly once.

A provider dimension is optional in a way a session total is not: Claude publishes its reasoning
subset in ``output_tokens_details``, and a real journal streams a detail-less record of a message
before the completed duplicate that carries it. A phase can therefore end inside that gap and see no
reasoning at all while the next phase on the same retained session sees the whole of it. So the
attribution rule is per dimension and lives in exactly one place, :func:`phase_interval`: absence of a
predecessor *occurrence* is not absence of one predecessor *dimension*, and neither is ever spelled
zero. What a phase cannot own it owns as ``null``, while the session total it did read stays on the
occurrence so the phase after it has a boundary to subtract from again.

The same records name the model and reasoning effort the CLI actually resolved
(:mod:`ummanu.runtime.provider_models`), which the occurrence carries beside the configured
``model``: the alias `opus` is what the head asked for, `claude-opus-5-5` is what ran.

Nothing here reads report or verdict prose, and nothing here decides anything: every failure is a
named degraded outcome, never an exception the caller has to survive.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ummanu.board.attempt_usage import AttemptUsagePayload, AttemptUsagePhase, TokenAccount
from ummanu.board.events import AttemptUsageOccurrence
from ummanu.board.models import TOKEN_DIMENSIONS, AttemptUsageOutcome
from ummanu.board.roles import Role
from ummanu.runtime.provider_models import (
    ProviderModels,
    claude_session_models,
    codex_session_models,
)

CODEX_ADAPTER = "codex"
CLAUDE_ADAPTER = "claude"
CODEX_SOURCE_KIND = "codex_session_event_jsonl"
CLAUDE_SOURCE_KIND = "claude_session_jsonl"
# Which fan-out policy key holds the structured record source for each adapter. The same split the
# routing journal uses to resolve a session id, so both read one run the same way.
SOURCE_POLICY_KEYS = {
    CODEX_ADAPTER: "provider_source",
    CLAUDE_ADAPTER: "provider_progress_source",
}
SOURCE_KINDS = {
    CODEX_ADAPTER: CODEX_SOURCE_KIND,
    CLAUDE_ADAPTER: CLAUDE_SOURCE_KIND,
}
# The Codex snapshot's own spelling of the dimensions it reports. Its input total contains its cache
# subcounts, which ``_canonical_codex_totals`` removes. Its output total is retained unchanged;
# reasoning is a contained subset used for analysis, not another additive cost bucket.
CODEX_TOKEN_FIELDS = {
    "input": "input_tokens",
    "cache_input": "cache_write_input_tokens",
    "cache_read_input": "cached_input_tokens",
    "output": "output_tokens",
    "reasoning": "reasoning_output_tokens",
}
# The Claude usage object's own spelling of the additive dimensions it reports. Thinking tokens live
# under ``output_tokens_details`` and are handled separately as a contained subset of output.
CLAUDE_TOKEN_FIELDS = {
    "input": "input_tokens",
    "cache_input": "cache_creation_input_tokens",
    "cache_read_input": "cache_read_input_tokens",
    "output": "output_tokens",
}


@dataclass(frozen=True)
class TokenTotals:
    """One account, with ``None`` for a dimension the provider does not report."""

    input: int | None = None
    cache_input: int | None = None
    cache_read_input: int | None = None
    output: int | None = None
    reasoning: int | None = None

    def to_json(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in TOKEN_DIMENSIONS}

    @property
    def empty(self) -> bool:
        return all(getattr(self, name) is None for name in TOKEN_DIMENSIONS)

    @classmethod
    def from_json(cls, source: Mapping[str, Any] | None) -> TokenTotals | None:
        """One recorded account, or ``None`` when the record carries no usable dimension."""
        if not isinstance(source, Mapping):
            return None
        values = {name: _count(source, name) for name in TOKEN_DIMENSIONS}
        if all(value is None for value in values.values()):
            return None
        return cls(**values)

    @classmethod
    def from_account(cls, account: TokenAccount) -> TokenTotals:
        return cls(**account.to_data())


@dataclass(frozen=True)
class SessionUsage:
    """What a provider journal says about the whole session it describes."""

    totals: TokenTotals = field(default_factory=TokenTotals)
    # How many usable structured usage records the aggregation counted.
    records: int = 0
    # How many records declared themselves usage records and carried no usable schema.
    invalid: int = 0


@dataclass(frozen=True)
class UsageCollection:
    """The result of asking one provider source what a finished phase cost."""

    outcome: AttemptUsageOutcome
    detail: str = ""
    source_kind: str = ""
    # How many provider usage records the aggregation actually counted, and how many source records
    # were unusable. Both are evidence about the read, not about the phase.
    records: int = 0
    skipped_records: int = 0
    # What the phase owns: the session total at its terminal boundary minus the boundary the
    # previous phase on this session already made durable.
    tokens: TokenTotals = field(default_factory=TokenTotals)
    # The session total this phase ends at, which is the next phase's starting boundary.
    session_totals: TokenTotals = field(default_factory=TokenTotals)
    # The boundary this phase started from: zero for a session nobody has accounted for yet, and
    # null for a dimension whose starting point no predecessor occurrence actually recorded.
    baseline: TokenTotals = field(default_factory=TokenTotals)
    # What the journal says actually ran, read from the same records whatever the token outcome:
    # a journal holding no usable usage record can still name its model.
    models: ProviderModels = field(default_factory=ProviderModels)

    @property
    def collected(self) -> bool:
        return self.outcome is AttemptUsageOutcome.COLLECTED


def provider_usage_source(lifecycle_run: Mapping[str, Any] | None, *, adapter: str) -> dict[str, Any]:
    """The adapter's structured record source held by one lifecycle run, or an empty mapping."""
    policy = lifecycle_run.get("fanout_policy") if isinstance(lifecycle_run, Mapping) else None
    if not isinstance(policy, Mapping):
        return {}
    source = policy.get(SOURCE_POLICY_KEYS.get(adapter, ""))
    return dict(source) if isinstance(source, Mapping) else {}


def causal_predecessor(
    occurrences: Sequence[AttemptUsageOccurrence],
    *,
    adapter: str,
    session_id: str,
    attempt: int,
    attempt_id: str,
    report_generation: int,
    phase: str,
    role: str,
) -> AttemptUsageOccurrence | None:
    """The occurrence that causally precedes this phase on this provider session, if there is one.

    Committed and staged occurrences have equal semantic authority. Their append order is irrelevant:
    the phase identity carried by each event decides which occurrence precedes this one. Selecting
    the predecessor is causal-identity validation and nothing else — it reads no provider journal and
    no token count — so it runs before the provider is read at all, and a phase slot already owned by
    a conflicting attempt id fails closed whatever that read would have said.
    """
    if not session_id:
        return None
    current_phase = AttemptUsagePhase(phase)
    current_role = Role(role)
    phase_order = {AttemptUsagePhase.WORKER: 0, AttemptUsagePhase.REVIEW: 1}
    current = (attempt, report_generation, phase_order[current_phase])
    predecessors: list[tuple[tuple[int, int, int], AttemptUsageOccurrence]] = []
    for occurrence in occurrences:
        payload = occurrence.payload
        if payload.role is not current_role or payload.phase is not current_phase:
            continue
        order = (
            payload.attempt,
            payload.report_generation,
            phase_order[payload.phase],
        )
        if order == current and payload.attempt_id != attempt_id:
            raise ValueError("current attempt usage phase belongs to a conflicting attempt id")
        if order < current and payload.adapter == adapter and payload.session_id == session_id:
            predecessors.append((order, occurrence))
    if not predecessors:
        return None
    _order, predecessor = max(predecessors, key=lambda item: item[0])
    return predecessor


def predecessor_boundary(predecessor: AttemptUsageOccurrence | None) -> TokenTotals | None:
    """The boundary one causal predecessor left behind, or ``None`` when there is no predecessor.

    A predecessor whose whole occurrence carries no session total is a degraded phase, not a session
    that starts over: reading it as ``None`` here would be indistinguishable from having no
    predecessor at all, so it is an audit failure rather than permission to restart at zero. A
    predecessor that recorded *some* dimensions and not others is a different thing entirely, and
    :func:`phase_interval` owns it dimension by dimension.
    """
    if predecessor is None:
        return None
    payload = predecessor.payload
    if payload.session_totals.empty:
        raise ValueError(
            "causal attempt usage predecessor has no readable session-total boundary "
            f"(attempt {payload.attempt}, generation {payload.report_generation})"
        )
    return TokenTotals.from_account(payload.session_totals)


@dataclass(frozen=True)
class PhaseAttribution:
    """The three accounts one phase publishes, or the contradiction that voids all three."""

    tokens: TokenTotals
    phase_baseline: TokenTotals
    session_totals: TokenTotals
    # Empty when the accounts are consistent; otherwise why they may not be published at all.
    contradiction: str = ""


def phase_interval(
    session_totals: TokenTotals,
    baseline: TokenTotals | None,
) -> PhaseAttribution:
    """The one phase-attribution rule, applied per dimension to every provider and lifecycle path.

    The lattice has one axis for the predecessor and one for each dimension, and the two are not the
    same question:

    * No predecessor occurrence at all. Every dimension this phase knows begins at a boundary of
      zero and the phase owns its whole current value, because nothing before it was ever accounted
      for. A dimension the provider did not report stays ``null`` — an unknown is not a zero.
    * A predecessor, and both its value and the current one are known and nondecreasing. The phase
      owns the difference, which is the ordinary retained-session case.
    * A predecessor, and either value is unavailable. The phase owns ``null`` for that dimension and
      no zero is invented in either account: it cannot be charged for work whose starting point
      nobody recorded, and the predecessor cannot be retroactively credited with a zero it never
      reported. The session total this phase actually read is kept exactly as read, so the dimension
      re-establishes a boundary here and the next phase subtracts from it normally.
    * A predecessor, and the current value is numerically below it. That is contradictory evidence
      about an immutable earlier occurrence, so the whole occurrence degrades and publishes no
      account rather than disguising the contradiction as a zero interval.

    Cross-account containment is checked here too, because it is a property of the attributed
    accounts rather than of either provider read: ``reasoning`` is a subset of ``output`` in each of
    the three accounts wherever both are known.
    """
    tokens: dict[str, int | None] = {}
    start: dict[str, int | None] = {}
    decreased: list[str] = []
    for name in TOKEN_DIMENSIONS:
        current = getattr(session_totals, name)
        previous = None if baseline is None else getattr(baseline, name)
        if current is None:
            # Nothing to own and nothing to say about where it would have started.
            tokens[name] = start[name] = None
        elif baseline is None:
            tokens[name], start[name] = current, 0
        elif previous is None:
            tokens[name] = start[name] = None
        elif current < previous:
            decreased.append(name)
            tokens[name] = start[name] = None
        else:
            tokens[name], start[name] = current - previous, previous
    attribution = PhaseAttribution(
        tokens=TokenTotals(**tokens),
        phase_baseline=TokenTotals(**start),
        session_totals=session_totals,
    )
    if decreased:
        return replace(
            attribution,
            contradiction=(
                "the provider session total fell below the authoritative boundary of an earlier "
                f"phase in {', '.join(decreased)}"
            ),
        )
    uncontained = _uncontained_reasoning(attribution)
    if uncontained:
        return replace(attribution, contradiction=uncontained)
    return attribution


def apply_phase_boundary(
    collection: UsageCollection,
    baseline: TokenTotals | None,
) -> UsageCollection:
    """Apply one projected predecessor to an already-read provider session total.

    Keeping this separate from the provider read preserves failure precedence without rereading the
    provider journal: a degraded current read needs no arithmetic, while a collected one must have
    its causal predecessor resolved before it becomes the phase occurrence.
    """
    if not collection.collected:
        return collection
    attribution = phase_interval(collection.session_totals, baseline)
    if attribution.contradiction:
        return replace(
            collection,
            outcome=AttemptUsageOutcome.ARITHMETIC_CONTRADICTION,
            detail=attribution.contradiction,
            tokens=TokenTotals(),
            session_totals=TokenTotals(),
            baseline=TokenTotals(),
        )
    return replace(
        collection,
        tokens=attribution.tokens,
        session_totals=attribution.session_totals,
        baseline=attribution.phase_baseline,
    )


def attribute_phase(
    collection: UsageCollection,
    predecessor: AttemptUsageOccurrence | None,
) -> UsageCollection:
    """Turn one provider read plus its already-selected causal predecessor into the three accounts.

    The single attribution site every lifecycle path goes through: first phase and retained phase,
    Codex and Claude, a live acceptance and a replayed one. A degraded read is returned untouched, so
    the predecessor's boundary is not even consulted for a phase that has no interval to compute.
    """
    if not collection.collected:
        return collection
    return apply_phase_boundary(collection, predecessor_boundary(predecessor))


def _uncontained_reasoning(attribution: PhaseAttribution) -> str:
    """The first account whose reasoning escapes its own output, or an empty string."""
    accounts = (
        ("tokens", attribution.tokens),
        ("phase_baseline", attribution.phase_baseline),
        ("session_totals", attribution.session_totals),
    )
    for name, account in accounts:
        if account.output is None or account.reasoning is None:
            continue
        if account.reasoning > account.output:
            return f"the {name} reasoning is not contained in its own output total"
    return ""


def collect_usage(*, adapter: str, source: Mapping[str, Any] | None) -> UsageCollection:
    """Read one finished phase's usage out of its bound provider source.

    Collection is the provider read and only the provider read: what comes back is the whole session
    total this phase ended at, with no boundary subtracted and no account attributed. Attribution is
    :func:`attribute_phase`, one step later and in one place, so no adapter path can grow arithmetic
    of its own.

    Never raises: every way this can fail is one of the declared degraded outcomes, because the
    caller runs on the path that accepts a worker report or a reviewer verdict.
    """
    kind = SOURCE_KINDS.get(adapter, "")
    if not kind:
        return UsageCollection(
            AttemptUsageOutcome.ADAPTER_UNSUPPORTED,
            detail=f"adapter {adapter or 'unknown'} publishes no structured usage records",
        )
    source = dict(source or {})
    state = str(source.get("state") or "")
    if str(source.get("kind") or "") != kind or state != "bound":
        return UsageCollection(
            AttemptUsageOutcome.SOURCE_UNAVAILABLE,
            detail=f"{adapter} structured record source is {state or 'absent'} for this run",
            source_kind=kind,
        )
    if not str(source.get("session_id") or ""):
        return UsageCollection(
            AttemptUsageOutcome.SESSION_UNAVAILABLE,
            detail=f"{adapter} bound source carries no provider session id",
            source_kind=kind,
        )
    path = str(source.get("path") or "")
    if not path:
        return UsageCollection(
            AttemptUsageOutcome.SOURCE_UNAVAILABLE,
            detail=f"{adapter} bound source names no journal path",
            source_kind=kind,
        )
    try:
        records, unparsed = read_jsonl(Path(path))
    except OSError as exc:
        return UsageCollection(
            AttemptUsageOutcome.SOURCE_UNREADABLE,
            detail=f"{adapter} session journal could not be read: {exc.strerror or exc}",
            source_kind=kind,
        )
    aggregate = codex_usage if adapter == CODEX_ADAPTER else claude_usage
    session = aggregate(records)
    models = (codex_session_models if adapter == CODEX_ADAPTER else claude_session_models)(records)
    skipped = unparsed + session.invalid
    if session.records:
        return UsageCollection(
            AttemptUsageOutcome.COLLECTED,
            source_kind=kind,
            records=session.records,
            skipped_records=skipped,
            session_totals=session.totals,
            models=models,
        )
    if session.invalid:
        return UsageCollection(
            AttemptUsageOutcome.RECORDS_MALFORMED,
            detail=(
                f"{session.invalid} {adapter} record(s) declare a usage record whose schema is not "
                "the one this adapter publishes, and none were usable"
            ),
            source_kind=kind,
            skipped_records=skipped,
            models=models,
        )
    if unparsed and not records:
        return UsageCollection(
            AttemptUsageOutcome.RECORDS_MALFORMED,
            detail=f"{unparsed} {adapter} session record(s) could not be parsed and none were usable",
            source_kind=kind,
            skipped_records=skipped,
            models=models,
        )
    return UsageCollection(
        AttemptUsageOutcome.USAGE_ABSENT,
        detail=f"{adapter} session journal carries no usage record for this phase",
        source_kind=kind,
        skipped_records=skipped,
        models=models,
    )


def read_jsonl(path: Path) -> tuple[list[Any], int]:
    """Parse one provider JSONL file, returning its records and how many lines would not parse.

    A truncated final line — the normal shape of a journal read while its writer is still around —
    is one skipped line, not a failed read: the records before it are complete records.
    """
    records: list[Any] = []
    skipped = 0
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    return records, skipped


def codex_usage(records: Iterable[Any]) -> SessionUsage:
    """Aggregate cumulative Codex snapshots into a monotone whole-session total.

    Counters are cumulative only until Codex starts a new reset segment, which can happen after a
    new user turn in the same session and rollout. A decrease in any reported raw counter closes the
    prior segment; summing segment endpoints produces a monotone session total. Equal/replayed
    snapshots remain in one segment. The canonical buckets are derived at each endpoint because
    Codex's raw input includes cache counts and raw output includes reasoning counts.
    """
    latest: dict[str, Any] | None = None
    completed: list[TokenTotals] = []
    seen = 0
    invalid = 0
    for record in records:
        snapshot, malformed = _codex_snapshot(record)
        if snapshot is None:
            invalid += malformed
            continue
        canonical = _canonical_codex_totals(snapshot)
        if canonical is None:
            invalid += 1
            continue
        if latest is not None and _codex_snapshot_reset(latest, snapshot):
            endpoint = _canonical_codex_totals(latest)
            if endpoint is None:  # Every assigned ``latest`` was validated above.
                invalid += 1
            else:
                completed.append(endpoint)
        seen += 1
        latest = snapshot
    if latest is None:
        return SessionUsage(invalid=invalid)
    endpoint = _canonical_codex_totals(latest)
    if endpoint is None:
        return SessionUsage(invalid=invalid + 1)
    completed.append(endpoint)
    return SessionUsage(
        totals=_sum_token_totals(completed),
        records=seen,
        invalid=invalid,
    )


def claude_usage(records: Iterable[Any]) -> SessionUsage:
    """Aggregate Claude assistant ``usage`` objects, which are per message and additive.

    A message id contributes once: its last usage object, because a streamed message is written
    repeatedly and only the final record carries the finished counts. A dimension no counted message
    reported stays unavailable, and an assistant record whose declared ``usage`` is not the object
    this adapter publishes is a malformed structured record rather than a message that cost nothing.

    ``output_tokens_details`` is optional metadata about an otherwise valid usage object, so it can
    only ever cost the reasoning subset. A message whose detail is missing, malformed or out of range
    still contributes its input, cache and total-output counts; it makes aggregate ``reasoning``
    unavailable for the session and is reported as a skipped record. Malformed *core* usage is the
    documented malformed outcome and is unchanged: there, the whole record carries nothing usable.
    """
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    invalid = 0
    for index, record in enumerate(records):
        usage, malformed = _claude_usage_object(record)
        if usage is None:
            invalid += malformed
            continue
        key = _claude_message_key(record, index)
        if key not in latest:
            order.append(key)
        latest[key] = usage
    if not order:
        return SessionUsage(invalid=invalid)
    sums: dict[str, int | None] = dict.fromkeys(CLAUDE_TOKEN_FIELDS, None)
    reasoning = 0
    reasoning_available = True
    seen = 0
    for key in order:
        usage = latest[key]
        thinking, malformed = _claude_thinking_tokens(usage)
        if malformed:
            # Optional reasoning metadata may not discard usage the provider did report.
            invalid += 1
        for name, provider_field in CLAUDE_TOKEN_FIELDS.items():
            value = _count(usage, provider_field)
            if value is None:
                continue
            sums[name] = value if sums[name] is None else (sums[name] or 0) + value
        seen += 1
        if thinking is None:
            reasoning_available = False
        else:
            reasoning += thinking
    if not seen:
        return SessionUsage(invalid=invalid)
    return SessionUsage(
        totals=TokenTotals(
            **sums,
            reasoning=reasoning if reasoning_available else None,
        ),
        records=seen,
        invalid=invalid,
    )


def attempt_usage_data(
    *,
    attempt: int,
    attempt_id: str,
    phase: str,
    role: str,
    report_generation: int,
    head: str,
    adapter: str,
    model: str,
    model_source: str,
    session_id: str | None,
    session_id_reason: str,
    launch_id: str,
    collection: UsageCollection,
) -> dict[str, Any]:
    """Project one typed ``attempt.usage`` payload onto the released event-data dictionary."""
    payload = AttemptUsagePayload(
        attempt=int(attempt),
        attempt_id=attempt_id,
        phase=AttemptUsagePhase(phase),
        role=Role(role),
        report_generation=int(report_generation),
        head=head,
        adapter=adapter,
        model=model,
        model_source=model_source,
        session_id=session_id or None,
        session_id_reason=session_id_reason,
        launch_id=launch_id,
        outcome=AttemptUsageOutcome(collection.outcome),
        detail=collection.detail,
        source_kind=collection.source_kind,
        records=int(collection.records),
        skipped_records=int(collection.skipped_records),
        tokens=TokenAccount.from_data(collection.tokens.to_json()),
        session_totals=TokenAccount.from_data(collection.session_totals.to_json()),
        phase_baseline=TokenAccount.from_data(collection.baseline.to_json()),
        resolved_model=collection.models.model,
        resolved_models=collection.models.models,
        resolved_effort=collection.models.effort,
    )
    return payload.to_data()


def attempt_usage_reason(data: Mapping[str, Any]) -> str:
    """One line of prose for the journal's ``reason`` field, derived from the typed payload."""
    return AttemptUsagePayload.from_data(data).reason()


def _codex_snapshot(record: Any) -> tuple[dict[str, Any] | None, bool]:
    """One session total, and whether the record declared a total it did not actually carry."""
    if not isinstance(record, dict):
        return None, False
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None, False
    info = payload.get("info")
    holder = info if isinstance(info, dict) else payload
    total = holder.get("total_token_usage") if isinstance(holder, dict) else None
    if not isinstance(total, dict):
        return None, True
    if not any(_count(total, field) is not None for field in CODEX_TOKEN_FIELDS.values()):
        return None, True
    return dict(total), False


def _canonical_codex_totals(snapshot: Mapping[str, Any]) -> TokenTotals | None:
    """Convert one Codex endpoint to the canonical protocol dimensions.

    Codex defines both cache counts inside ``input_tokens`` and reasoning inside ``output_tokens``.
    Cache counts are split out of input, while output stays inclusive and reasoning remains its
    contained subset. All five raw fields and valid containment are required for a usable snapshot.
    """
    raw = {name: _count(snapshot, field) for name, field in CODEX_TOKEN_FIELDS.items()}
    if any(value is None for value in raw.values()):
        return None
    values = {name: int(value) for name, value in raw.items()}
    uncached = values["input"] - values["cache_input"] - values["cache_read_input"]
    if uncached < 0 or values["reasoning"] > values["output"]:
        return None
    return TokenTotals(
        input=uncached,
        cache_input=values["cache_input"],
        cache_read_input=values["cache_read_input"],
        output=values["output"],
        reasoning=values["reasoning"],
    )


def _codex_snapshot_reset(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Whether a later usable snapshot starts a new cumulative-counter segment."""
    return any(
        current_value < previous_value
        for field in CODEX_TOKEN_FIELDS.values()
        if (previous_value := _count(previous, field)) is not None
        and (current_value := _count(current, field)) is not None
    )


def _sum_token_totals(parts: Sequence[TokenTotals]) -> TokenTotals:
    """Sum segment endpoints, keeping a dimension unavailable if any segment lacks it."""
    totals: dict[str, int | None] = {}
    for name in TOKEN_DIMENSIONS:
        values = [getattr(part, name) for part in parts]
        totals[name] = (
            None
            if any(value is None for value in values)
            else sum(value for value in values if value is not None)
        )
    return TokenTotals(**totals)


def _claude_usage_object(record: Any) -> tuple[dict[str, Any] | None, bool]:
    """One message's usage object, and whether the record declared one it did not actually carry."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None, False
    message = record.get("message")
    if not isinstance(message, dict) or "usage" not in message:
        return None, False
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None, True
    if not any(_count(usage, field) is not None for field in CLAUDE_TOKEN_FIELDS.values()):
        return None, True
    return dict(usage), False


def _claude_thinking_tokens(usage: Mapping[str, Any]) -> tuple[int | None, bool]:
    """One deduplicated message's reasoning subset and whether its detail is malformed.

    An omitted detail makes aggregate reasoning unavailable without losing the known total output,
    and so does a malformed one: the caller keeps this message's additive counts either way. A
    present detail must be a non-negative integer contained in that message's output total.
    """
    details = usage.get("output_tokens_details")
    if details is None:
        return None, False
    if not isinstance(details, Mapping):
        return None, True
    if "thinking_tokens" not in details:
        return None, False
    thinking = _count(details, "thinking_tokens")
    output = _count(usage, "output_tokens")
    if thinking is None or output is None or thinking > output:
        return None, True
    return thinking, False


def _claude_message_key(record: Mapping[str, Any], index: int) -> str:
    """The occurrence key of one assistant record: its message, else the record, else its line."""
    message = record.get("message")
    identifier = str(message.get("id") or "") if isinstance(message, Mapping) else ""
    if identifier:
        return f"message:{identifier}"
    record_id = str(record.get("uuid") or "")
    if record_id:
        return f"record:{record_id}"
    return f"line:{index}"


def _count(source: Mapping[str, Any], name: str) -> int | None:
    """One provider count, or ``None`` when it is absent or is not a usable non-negative integer."""
    value = source.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
