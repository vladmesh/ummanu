"""What one finished phase cost, and what recording it must never do to the card.

Three contracts live here. The provider aggregations, because "how many tokens" is the one thing a
reader cannot recompute later from the journal and a wrong rule would be invisible. The typed
event, because the point of the record is that nobody has to reopen a session file to read it. And
the dispatcher seam, because the accounting runs on the path that accepts a worker report or a
reviewer verdict and must not be able to change what that path does.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.fakes.dispatcher import dispatcher_seed
from tests.sql_backend_fixtures import card_store
from ummanu.board.events import AttemptUsageOccurrence, BoardEventCanon
from ummanu.board.models import (
    TOKEN_DIMENSIONS,
    Actor,
    AttemptUsageOutcome,
    EntityKind,
    Event,
    EventKind,
)
from ummanu.dispatch import attempt_usage as attempt_usage_module
from ummanu.dispatch.attempt_usage import (
    CLAUDE_SOURCE_KIND,
    CODEX_SOURCE_KIND,
    TokenTotals,
    apply_phase_boundary,
    causal_predecessor,
    claude_usage,
    codex_usage,
    collect_usage,
    phase_interval,
    predecessor_boundary,
    provider_usage_source,
)
from ummanu.tasks import TaskError, is_significant_card_event, task_audit_for

DIGEST = "a" * 64
USAGE_FIXTURES = Path(__file__).parent / "fixtures" / "attempt_usage"
# The three accounts one occurrence carries, in the event's own spelling.
ACCOUNTS = ("tokens", "session_totals", "phase_baseline")


def codex_token_count(
    *,
    input_tokens: int | None = None,
    cached_input_tokens: int | None = 0,
    cache_write_input_tokens: int | None = 0,
    output_tokens: int | None = None,
    reasoning_output_tokens: int | None = 0,
) -> dict:
    """One Codex ``token_count`` event carrying the session's running total.

    The field names are the ones a real rollout writes, including both cache sides.
    """
    total = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
    }
    return {
        "timestamp": "2026-08-31T12:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {name: value for name, value in total.items() if value is not None},
                "model_context_window": 272000,
            },
        },
    }


def claude_assistant(message_id: str, **usage: object) -> dict:
    """One Claude assistant record carrying that message's usage object."""
    return {
        "type": "assistant",
        "uuid": f"record-{message_id}",
        "message": {"id": message_id, "role": "assistant", "usage": dict(usage)},
    }


def bound_codex_source(path: Path, *, session_id: str = "codex-session-1") -> dict:
    """A completely bound Codex source, in the shape the runtime persists and re-validates."""
    return {
        "version": 1,
        "kind": CODEX_SOURCE_KIND,
        "state": "bound",
        "root": str(path.parent),
        "path": str(path),
        "session_id": session_id,
        "parent_thread_id": "thread-1",
        "cursor": {"line": 0, "digest": DIGEST},
        "initial_range": {
            "first": {"line": 1, "digest": DIGEST},
            "root": {"line": 1, "digest": DIGEST},
            "last": {"line": 2, "digest": DIGEST},
            "digest": DIGEST,
        },
        "bound_at": "2026-08-31T12:00:00Z",
    }


def bound_claude_source(path: Path, *, session_id: str = "claude-session-1") -> dict:
    return {
        "version": 1,
        "kind": CLAUDE_SOURCE_KIND,
        "state": "bound",
        "root": str(path.parent),
        "path": str(path),
        "session_id": session_id,
    }


def write_jsonl(path: Path, records: list, *, tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(record) + "\n" for record in records) + tail
    path.write_text(body, encoding="utf-8")
    return path


class CodexAggregationTests(unittest.TestCase):
    """Codex snapshot segments produce one monotone canonical session account."""

    def test_the_last_cumulative_snapshot_is_the_whole_session(self) -> None:
        session = codex_usage(
            [
                codex_token_count(input_tokens=100, output_tokens=10, reasoning_output_tokens=4),
                codex_token_count(input_tokens=450, output_tokens=61, reasoning_output_tokens=25),
            ]
        )

        self.assertEqual(session.records, 2)
        self.assertEqual(session.totals.input, 450)
        self.assertEqual(session.totals.output, 61)
        self.assertEqual(session.totals.reasoning, 25)

    def test_a_repeated_snapshot_names_the_same_total_rather_than_adding_to_it(self) -> None:
        """A re-read or replayed journal reports what the session cost, not a multiple of it."""
        snapshot = codex_token_count(input_tokens=450, output_tokens=61)

        once = codex_usage([snapshot])
        again = codex_usage([snapshot, snapshot, snapshot])

        self.assertEqual(once.totals, again.totals)
        self.assertEqual(again.records, 3, "the read is still described honestly")

    def test_both_cache_sides_are_read_from_the_snapshot_that_publishes_them(self) -> None:
        """A real rollout publishes `cache_write_input_tokens`, so it is a dimension, not a null."""
        session = codex_usage(
            [
                codex_token_count(
                    input_tokens=594168,
                    cached_input_tokens=546048,
                    cache_write_input_tokens=41772,
                    output_tokens=6524,
                    reasoning_output_tokens=2494,
                )
            ]
        )

        self.assertEqual(session.totals.cache_input, 41772)
        self.assertEqual(session.totals.cache_read_input, 546048)
        self.assertEqual(session.totals.input, 6348)
        self.assertEqual(session.totals.output, 6524)
        self.assertEqual(session.totals.reasoning, 2494)

    def test_a_cache_write_of_zero_is_the_provider_count_and_not_an_absence(self) -> None:
        session = codex_usage(
            [codex_token_count(input_tokens=900, cache_write_input_tokens=0, output_tokens=0)]
        )

        self.assertEqual(session.totals.cache_input, 0)

    def test_a_snapshot_missing_containment_fields_is_malformed(self) -> None:
        session = codex_usage(
            [
                codex_token_count(
                    input_tokens=10,
                    cached_input_tokens=None,
                    cache_write_input_tokens=None,
                    output_tokens=2,
                    reasoning_output_tokens=None,
                )
            ]
        )

        self.assertEqual(session.records, 0)
        self.assertEqual(session.invalid, 1)
        self.assertTrue(session.totals.empty)

    def test_reset_segments_sum_to_a_monotone_canonical_session_total(self) -> None:
        records = [
            json.loads(line)
            for line in (USAGE_FIXTURES / "codex-reset-session.jsonl").read_text().splitlines()
        ]

        session = codex_usage(records)

        self.assertEqual(session.records, 6)
        self.assertEqual(
            session.totals,
            TokenTotals(
                input=789388,
                cache_input=100000,
                cache_read_input=8900000,
                output=37873,
                reasoning=13000,
            ),
        )

    def test_a_record_that_declares_no_token_count_is_simply_not_a_usage_record(self) -> None:
        session = codex_usage([{"type": "event_msg", "payload": {"type": "task_started"}}, "not an object"])

        self.assertEqual(session.records, 0)
        self.assertEqual(session.invalid, 0)
        self.assertTrue(session.totals.empty)

    def test_a_declared_token_count_with_the_wrong_schema_is_malformed(self) -> None:
        """A provider schema change must not read as a session that spent nothing."""
        session = codex_usage(
            [
                {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": [1, 2]}},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {"tokens": "many"}}},
                },
            ]
        )

        self.assertEqual(session.records, 0)
        self.assertEqual(session.invalid, 3)
        self.assertTrue(session.totals.empty)


class ClaudeAggregationTests(unittest.TestCase):
    """Claude reports per message, so the session total is a sum over distinct messages."""

    def test_usage_objects_sum_across_the_messages_of_one_session(self) -> None:
        session = claude_usage(
            [
                claude_assistant(
                    "msg_1",
                    input_tokens=4,
                    cache_creation_input_tokens=1200,
                    cache_read_input_tokens=0,
                    output_tokens=90,
                    output_tokens_details={"thinking_tokens": 30},
                ),
                claude_assistant(
                    "msg_2",
                    input_tokens=6,
                    cache_creation_input_tokens=300,
                    cache_read_input_tokens=1200,
                    output_tokens=45,
                    output_tokens_details={"thinking_tokens": 0},
                ),
            ]
        )

        self.assertEqual(session.records, 2)
        self.assertEqual(session.totals.input, 10)
        self.assertEqual(session.totals.cache_input, 1500)
        self.assertEqual(session.totals.cache_read_input, 1200)
        self.assertEqual(session.totals.output, 135)
        self.assertEqual(session.totals.reasoning, 30)

    def test_realistic_fixture_keeps_input_exclusive_and_output_inclusive(self) -> None:
        records = [
            json.loads(line)
            for line in (USAGE_FIXTURES / "claude-cache-session.jsonl").read_text().splitlines()
        ]

        session = claude_usage(records[:3])

        self.assertEqual(session.totals.input, 10258)
        self.assertEqual(session.totals.cache_input, 12174)
        self.assertEqual(session.totals.cache_read_input, 44196)
        self.assertEqual(session.totals.output, 691)
        self.assertEqual(session.totals.reasoning, 9)

    def test_missing_thinking_detail_makes_reasoning_unavailable_without_losing_output(self) -> None:
        records = [
            json.loads(line)
            for line in (USAGE_FIXTURES / "claude-cache-session.jsonl").read_text().splitlines()
        ]

        session = claude_usage(records[:4])

        self.assertEqual(session.records, 3, "the duplicate message id contributes only its final record")
        self.assertEqual(session.totals.output, 901)
        self.assertIsNone(session.totals.reasoning)

    def test_thinking_greater_than_output_costs_only_the_reasoning_subset(self) -> None:
        """Optional reasoning metadata may not silently discard otherwise valid usage.

        This replaces the previous round's `test_thinking_greater_than_output_is_malformed`, which
        asserted that an out-of-range detail made the whole message unusable and left the session
        with no totals at all. `output_tokens_details` is metadata about a usage object that is
        otherwise complete, so a bad detail can only ever cost the reasoning subset; throwing the
        message away turned a Claude-side detail defect into an unaccounted phase.
        """
        records = [
            json.loads(line)
            for line in (USAGE_FIXTURES / "claude-cache-session.jsonl").read_text().splitlines()
        ]

        session = claude_usage(records[4:])

        self.assertEqual(session.records, 1)
        self.assertEqual(session.invalid, 1, "the bad detail is still reported as a skipped record")
        self.assertEqual(session.totals.input, 1)
        self.assertEqual(session.totals.output, 20)
        self.assertIsNone(session.totals.reasoning)

    def test_a_detail_object_of_the_wrong_shape_costs_only_the_reasoning_subset(self) -> None:
        session = claude_usage(
            [
                claude_assistant("msg_1", input_tokens=4, output_tokens=90, output_tokens_details=[]),
                claude_assistant(
                    "msg_2",
                    input_tokens=6,
                    output_tokens=45,
                    output_tokens_details={"thinking_tokens": -1},
                ),
            ]
        )

        self.assertEqual(session.records, 2)
        self.assertEqual(session.invalid, 2)
        self.assertEqual(session.totals.output, 135)
        self.assertIsNone(session.totals.reasoning)

    def test_malformed_core_usage_is_still_the_typed_malformed_outcome(self) -> None:
        """The distinction the change above has to preserve: a usage object that carries no usable
        count at all is a malformed record, not a message with unavailable reasoning."""
        session = claude_usage(
            [
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg_1",
                        "usage": {"input_tokens": "many", "output_tokens_details": {"thinking_tokens": 3}},
                    },
                }
            ]
        )

        self.assertEqual(session.records, 0)
        self.assertEqual(session.invalid, 1)
        self.assertTrue(session.totals.empty)

    def test_output_has_one_inclusive_meaning_across_adapters(self) -> None:
        codex = codex_usage(
            [codex_token_count(input_tokens=10, output_tokens=90, reasoning_output_tokens=30)]
        )
        claude = claude_usage(
            [
                claude_assistant(
                    "msg_1",
                    input_tokens=10,
                    output_tokens=90,
                    output_tokens_details={"thinking_tokens": 30},
                )
            ]
        )

        for session in (codex, claude):
            self.assertEqual(session.totals.output, 90)
            self.assertEqual(session.totals.reasoning, 30)
            additive_total = sum(
                value
                for name in ("input", "cache_input", "cache_read_input", "output")
                if (value := getattr(session.totals, name)) is not None
            )
            self.assertEqual(additive_total, 100, "reasoning is not added to the cost total")

    def test_one_message_counts_once_however_many_times_it_was_written(self) -> None:
        """A streamed message is written repeatedly and a resumed session repeats earlier ones."""
        partial = claude_assistant("msg_1", input_tokens=4, output_tokens=1)
        final = claude_assistant("msg_1", input_tokens=4, output_tokens=90)

        session = claude_usage([partial, final, final])

        self.assertEqual(session.records, 1)
        self.assertEqual(session.totals.output, 90, "the finished record of the message, not the partial")
        self.assertEqual(session.totals.input, 4)

    def test_a_zero_a_provider_reports_is_kept_and_a_field_it_omits_is_not(self) -> None:
        session = claude_usage(
            [
                claude_assistant("msg_1", input_tokens=0, output_tokens=90),
                claude_assistant("msg_2", input_tokens=5, output_tokens=10),
            ]
        )

        self.assertEqual(session.totals.input, 5)
        self.assertEqual(session.totals.output, 100)
        self.assertIsNone(session.totals.cache_input)
        self.assertIsNone(session.totals.cache_read_input)

    def test_records_that_declare_no_usage_are_not_counted_and_are_not_malformed(self) -> None:
        session = claude_usage(
            [
                {"type": "user", "message": {"role": "user", "content": "go"}},
                {"type": "assistant", "message": {"id": "msg_1"}},
                {"type": "summary"},
            ]
        )

        self.assertEqual(session.records, 0)
        self.assertEqual(session.invalid, 0)
        self.assertTrue(session.totals.empty)

    def test_a_declared_usage_object_with_the_wrong_schema_is_malformed(self) -> None:
        session = claude_usage(
            [
                {"type": "assistant", "message": {"id": "msg_1", "usage": ["input", 5]}},
                {"type": "assistant", "message": {"id": "msg_2", "usage": {"input_tokens": "many"}}},
            ]
        )

        self.assertEqual(session.records, 0)
        self.assertEqual(session.invalid, 2)
        self.assertTrue(session.totals.empty)


class PhaseIntervalTests(unittest.TestCase):
    """The one per-dimension attribution lattice, for every provider and every lifecycle path.

    Two axes, and they are different questions. Whether a predecessor occurrence exists at all is
    the first; whether that predecessor and this read each know a given dimension is the second. The
    old rule collapsed them by reading a missing predecessor dimension as zero, which handed a whole
    session's reasoning to whichever phase first managed to see it.
    """

    def test_a_session_nobody_has_accounted_for_yet_starts_at_zero(self) -> None:
        attributed = phase_interval(TokenTotals(input=450, output=61), None)

        self.assertEqual(attributed.tokens, TokenTotals(input=450, output=61))
        self.assertEqual(attributed.phase_baseline, TokenTotals(input=0, output=0))
        self.assertEqual(attributed.session_totals, TokenTotals(input=450, output=61))
        self.assertFalse(attributed.contradiction)

    def test_a_first_phase_dimension_the_provider_never_reported_stays_unknown(self) -> None:
        """No predecessor is not a licence to write zeros for what the provider did not say."""
        attributed = phase_interval(TokenTotals(input=450, output=61), None)

        self.assertIsNone(attributed.tokens.reasoning)
        self.assertIsNone(attributed.phase_baseline.reasoning)

    def test_a_second_phase_owns_only_what_the_session_spent_after_the_first(self) -> None:
        attributed = phase_interval(
            TokenTotals(input=1000, cache_input=40, output=200),
            TokenTotals(input=450, cache_input=10, output=61),
        )

        self.assertEqual(attributed.tokens, TokenTotals(input=550, cache_input=30, output=139))
        self.assertEqual(attributed.phase_baseline, TokenTotals(input=450, cache_input=10, output=61))

    def test_a_dimension_the_predecessor_never_recorded_is_owned_by_nobody(self) -> None:
        """The correction this card exists for.

        This replaces the previous round's `test_a_dimension_the_boundary_never_recorded_is_owned
        _from_zero`, which asserted that a predecessor's missing dimension read as a zero boundary
        and so gave this phase the entire session value. That is exactly the backcharge a real
        Claude journal produces: phase one ends between a detail-less tool-use record and the
        completed duplicate that carries `thinking_tokens`, and phase two would then own all of the
        session's reasoning. Absence of one predecessor dimension is not a boundary of zero.
        """
        attributed = phase_interval(TokenTotals(input=1000, reasoning=7), TokenTotals(input=450))

        self.assertIsNone(attributed.tokens.reasoning, "no phase owns what nobody bounded")
        self.assertIsNone(attributed.phase_baseline.reasoning)
        self.assertEqual(attributed.tokens.input, 550, "the dimensions that are known stay exact")

    def test_an_unattributed_dimension_still_re_establishes_its_session_boundary(self) -> None:
        """Null ownership may not poison the dimension for every later phase."""
        attributed = phase_interval(TokenTotals(input=1000, reasoning=7), TokenTotals(input=450))

        self.assertEqual(attributed.session_totals.reasoning, 7)

        later = phase_interval(TokenTotals(input=1500, reasoning=20), attributed.session_totals)

        self.assertEqual(later.tokens.reasoning, 13, "the next phase subtracts from that boundary")
        self.assertEqual(later.phase_baseline.reasoning, 7)

    def test_a_dimension_this_session_stopped_reporting_stays_unavailable(self) -> None:
        attributed = phase_interval(TokenTotals(input=1000), TokenTotals(input=450, output=61))

        self.assertIsNone(attributed.tokens.output)
        self.assertIsNone(attributed.phase_baseline.output)
        self.assertIsNone(attributed.session_totals.output)

    def test_a_session_total_below_its_own_durable_boundary_owns_nothing(self) -> None:
        """A replaced or rotated session file is not a phase that earned a negative account."""
        attributed = phase_interval(TokenTotals(input=10), TokenTotals(input=450))

        self.assertIn("fell below the authoritative boundary", attributed.contradiction)
        self.assertIsNone(attributed.tokens.input, "and not a fabricated zero either")

    def test_reasoning_that_escapes_its_own_output_is_a_contradiction_not_an_account(self) -> None:
        """Cross-account containment is a property of the attributed accounts, so it is checked
        where they are produced rather than only where they are written."""
        attributed = phase_interval(
            TokenTotals(output=100, reasoning=90), TokenTotals(output=95, reasoning=10)
        )

        self.assertIn("contained in its own output", attributed.contradiction)

    def test_containment_holds_for_every_account_it_can_be_read_in(self) -> None:
        consistent = phase_interval(
            TokenTotals(output=100, reasoning=40), TokenTotals(output=50, reasoning=20)
        )

        self.assertEqual(consistent.contradiction, "")
        for account in (consistent.tokens, consistent.phase_baseline, consistent.session_totals):
            self.assertLessEqual(account.reasoning, account.output)


class CausalBoundaryTests(unittest.TestCase):
    """Phase identity, not publication order, selects the predecessor boundary."""

    @staticmethod
    def occurrence(
        attempt: int,
        total: int | None,
        *,
        request_id: str = "request",
        pending: bool = False,
        session_id: str = "session-1",
        attempt_id: str = "attempt-1",
    ) -> AttemptUsageOccurrence:
        accounts = dict.fromkeys(TOKEN_DIMENSIONS, None)
        overrides = {
            "attempt": attempt,
            "report_generation": attempt,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "tokens": accounts | ({"input": total} if total is not None else {}),
            "session_totals": accounts | ({"input": total} if total is not None else {}),
            "phase_baseline": accounts | ({"input": 0} if total is not None else {}),
        }
        if total is None:
            overrides |= {"outcome": "source_unreadable", "detail": "unreadable"}
        event = usage_event(**overrides)
        event = Event(
            event_id=f"evt-{request_id}",
            kind=event.kind,
            entity_kind=event.entity_kind,
            ref=event.ref,
            actor=event.actor,
            reason=event.reason,
            occurred_at=event.occurred_at,
            data=event.data,
        )
        return AttemptUsageOccurrence(request_id, event, pending)

    def boundary(self, occurrences: list[AttemptUsageOccurrence]) -> TokenTotals | None:
        """Selection and then the boundary read, in the order the dispatcher seam runs them."""
        return predecessor_boundary(
            causal_predecessor(
                occurrences,
                adapter="codex",
                session_id="session-1",
                attempt=3,
                attempt_id="attempt-1",
                report_generation=3,
                phase="worker",
                role="worker",
            )
        )

    def test_the_latest_causal_phase_wins_when_publication_order_is_reversed(self) -> None:
        boundary = self.boundary(
            [self.occurrence(2, 450, request_id="second"), self.occurrence(1, 100, request_id="first")]
        )

        self.assertEqual(boundary, TokenTotals(input=450))

    def test_a_pending_occurrence_is_an_authoritative_boundary(self) -> None:
        self.assertEqual(
            self.boundary([self.occurrence(2, 450, request_id="second", pending=True)]),
            TokenTotals(input=450),
        )

    def test_another_session_leaves_no_boundary_here(self) -> None:
        self.assertIsNone(self.boundary([self.occurrence(2, 999, session_id="session-2")]))

    def test_a_degraded_causal_predecessor_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "no readable session-total boundary"):
            self.boundary([self.occurrence(2, None)])

    def test_a_phase_slot_owned_by_another_attempt_fails_closed(self) -> None:
        """Selecting the predecessor is causal-identity validation, and it reads no token count at
        all — which is why the seam can run it before the provider journal is opened."""
        with self.assertRaisesRegex(ValueError, "conflicting attempt id"):
            self.boundary([self.occurrence(3, 100, attempt_id="somebody-elses-attempt")])

    def test_a_run_with_no_session_identity_has_no_boundary_to_find(self) -> None:
        self.assertIsNone(
            causal_predecessor(
                [],
                adapter="codex",
                session_id="",
                attempt=1,
                attempt_id="attempt-1",
                report_generation=1,
                phase="worker",
                role="worker",
            )
        )


class SourceCollectionTests(unittest.TestCase):
    """Every way a read can fail has a name, and none of them is a zero-cost phase."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def test_an_adapter_with_no_structured_records_says_so(self) -> None:
        result = collect_usage(adapter="shell", source={})

        self.assertIs(result.outcome, AttemptUsageOutcome.ADAPTER_UNSUPPORTED)
        self.assertTrue(result.tokens.empty)

    def test_a_source_that_was_never_bound_to_this_run_is_named_unavailable(self) -> None:
        result = collect_usage(
            adapter="codex", source={"version": 1, "kind": CODEX_SOURCE_KIND, "state": "unbound"}
        )

        self.assertIs(result.outcome, AttemptUsageOutcome.SOURCE_UNAVAILABLE)
        self.assertIn("unbound", result.detail)

    def test_a_bound_source_without_a_session_id_is_a_missing_session_identity(self) -> None:
        source = bound_codex_source(self.root / "rollout.jsonl")
        source["session_id"] = ""

        result = collect_usage(adapter="codex", source=source)

        self.assertIs(result.outcome, AttemptUsageOutcome.SESSION_UNAVAILABLE)

    def test_a_journal_that_cannot_be_read_is_a_read_failure_and_not_an_empty_phase(self) -> None:
        result = collect_usage(adapter="claude", source=bound_claude_source(self.root / "missing.jsonl"))

        self.assertIs(result.outcome, AttemptUsageOutcome.SOURCE_UNREADABLE)
        self.assertTrue(result.tokens.empty)

    def test_a_journal_nothing_parses_out_of_is_malformed(self) -> None:
        path = self.root / "broken.jsonl"
        path.write_text("{not json\n{also not json\n", encoding="utf-8")

        result = collect_usage(adapter="claude", source=bound_claude_source(path))

        self.assertIs(result.outcome, AttemptUsageOutcome.RECORDS_MALFORMED)
        self.assertEqual(result.skipped_records, 2)

    def test_a_readable_journal_with_no_usage_record_says_the_usage_is_absent(self) -> None:
        for adapter, source, record in (
            ("claude", bound_claude_source, {"type": "user", "message": {}}),
            ("codex", bound_codex_source, {"type": "event_msg", "payload": {"type": "task_started"}}),
        ):
            with self.subTest(adapter=adapter):
                path = write_jsonl(self.root / f"quiet-{adapter}.jsonl", [record])

                result = collect_usage(adapter=adapter, source=source(path))

                self.assertIs(result.outcome, AttemptUsageOutcome.USAGE_ABSENT)
                self.assertEqual(result.skipped_records, 0)

    def test_the_journal_names_the_models_that_actually_ran_in_order_of_last_use(self) -> None:
        """`opus` is what the head asked for; the journal is the only record of what it resolved to."""
        claude = write_jsonl(
            self.root / "claude.jsonl",
            [
                claude_assistant("msg_1", input_tokens=1, output_tokens=1)
                | {
                    "effort": "medium",
                    "message": {
                        "id": "msg_1",
                        "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
                claude_assistant("msg_2", input_tokens=1, output_tokens=1)
                | {
                    "effort": "high",
                    "message": {
                        "id": "msg_2",
                        "model": "claude-fable-5-1",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
                claude_assistant("msg_3", input_tokens=1, output_tokens=1)
                | {
                    "effort": "high",
                    "message": {
                        "id": "msg_3",
                        "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
                {"type": "assistant", "message": {"id": "msg_4", "model": "<synthetic>"}},
            ],
        )
        codex = write_jsonl(
            self.root / "codex.jsonl",
            [
                {"type": "turn_context", "payload": {"model": "gpt-5.6-terra", "effort": "xhigh"}},
                codex_token_count(input_tokens=10, output_tokens=2),
            ],
        )

        read_claude = collect_usage(adapter="claude", source=bound_claude_source(claude))
        read_codex = collect_usage(adapter="codex", source=bound_codex_source(codex))

        self.assertEqual(read_claude.models.models, ("claude-fable-5-1", "claude-opus-5-5"))
        self.assertEqual(read_claude.models.model, "claude-opus-5-5")
        self.assertEqual(read_claude.models.effort, "high")
        self.assertEqual((read_codex.models.models, read_codex.models.effort), (("gpt-5.6-terra",), "xhigh"))

    def test_a_journal_with_no_usage_record_still_names_its_model(self) -> None:
        path = write_jsonl(
            self.root / "quiet.jsonl",
            [{"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "reasoning_effort": "low"}}],
        )

        result = collect_usage(adapter="codex", source=bound_codex_source(path))

        self.assertIs(result.outcome, AttemptUsageOutcome.USAGE_ABSENT)
        self.assertEqual((result.models.model, result.models.effort), ("gpt-5.6-sol", "low"))

    def test_a_declared_usage_record_with_the_wrong_schema_is_malformed_not_absent(self) -> None:
        """A provider schema change reads as a broken record, never as a phase that cost nothing."""
        for adapter, source, record in (
            (
                "codex",
                bound_codex_source,
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": []}}},
            ),
            (
                "claude",
                bound_claude_source,
                {"type": "assistant", "message": {"id": "msg_1", "usage": []}},
            ),
        ):
            with self.subTest(adapter=adapter):
                path = write_jsonl(self.root / f"broken-schema-{adapter}.jsonl", [record])

                result = collect_usage(adapter=adapter, source=source(path))

                self.assertIs(result.outcome, AttemptUsageOutcome.RECORDS_MALFORMED)
                self.assertEqual(result.skipped_records, 1)
                self.assertTrue(result.tokens.empty)

    def test_claude_thinking_outside_total_output_keeps_the_rest_of_the_usage(self) -> None:
        """This replaces the previous round's `test_claude_thinking_outside_total_output_is_a_typed
        _malformed_outcome`, which degraded a whole readable phase over its optional reasoning
        metadata. The valid input, cache and output evidence survives; only reasoning does not."""
        path = write_jsonl(
            self.root / "broken-thinking.jsonl",
            [
                claude_assistant(
                    "msg_1",
                    input_tokens=5,
                    output_tokens=20,
                    output_tokens_details={"thinking_tokens": 21},
                )
            ],
        )

        result = collect_usage(adapter="claude", source=bound_claude_source(path))

        self.assertIs(result.outcome, AttemptUsageOutcome.COLLECTED)
        self.assertEqual(result.skipped_records, 1)
        self.assertEqual(result.session_totals.input, 5)
        self.assertEqual(result.session_totals.output, 20)
        self.assertIsNone(result.session_totals.reasoning)

    def test_a_phase_after_a_durable_boundary_is_read_as_the_interval_since_it(self) -> None:
        path = write_jsonl(self.root / "grown.jsonl", [codex_token_count(input_tokens=450, output_tokens=61)])

        read = collect_usage(adapter="codex", source=bound_codex_source(path))
        self.assertEqual(
            read.tokens,
            TokenTotals(),
            "collection is the provider read alone; it attributes nothing",
        )
        # The boundary a Codex predecessor leaves is complete, because a usable Codex snapshot
        # reports all five dimensions. A partial boundary is the lattice's own case, above.
        result = apply_phase_boundary(
            read,
            TokenTotals(input=100, cache_input=0, cache_read_input=0, output=10, reasoning=0),
        )

        self.assertIs(result.outcome, AttemptUsageOutcome.COLLECTED)
        self.assertEqual(
            result.tokens,
            TokenTotals(input=350, cache_input=0, cache_read_input=0, output=51, reasoning=0),
        )
        self.assertEqual(
            result.session_totals,
            TokenTotals(input=450, cache_input=0, cache_read_input=0, output=61, reasoning=0),
        )
        self.assertEqual(
            result.baseline,
            TokenTotals(input=100, cache_input=0, cache_read_input=0, output=10, reasoning=0),
        )

    def test_a_session_total_below_the_boundary_is_a_typed_contradiction(self) -> None:
        path = write_jsonl(self.root / "reset.jsonl", [codex_token_count(input_tokens=5, output_tokens=0)])

        result = apply_phase_boundary(
            collect_usage(adapter="codex", source=bound_codex_source(path)),
            TokenTotals(input=450),
        )

        self.assertIs(result.outcome, AttemptUsageOutcome.ARITHMETIC_CONTRADICTION)
        self.assertTrue(result.tokens.empty)
        self.assertTrue(result.session_totals.empty)
        self.assertIn("fell below the authoritative boundary", result.detail)

    def test_a_truncated_tail_still_reports_the_complete_records_before_it(self) -> None:
        """The normal shape of a journal read while its writer is still around."""
        path = write_jsonl(
            self.root / "rollout.jsonl",
            [codex_token_count(input_tokens=120, output_tokens=8)],
            tail='{"type": "event_msg", "payl',
        )

        result = collect_usage(adapter="codex", source=bound_codex_source(path))

        self.assertIs(result.outcome, AttemptUsageOutcome.COLLECTED)
        self.assertEqual(result.session_totals.input, 120)
        self.assertEqual(result.skipped_records, 1)

    def test_a_bound_claude_journal_is_read_through_the_progress_source(self) -> None:
        path = write_jsonl(
            self.root / "session.jsonl",
            [claude_assistant("msg_1", input_tokens=3, output_tokens=77)],
        )
        run = {"fanout_policy": {"provider_progress_source": bound_claude_source(path)}}

        result = collect_usage(adapter="claude", source=provider_usage_source(run, adapter="claude"))

        self.assertIs(result.outcome, AttemptUsageOutcome.COLLECTED)
        self.assertEqual(result.session_totals.output, 77)
        self.assertEqual(result.records, 1)

    def test_a_run_that_holds_no_policy_at_all_yields_no_source(self) -> None:
        self.assertEqual(provider_usage_source(None, adapter="codex"), {})
        self.assertEqual(provider_usage_source({"fanout_policy": []}, adapter="codex"), {})


def usage_event(**overrides) -> Event:
    data = {
        "attempt": 2,
        "attempt_id": "attempt-1",
        "phase": "worker",
        "role": "worker",
        "report_generation": 2,
        "head": "codex",
        "adapter": "codex",
        "model": "gpt-5.6-terra",
        "model_source": "profile",
        "session_id": "codex-session-1",
        "session_id_reason": "",
        "launch_id": "run-1",
        "outcome": "collected",
        "detail": "",
        "source_kind": CODEX_SOURCE_KIND,
        "records": 3,
        "skipped_records": 0,
        "tokens": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 100, "output": 20},
        "session_totals": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 550, "output": 81},
        "phase_baseline": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 450, "output": 61},
    }
    data.update(overrides)
    return Event(
        event_id="evt_usage",
        kind=EventKind.ATTEMPT_USAGE,
        entity_kind=EntityKind.CARD,
        ref=CARD_REF,
        actor=Actor("dispatcher", "ummanu-dispatcher"),
        reason="attempt.usage: worker phase of attempt 2",
        occurred_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        data=data,
    )


class AttemptUsageEventTests(unittest.TestCase):
    """The typed boundary, because the whole point is a record nobody has to re-derive."""

    def test_a_complete_occurrence_round_trips_through_the_journal_record(self) -> None:
        event = usage_event()

        restored = Event.from_record(event.to_record("request-1"))

        self.assertEqual(restored, event)
        self.assertEqual(restored.kind, EventKind.ATTEMPT_USAGE)
        self.assertEqual(restored.data["tokens"]["input"], 100)

    def test_the_interval_its_boundary_and_its_baseline_all_round_trip(self) -> None:
        """The interval is checkable only if the two totals it was derived from are recorded too."""
        restored = Event.from_record(usage_event().to_record("request-boundary"))

        self.assertEqual(restored.data["session_totals"]["input"], 550)
        self.assertEqual(restored.data["phase_baseline"]["input"], 450)
        self.assertEqual(
            restored.data["tokens"]["input"],
            restored.data["session_totals"]["input"] - restored.data["phase_baseline"]["input"],
        )

    def test_a_collected_interval_must_match_its_total_and_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            usage_event(tokens=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 99, "output": 20})

    def test_a_collected_total_may_not_be_below_its_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "below its baseline"):
            usage_event(
                tokens=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 0},
                session_totals=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 10},
                phase_baseline=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 450},
            )

    def test_an_attributed_dimension_names_its_interval_baseline_and_total_together(self) -> None:
        """This replaces the previous round's `test_a_dimension_is_available_in_all_three_accounts
        _or_none`, which forbade the shape this card has to write: a dimension whose session total
        is known while its ownership is not. What stays forbidden is an *attributed* dimension
        missing the evidence it was computed from, which is what makes `tokens` checkable."""
        with self.assertRaisesRegex(ValueError, "interval and its baseline together"):
            usage_event(tokens=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 100})
        with self.assertRaisesRegex(ValueError, "interval and its baseline together"):
            usage_event(phase_baseline=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 450})
        with self.assertRaisesRegex(ValueError, "no session total"):
            usage_event(
                tokens=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 100, "output": 20},
                session_totals=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"output": 81},
                phase_baseline=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 450, "output": 61},
            )

    def test_a_known_session_boundary_survives_an_unattributed_dimension(self) -> None:
        """The lattice's third state, and the reason the accounts are independently nullable: the
        phase owns no reasoning because nobody bounded it, and the boundary it read is still on the
        record for the phase after it."""
        event = usage_event(
            tokens=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 100, "output": 20},
            session_totals=dict.fromkeys(TOKEN_DIMENSIONS, None)
            | {"input": 550, "output": 81, "reasoning": 30},
            phase_baseline=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 450, "output": 61},
        )

        restored = Event.from_record(event.to_record("request-unattributed"))

        self.assertEqual(restored.data["outcome"], "collected")
        self.assertIsNone(restored.data["tokens"]["reasoning"])
        self.assertIsNone(restored.data["phase_baseline"]["reasoning"])
        self.assertEqual(restored.data["session_totals"]["reasoning"], 30)

    def test_a_collected_occurrence_that_attributes_nothing_is_still_a_boundary(self) -> None:
        """Every dimension unattributed and the session total known: the phase read a real total and
        owns none of it, which a degraded outcome would not be able to say."""
        event = usage_event(
            tokens=dict.fromkeys(TOKEN_DIMENSIONS, None),
            phase_baseline=dict.fromkeys(TOKEN_DIMENSIONS, None),
            session_totals=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 550},
        )

        self.assertEqual(event.data["session_totals"]["input"], 550)

    def test_reasoning_may_not_escape_its_own_output_in_any_account(self) -> None:
        for account in ACCOUNTS:
            accounts = {name: dict.fromkeys(TOKEN_DIMENSIONS, None) for name in ACCOUNTS}
            accounts["session_totals"] = dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 550}
            accounts[account] = dict.fromkeys(TOKEN_DIMENSIONS, None) | {"output": 5, "reasoning": 6}
            with self.subTest(account=account), self.assertRaisesRegex(ValueError, "contained in its"):
                usage_event(**accounts)

    def test_a_historical_all_three_or_none_occurrence_still_reads_unchanged(self) -> None:
        """No migration and no backfill: the shape written before this card is still valid."""
        historical = usage_event(
            tokens=dict.fromkeys(TOKEN_DIMENSIONS, 0) | {"input": 100, "output": 20, "reasoning": 4},
            session_totals=dict.fromkeys(TOKEN_DIMENSIONS, 0) | {"input": 550, "output": 81, "reasoning": 9},
            phase_baseline=dict.fromkeys(TOKEN_DIMENSIONS, 0) | {"input": 450, "output": 61, "reasoning": 5},
        )

        restored = Event.from_record(historical.to_record("request-historical"))

        self.assertEqual(restored, historical)
        self.assertEqual(restored.data["tokens"]["cache_input"], 0, "a reported zero is still a count")

    def test_a_degraded_outcome_may_not_carry_token_totals(self) -> None:
        """Fabricating a zero for an unreadable phase is exactly the confusion this event exists
        to remove."""
        for account in ACCOUNTS:
            degraded = {name: dict.fromkeys(TOKEN_DIMENSIONS, None) for name in ACCOUNTS}
            degraded[account] = dict.fromkeys(TOKEN_DIMENSIONS, 0)
            with self.subTest(account=account), self.assertRaises(ValueError):
                usage_event(outcome="source_unreadable", **degraded)

    def test_a_collected_outcome_must_report_at_least_one_session_total(self) -> None:
        """A collected read that knows no dimension at all is not a read; the other two accounts may
        legitimately be empty when nothing about this phase could be attributed."""
        with self.assertRaisesRegex(ValueError, "at least one session total"):
            usage_event(
                tokens=dict.fromkeys(TOKEN_DIMENSIONS, None),
                phase_baseline=dict.fromkeys(TOKEN_DIMENSIONS, None),
                session_totals=dict.fromkeys(TOKEN_DIMENSIONS, None),
            )

    def test_the_declared_dimensions_are_the_whole_token_object(self) -> None:
        for account in ACCOUNTS:
            with self.subTest(account=account):
                with self.assertRaises(ValueError):
                    usage_event(**{account: {"input": 5}})
                with self.assertRaises(ValueError):
                    usage_event(**{account: dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 5, "extra": 1}})

    def test_a_count_is_a_non_negative_integer_or_nothing(self) -> None:
        for account in ACCOUNTS:
            for value in (-1, "5", True, 1.5):
                with self.subTest(account=account, value=value), self.assertRaises(ValueError):
                    usage_event(**{account: dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": value}})

    def test_an_absent_session_id_has_to_say_why_it_is_absent(self) -> None:
        with self.assertRaises(ValueError):
            usage_event(session_id=None, session_id_reason="")
        typed_absence = usage_event(session_id=None, session_id_reason="the pane wrote no session")
        self.assertIsNone(typed_absence.data["session_id"])

    def test_the_occurrence_names_its_round_role_and_head_configuration(self) -> None:
        for field, value in (
            ("attempt", 0),
            ("report_generation", 0),
            ("attempt_id", ""),
            ("adapter", ""),
            ("role", "observer"),
            ("phase", "verdict"),
            ("model_source", ""),
            ("outcome", "probably_fine"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                usage_event(**{field: value})

    def test_the_resolved_model_fields_are_optional_and_checked_when_present(self) -> None:
        """An occurrence written before them reads unchanged; a new one states all three coherently."""
        self.assertNotIn("resolved_model", usage_event().data)
        resolved = usage_event(
            resolved_model="gpt-5.6-terra",
            resolved_models=["gpt-5.6-sol", "gpt-5.6-terra"],
            resolved_effort="high",
        )
        self.assertEqual(Event.from_record(resolved.to_record("request-1")), resolved)
        for overrides in (
            {
                "resolved_model": "gpt-5.6-sol",
                "resolved_models": ["gpt-5.6-sol", "gpt-5.6-terra"],
                "resolved_effort": "",
            },
            {"resolved_model": "", "resolved_models": ["a", "a"], "resolved_effort": ""},
            {"resolved_model": "", "resolved_models": [""], "resolved_effort": ""},
            {"resolved_model": "a"},
            {"resolved_model": "", "resolved_models": [], "resolved_effort": None},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                usage_event(**overrides)

    def test_the_other_event_kinds_are_untouched_by_the_new_validation(self) -> None:
        """Historical records must keep reading exactly as they did."""
        moved = Event(
            event_id="evt_moved",
            kind=EventKind.CARD_MOVED,
            entity_kind=EntityKind.CARD,
            ref=CARD_REF,
            actor=Actor("dispatcher", "ummanu-dispatcher"),
            reason="worker report:done",
            occurred_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
            source_state="in_progress",
            target_state="validate",
        )

        self.assertEqual(Event.from_record(moved.to_record("request-2")), moved)


class AttemptUsageProjectionTests(unittest.TestCase):
    """The repository view joins export visibility without changing occurrence semantics.

    The canon is the card audit of a real store; a committed and a staged record under one request
    id, or one event id under two, are states its keys refuse, so they are not arranged here.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.audit = task_audit_for(card_store(self, dispatcher_seed(), instance_dir=self.root))
        self.canon = BoardEventCanon(self.audit)

    def test_committed_and_pending_occurrences_share_one_validated_view(self) -> None:
        committed = usage_event(attempt=1, report_generation=1)
        pending = usage_event(attempt=2, report_generation=2)
        pending = Event(
            event_id="evt_pending",
            kind=pending.kind,
            entity_kind=pending.entity_kind,
            ref=pending.ref,
            actor=pending.actor,
            reason=pending.reason,
            occurred_at=pending.occurred_at,
            data=pending.data,
        )
        self.canon.commit("request-committed", committed)
        self.canon.stage("request-pending", pending)

        projected = self.canon.attempt_usage_occurrences(ref=CARD_REF)

        self.assertEqual([item.request_id for item in projected], ["request-committed", "request-pending"])
        self.assertEqual([item.pending for item in projected], [False, True])
        self.assertEqual(projected[1].event.data, pending.data)

    def test_one_causal_phase_cannot_have_two_occurrence_owners(self) -> None:
        first = usage_event()
        second = Event(
            event_id="evt_second",
            kind=first.kind,
            entity_kind=first.entity_kind,
            ref=first.ref,
            actor=first.actor,
            reason=first.reason,
            occurred_at=first.occurred_at,
            data=first.data,
        )
        self.canon.commit("first-request", first)
        self.canon.stage("second-request", second)

        with self.assertRaisesRegex(ValueError, "conflicting occurrence owners"):
            self.canon.attempt_usage_occurrences(ref=CARD_REF)


class DispatcherAttemptUsageTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The seam: one occurrence per finished phase, bound to the run that earned it, deciding
    nothing about the card it is recorded against."""

    def usage_events(self) -> list[dict]:
        return [
            event["data"]
            for event in self.writer.audit.events(CARD_REF)
            if event.get("kind") == EventKind.ATTEMPT_USAGE.value
        ]

    def bind_source(self, role: str, journal: Path, *, adapter: str = "codex") -> str:
        """Bind a provider journal to the head that is serving this round, as a launch would.

        The session id comes from the run's own launch record rather than from the test, because a
        source that named some other session would not be the phase's source at all.
        """
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        run_key = "worker_head_run" if role == "worker" else "review_head_run"
        routing_key = "worker_run" if role == "worker" else "review_run"
        session_id = str(record[routing_key]["session_id"])
        source = (
            bound_codex_source(journal, session_id=session_id)
            if adapter == "codex"
            else bound_claude_source(journal, session_id=session_id)
        )
        run = dict(record[run_key])
        policy = dict(run.get("fanout_policy") or {})
        policy["provider_source" if adapter == "codex" else "provider_progress_source"] = source
        run["fanout_policy"] = policy
        record[run_key] = run
        self.runtime.production_state.save(payload)
        return session_id

    def codex_journal(self, name: str, **totals: int) -> Path:
        return write_jsonl(self.data_dir / "sessions" / name, [codex_token_count(**totals)])

    def claude_journal(self, name: str, message_id: str, **usage: int) -> Path:
        return write_jsonl(self.data_dir / "sessions" / name, [claude_assistant(message_id, **usage)])

    def claude_worker(self) -> None:
        """Put a Claude head on the worker role, so both adapters are exercised at this seam."""
        self.catalog.role_defaults["new_card"] = "claude-opus"

    def staged_usage(self) -> list[dict]:
        """Usage occurrences that are durable in the audit but not published yet."""
        return [
            record
            for record in self.writer.audit.pending_events()
            if record.get("kind") == EventKind.ATTEMPT_USAGE.value
        ]

    def refuse_usage_audit(self, step: str):
        """Break exactly one audit step, and only for usage records; hand back its repair.

        `claim` is the staging write and `append` is the publication, and the point of the two
        tests below is that the seam owes the card something different in each case.
        """
        audit = self.writer.audit
        original = getattr(audit, step)

        def refuse(request_id, event, **kwargs):
            if event.get("kind") == EventKind.ATTEMPT_USAGE.value:
                raise OSError("the audit journal is unwritable")
            return original(request_id, event, **kwargs)

        setattr(audit, step, refuse)
        return lambda: setattr(audit, step, original)

    def production_tick(self) -> dict:
        """One whole production tick, the way the running dispatcher takes one.

        `self.tick()` drives `_tick_task` for the card under test. A card that has reached Blocked
        or Done never gets another turn there, so a durability claim about "the next tick" has to be
        asserted against the tick itself and not against a per-card call the real loop would never
        make for that card again.
        """
        return self.runtime.production_tick()

    def usage_actions(self, tick: dict) -> list[dict]:
        """What one production tick reported about the usage obligations it found."""
        return [
            action
            for action in (tick.get("actions") or [])
            if isinstance(action, dict) and action.get("step") == "attempt-usage-recovery"
        ]

    def unobserved_card(self) -> None:
        """Take the observer away, so a green verdict retires the card in its own tick."""
        self.board.save_metadata(12, {"sprint_ref": ""})
        self.sprints.rows.clear()
        self.board.clear_sprints()

    def _report_blocked(self) -> None:
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            body="the card contradicts itself",
            classification="wrong_task_definition",
            request_id=self._worker_report_request_id("blocked", "wrong_task_definition"),
        )

    def _review_green(self, request_id: str = "review-green") -> None:
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="the change is right",
            request_id=request_id,
        )

    def test_an_accepted_done_report_accounts_the_worker_phase_it_closed(self) -> None:
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal(
            "worker-1.jsonl",
            input_tokens=8100,
            cached_input_tokens=7000,
            cache_write_input_tokens=1024,
            output_tokens=320,
        )
        session_id = self.bind_source("worker", journal)
        self._report_done()

        advanced = self.tick()

        self.assertEqual(advanced["to"], "ci")
        events = self.usage_events()
        self.assertEqual(len(events), 1)
        usage = events[0]
        self.assertEqual(usage["outcome"], AttemptUsageOutcome.COLLECTED.value)
        self.assertEqual(usage["role"], "worker")
        self.assertEqual(usage["phase"], "worker")
        self.assertEqual(usage["attempt"], 1)
        self.assertEqual(usage["report_generation"], 1)
        self.assertEqual(usage["adapter"], "codex")
        self.assertEqual(usage["model"], "gpt-5.6-terra")
        self.assertEqual(usage["model_source"], "profile")
        self.assertEqual(usage["session_id"], session_id)
        self.assertEqual(
            usage["tokens"],
            {
                "input": 76,
                "cache_input": 1024,
                "cache_read_input": 7000,
                "output": 320,
                "reasoning": 0,
            },
        )
        self.assertEqual(
            usage["phase_baseline"],
            dict.fromkeys(TOKEN_DIMENSIONS, None)
            | {
                "input": 0,
                "cache_input": 0,
                "cache_read_input": 0,
                "output": 0,
                "reasoning": 0,
            },
        )

    def test_a_claude_worker_phase_is_read_from_its_own_session_records(self) -> None:
        self.claude_worker()
        self.start_dispatcher()
        self.tick()
        journal = self.claude_journal(
            "worker-1.jsonl",
            "msg_1",
            input_tokens=9,
            cache_creation_input_tokens=1400,
            cache_read_input_tokens=22000,
            output_tokens=512,
            output_tokens_details={"thinking_tokens": 128},
        )
        session_id = self.bind_source("worker", journal, adapter="claude")
        self._report_done()

        self.assertEqual(self.tick()["to"], "ci")

        usage = self.usage_events()[0]
        self.assertEqual(usage["adapter"], "claude")
        self.assertEqual(usage["session_id"], session_id)
        self.assertEqual(usage["source_kind"], CLAUDE_SOURCE_KIND)
        self.assertEqual(
            usage["tokens"],
            {
                "input": 9,
                "cache_input": 1400,
                "cache_read_input": 22000,
                "output": 512,
                "reasoning": 128,
            },
        )

    def test_the_occurrence_carries_the_model_and_effort_the_cli_resolved(self) -> None:
        self.claude_worker()
        self.start_dispatcher()
        self.tick()
        journal = write_jsonl(
            self.data_dir / "sessions" / "worker-1.jsonl",
            [
                claude_assistant("msg_1", input_tokens=9, output_tokens=12)
                | {
                    "effort": "xhigh",
                    "message": {
                        "id": "msg_1",
                        "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 9, "output_tokens": 12},
                    },
                },
            ],
        )
        self.bind_source("worker", journal, adapter="claude")
        self._report_done()

        self.assertEqual(self.tick()["to"], "ci")

        usage = self.usage_events()[0]
        self.assertEqual(usage["resolved_model"], "claude-opus-5-5")
        self.assertEqual(usage["resolved_models"], ["claude-opus-5-5"])
        self.assertEqual(usage["resolved_effort"], "xhigh")

    def test_a_replayed_report_and_a_re_entered_tick_keep_one_occurrence(self) -> None:
        """Recovery re-reads a session that has moved on; the phase still has one account."""
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10)
        self.bind_source("worker", journal)
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        first = self.usage_events()

        write_jsonl(journal, [codex_token_count(input_tokens=999999, output_tokens=88888)])
        self.tick()
        self.tick()

        self.assertEqual(len(first), 1)
        self.assertEqual(self.usage_events(), first, "the occurrence is neither doubled nor rewritten")

    def test_an_accepted_blocked_report_accounts_the_worker_phase_too(self) -> None:
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=42, output_tokens=7))
        self._report_blocked()

        blocked = self.tick()

        self.assertEqual(blocked["to"], "blocked")
        events = self.usage_events()
        self.assertEqual([usage["phase"] for usage in events], ["worker"])
        self.assertEqual(events[0]["tokens"]["input"], 42)

    def test_a_green_verdict_accounts_the_review_phase_beside_the_worker_one(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        session_id = self.bind_source(
            "review", self.codex_journal("review-1.jsonl", input_tokens=5100, output_tokens=210)
        )
        self._review_green()

        self.tick()

        events = self.usage_events()
        self.assertEqual([usage["phase"] for usage in events], ["worker", "review"])
        review = events[1]
        self.assertEqual(review["role"], "reviewer")
        self.assertEqual(review["head"], "codex-reviewer")
        self.assertEqual(review["session_id"], session_id)
        self.assertEqual(review["tokens"]["output"], 210)

    def test_a_red_verdict_and_its_rework_round_are_separate_bound_occurrences(self) -> None:
        """Relaunch and rework are exactly where a phase account could drift onto another run."""
        self.claude_worker()
        self.start_dispatcher()
        self.tick()
        first_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-1.jsonl", "msg_1", input_tokens=100, output_tokens=10),
            adapter="claude",
        )
        self._report_done()
        self.tick()
        self.tick()
        reviewer = self.bind_source(
            "review", self.codex_journal("review-1.jsonl", input_tokens=200, output_tokens=20)
        )
        self._review_red()
        self._park_and_decide("rework")
        # The rework round writes code, so its done report is not the rejected checkout again.
        self.host.commit = "rework-round-two-c0ffee"
        second_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-2.jsonl", "msg_2", input_tokens=300, output_tokens=30),
            adapter="claude",
        )
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        events = self.usage_events()
        self.assertEqual(
            [(usage["role"], usage["report_generation"], usage["session_id"]) for usage in events],
            [
                ("worker", 1, first_worker),
                ("reviewer", 1, reviewer),
                ("worker", 2, second_worker),
            ],
        )
        self.assertNotEqual(first_worker, second_worker, "the rework round ran a second session")
        self.assertEqual([usage["tokens"]["input"] for usage in events], [100, 200, 300])

    def test_a_respawned_worker_is_accounted_for_the_run_that_actually_reported(self) -> None:
        """A round can bring the same role up twice; the phase belongs to the head that finished."""
        self.claude_worker()
        self.start_dispatcher()
        self.tick()
        first_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-1.jsonl", "msg_1", input_tokens=11, output_tokens=1),
            adapter="claude",
        )
        # The head's process died after the first tick, so the next one reclaims and respawns it.
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.host.head_pid = proc.pid
        record = self.runtime.production_state.records(self.runtime.production_state.load())[CARD_REF]
        self.host._write_head_pid(
            "worker", CARD_REF, head_run=record.worker_head_run, leaf=record.worker_leaf
        )
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        self.assertEqual(self.tick()["action"], "worker-respawned")
        self.host.worker_status_result = None
        second_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-2.jsonl", "msg_2", input_tokens=770, output_tokens=42),
            adapter="claude",
        )
        self._report_done()

        self.assertEqual(self.tick()["to"], "ci")

        usage = self.usage_events()
        self.assertEqual(len(usage), 1, "one round, one worker phase, however often it was launched")
        self.assertNotEqual(first_worker, second_worker)
        self.assertEqual(usage[0]["session_id"], second_worker)
        self.assertEqual(usage[0]["tokens"]["input"], 770)

    def test_the_occurrence_is_not_a_semantic_event_for_a_sprint_observer(self) -> None:
        """Routing telemetry never woke an observer, and neither does phase accounting."""
        self.start_dispatcher()
        self.tick()
        self._report_done()
        self.tick()

        usage = next(
            event
            for event in self.writer.audit.events(CARD_REF)
            if event.get("kind") == EventKind.ATTEMPT_USAGE.value
        )

        self.assertFalse(is_significant_card_event(usage, linked_refs={CARD_REF}))

    def test_a_session_that_cannot_be_read_degrades_and_moves_the_card_anyway(self) -> None:
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.data_dir / "sessions" / "gone.jsonl")
        self._report_done()

        advanced = self.tick()

        self.assertEqual(advanced["to"], "ci", "usage collection never decides the card")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "ci")
        usage = self.usage_events()[0]
        self.assertEqual(usage["outcome"], AttemptUsageOutcome.SOURCE_UNREADABLE.value)
        self.assertTrue(all(value is None for value in usage["tokens"].values()))

    def test_a_head_that_never_bound_a_session_still_leaves_a_typed_account(self) -> None:
        self.start_dispatcher()
        self.tick()
        self._report_done()

        self.assertEqual(self.tick()["to"], "ci")

        usage = self.usage_events()[0]
        self.assertEqual(usage["outcome"], AttemptUsageOutcome.SOURCE_UNAVAILABLE.value)
        self.assertTrue(usage["detail"])

    def test_causal_identity_is_validated_before_the_provider_is_read_at_all(self) -> None:
        """Ordering, not arithmetic: whether this phase slot already belongs to another attempt does
        not depend on what a provider journal says, so it is settled first and fails closed even
        when the read that follows would have been degraded and carried no counts to check."""
        self.start_dispatcher()
        self.tick()
        # No source is bound, so the provider read below can only degrade.
        foreign = usage_event(
            attempt=1,
            report_generation=1,
            attempt_id="somebody-elses-attempt",
            session_id="some-other-session",
        )
        self.writer.board_host.canon.commit("foreign-occurrence", foreign)
        self._report_done()

        with self.assertRaises(TaskError) as refused:
            self.tick()

        self.assertEqual(refused.exception.code, "audit_unavailable")
        self.assertIn("conflicting attempt id", refused.exception.message)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")

    def test_a_staged_occurrence_the_journal_cannot_publish_yet_still_advances_the_card(self) -> None:
        """A staged obligation is durable: the phase is accounted for, only its append is owed.

        This replaces the previous round's `test_a_refused_usage_write_leaves_the_report_and_the
        _transition_alone`, which asserted that a refused write left an accepted phase with no
        occurrence at all. That is the loss the reviewer found: once the card advanced, nothing ever
        revisited the phase. The contract here is the durability order instead — the card may pass a
        staged obligation, never an absent one — so the old assertion had to go.

        The recovery step drives a whole production tick rather than this card's turn in one: the
        published record is the same, and where it comes from is the point of this round's repair.
        """
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10))
        restore = self.refuse_usage_audit("append")
        self._report_done()

        advanced = self.tick()

        self.assertEqual(advanced["to"], "ci", "a staged obligation does not hold the card")
        self.assertEqual(self.usage_events(), [], "nothing is published yet")
        staged = self.staged_usage()
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["data"]["tokens"]["input"], 100)

        restore()
        self.production_tick()

        published = self.usage_events()
        self.assertEqual(len(published), 1, "recovery finishes the staged occurrence, once")
        self.assertEqual(published[0], staged[0]["data"], "and finishes exactly the staged one")
        self.assertEqual(self.staged_usage(), [])

    def test_recovery_publishes_the_staged_occurrence_and_not_a_re_read_of_the_session(self) -> None:
        """The session file has moved on by then; the phase's own interval is what is owed."""
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10)
        self.bind_source("worker", journal)
        restore = self.refuse_usage_audit("append")
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        write_jsonl(journal, [codex_token_count(input_tokens=999999, output_tokens=88888)])
        restore()
        self.production_tick()

        self.assertEqual(self.usage_events()[0]["tokens"]["input"], 100)

    def test_a_phase_never_advances_past_an_occurrence_that_could_not_be_staged(self) -> None:
        """An audit that cannot take the occurrence at all is an audit failure, not a token count."""
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10))
        restore = self.refuse_usage_audit("claim")
        self._report_done()

        with self.assertRaises(TaskError) as refused:
            self.tick()

        self.assertEqual(refused.exception.code, "audit_unavailable")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress", "the card did not advance")
        self.assertEqual(self.usage_events(), [])
        self.assertEqual(self.staged_usage(), [])

        restore()

        self.assertEqual(self.tick()["to"], "ci", "the retry accepts the same report")
        self.assertEqual(len(self.usage_events()), 1)

    def test_two_worker_phases_on_one_retained_codex_session_split_it_at_the_boundary(self) -> None:
        """The ordinary rework route keeps the conversation, so its journal keeps growing."""
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal("worker.jsonl", input_tokens=100, output_tokens=10)
        session_id = self.bind_source("worker", journal)
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        self.tick()
        self._review_red()

        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        # The retained head goes on writing into the session it already had.
        write_jsonl(
            journal,
            [
                codex_token_count(input_tokens=100, output_tokens=10),
                codex_token_count(input_tokens=450, output_tokens=61),
            ],
        )
        self.host.commit = "retained-rework-c0ffee"
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        phases = [usage for usage in self.usage_events() if usage["role"] == "worker"]
        self.assertEqual([usage["session_id"] for usage in phases], [session_id, session_id])
        self.assertEqual([usage["report_generation"] for usage in phases], [1, 2])
        self.assertEqual([usage["tokens"]["input"] for usage in phases], [100, 350])
        self.assertEqual([usage["tokens"]["output"] for usage in phases], [10, 51])
        self.assertEqual([usage["session_totals"]["input"] for usage in phases], [100, 450])
        self.assertEqual([usage["phase_baseline"]["input"] for usage in phases], [0, 100])

    def test_retained_codex_phases_straddling_resets_sum_to_the_derived_session_total(self) -> None:
        """New user turns reset raw counters without replacing the retained session or rollout."""
        records = [
            json.loads(line)
            for line in (USAGE_FIXTURES / "codex-reset-session.jsonl").read_text().splitlines()
        ]
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        journal = write_jsonl(self.data_dir / "sessions" / "reset-retained.jsonl", records[:2])
        session_id = self.bind_source("worker", journal)
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        self.tick()
        self._review_red()
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")

        write_jsonl(journal, records)
        self.host.commit = "retained-reset-rework-c0ffee"
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        phases = [
            usage
            for usage in self.usage_events()
            if usage["role"] == "worker" and usage["session_id"] == session_id
        ]
        self.assertEqual(len(phases), 2)
        expected = {
            "input": 789388,
            "cache_input": 100000,
            "cache_read_input": 8900000,
            "output": 37873,
            "reasoning": 13000,
        }
        for dimension, total in expected.items():
            self.assertEqual(sum(usage["tokens"][dimension] for usage in phases), total)
            self.assertEqual(phases[-1]["session_totals"][dimension], total)

    def test_a_contradictory_retained_total_is_degraded_without_holding_the_lifecycle(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal("replaced.jsonl", input_tokens=100, output_tokens=10)
        self.bind_source("worker", journal)
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        self.tick()
        self._review_red()
        self._park_and_decide("rework")

        write_jsonl(journal, [codex_token_count(input_tokens=5, output_tokens=1)])
        self.host.commit = "retained-replaced-c0ffee"
        self._report_done()

        self.assertEqual(self.tick()["to"], "ci")
        phases = [usage for usage in self.usage_events() if usage["role"] == "worker"]
        self.assertEqual(phases[-1]["outcome"], "arithmetic_contradiction")
        self.assertTrue(all(value is None for value in phases[-1]["tokens"].values()))

    def test_two_worker_phases_on_one_retained_claude_session_split_it_at_the_boundary(self) -> None:
        """Claude sums its messages, so a resumed session's earlier messages are the same risk."""
        self.claude_worker()
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        journal = self.claude_journal("worker.jsonl", "msg_1", input_tokens=8, output_tokens=90)
        session_id = self.bind_source("worker", journal, adapter="claude")
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        self.tick()
        self._review_red()

        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        write_jsonl(
            journal,
            [
                claude_assistant("msg_1", input_tokens=8, output_tokens=90),
                claude_assistant("msg_2", input_tokens=5, output_tokens=40),
            ],
        )
        self.host.commit = "retained-rework-c0ffee"
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        phases = [usage for usage in self.usage_events() if usage["role"] == "worker"]
        self.assertEqual([usage["session_id"] for usage in phases], [session_id, session_id])
        self.assertEqual([usage["tokens"]["input"] for usage in phases], [8, 5])
        self.assertEqual([usage["tokens"]["output"] for usage in phases], [90, 40])
        self.assertEqual([usage["session_totals"]["output"] for usage in phases], [90, 130])
        self.assertEqual([usage["phase_baseline"]["output"] for usage in phases], [0, 90])

    def test_a_retained_claude_session_attributes_reasoning_across_the_streaming_gap(self) -> None:
        """The real journal shape this card exists for, over three retained rounds.

        A Claude head writes a detail-less record of a tool-use message before the completed
        duplicate of the same message that carries `output_tokens_details.thinking_tokens`, and the
        terminal report tool runs inside that gap. So phase 1 ends having seen a real usage object
        with no reasoning in it at all, and phase 2 — resumed on the same session — sees the whole
        of it. Phase 2 must not be charged for phase 1's reasoning, must stay exact in every
        additive dimension, and must still leave the reasoning boundary it did read on the record,
        so phase 3 subtracts from that re-established boundary rather than inheriting a poisoned
        dimension forever.

        Publication is refused throughout, so all three phases are attributed from staged
        predecessors; the same-request replay and the reversed publication at the end are the two
        recovery routes over exactly this data.
        """
        records = [
            json.loads(line)
            for line in (USAGE_FIXTURES / "claude-thinking-gap-session.jsonl").read_text().splitlines()
        ]
        self.claude_worker()
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        # Phase 1 reports from inside the gap: the tool-use record is written, its completed
        # duplicate is not.
        journal = write_jsonl(self.data_dir / "sessions" / "thinking-gap.jsonl", records[:1])
        session_id = self.bind_source("worker", journal, adapter="claude")
        restore = self.refuse_usage_audit("append")
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        self.tick()
        self._review_red("review-red-round-one")
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        # The completed duplicate lands, and the retained head works the second round.
        write_jsonl(journal, records[:3])
        self.host.commit = "retained-gap-round-two-c0ffee"
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        self.tick()
        self._review_red("review-red-round-two")
        self.assertEqual(
            self._park_and_decide("rework", request_id="decision-rework-round-two")["action"],
            "review-red-reused-worker",
        )
        write_jsonl(journal, records)
        self.host.commit = "retained-gap-round-three-c0ffee"
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        staged = [
            item
            for item in self.writer.board_host.canon.attempt_usage_occurrences(ref=CARD_REF)
            if item.event.data["role"] == "worker" and item.event.data["session_id"] == session_id
        ]
        staged.sort(key=lambda item: item.event.data["report_generation"])
        self.assertEqual(len(staged), 3)
        self.assertTrue(all(item.pending for item in staged), "no append succeeded yet")
        phases = [item.event.data for item in staged]
        self.assertEqual([usage["outcome"] for usage in phases], ["collected"] * 3)

        # Phase 1 saw no reasoning at all, and that is not a zero.
        self.assertIsNone(phases[0]["tokens"]["reasoning"])
        self.assertIsNone(phases[0]["session_totals"]["reasoning"])
        self.assertEqual(phases[0]["tokens"]["output"], 118)

        # Phase 2 owns none of the session's reasoning, because nobody bounded it.
        self.assertIsNone(phases[1]["tokens"]["reasoning"], "phase 2 does not own phase 1's reasoning")
        self.assertIsNone(phases[1]["phase_baseline"]["reasoning"], "and no zero was invented for it")
        # Its additive dimensions are exact all the same.
        self.assertEqual(
            phases[1]["tokens"],
            {
                "input": 6,
                "cache_input": 120,
                "cache_read_input": 62181,
                "output": 240,
                "reasoning": None,
            },
        )
        # And the reasoning boundary it did read survives on the occurrence.
        self.assertEqual(phases[1]["session_totals"]["reasoning"], 170)

        # Phase 3 subtracts from that re-established boundary instead of staying poisoned.
        self.assertEqual(phases[2]["phase_baseline"]["reasoning"], 170)
        self.assertEqual(phases[2]["tokens"]["reasoning"], 150)
        self.assertEqual(phases[2]["session_totals"]["reasoning"], 320)
        for dimension, total in (
            ("input", 13),
            ("cache_input", 10031),
            ("cache_read_input", 176991),
            ("output", 658),
        ):
            self.assertEqual(
                sum(usage["tokens"][dimension] for usage in phases),
                total,
                f"{dimension} stays additive and exact across all three phases",
            )
            self.assertEqual(phases[-1]["session_totals"][dimension], total)

        # A replayed request returns the immutable staged occurrence, not a fresh read of a journal
        # that has grown since.
        with self.assertRaises(TaskError) as still_pending:
            self.writer.attempt_usage(
                role="dispatcher",
                actor="ummanu-dispatcher",
                reference=CARD_REF,
                data=staged[2].event.data,
                reason=staged[2].event.reason,
                request_id=staged[2].request_id,
            )
        self.assertEqual(still_pending.exception.code, "audit_pending")

        restore()
        canon = self.writer.board_host.canon
        original_projection = canon.attempt_usage_occurrences

        def reversed_projection(*, ref: str = ""):
            return tuple(reversed(original_projection(ref=ref)))

        with mock.patch.object(canon, "attempt_usage_occurrences", side_effect=reversed_projection):
            self.production_tick()

        exported = [
            usage
            for usage in self.usage_events()
            if usage["role"] == "worker" and usage["session_id"] == session_id
        ]
        exported.sort(key=lambda usage: usage["report_generation"])
        self.assertEqual(exported, phases, "reversed publication publishes exactly what was staged")
        self.assertEqual(self.staged_usage(), [])

    def _assert_two_pending_retained_worker_phases(self, adapter: str) -> None:
        """Keep publication down through both phases, then recover them in reverse order."""
        if adapter == "claude":
            self.claude_worker()
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        journal = (
            self.codex_journal("retained.jsonl", input_tokens=100, output_tokens=10)
            if adapter == "codex"
            else self.claude_journal("retained.jsonl", "msg_1", input_tokens=8, output_tokens=90)
        )
        session_id = self.bind_source("worker", journal, adapter=adapter)
        restore = self.refuse_usage_audit("append")
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        self.tick()
        self._review_red()
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        if adapter == "codex":
            write_jsonl(journal, [codex_token_count(input_tokens=450, output_tokens=61)])
            expected = {"input": [100, 350], "output": [10, 51], "final": {"input": 450, "output": 61}}
        else:
            write_jsonl(
                journal,
                [
                    claude_assistant("msg_1", input_tokens=8, output_tokens=90),
                    claude_assistant("msg_2", input_tokens=5, output_tokens=40),
                ],
            )
            expected = {"input": [8, 5], "output": [90, 40], "final": {"input": 13, "output": 130}}
        self.host.commit = "retained-rework-c0ffee"
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")

        staged = [
            item
            for item in self.writer.board_host.canon.attempt_usage_occurrences(ref=CARD_REF)
            if item.event.data["role"] == "worker" and item.event.data["session_id"] == session_id
        ]
        staged.sort(key=lambda item: item.event.data["report_generation"])
        self.assertEqual(len(staged), 2)
        self.assertTrue(all(item.pending for item in staged))
        self.assertEqual([item.event.data["tokens"]["input"] for item in staged], expected["input"])
        self.assertEqual([item.event.data["tokens"]["output"] for item in staged], expected["output"])

        # Same-request replay returns the immutable staged occurrence; the grown provider journal
        # cannot replace its already-accounted interval.
        replay = staged[1]
        with self.assertRaises(TaskError) as still_pending:
            self.writer.attempt_usage(
                role="dispatcher",
                actor="ummanu-dispatcher",
                reference=CARD_REF,
                data=replay.event.data,
                reason=replay.event.reason,
                request_id=replay.request_id,
            )
        self.assertEqual(still_pending.exception.code, "audit_pending")

        restore()
        canon = self.writer.board_host.canon
        original_projection = canon.attempt_usage_occurrences

        def reversed_projection(*, ref: str = ""):
            return tuple(reversed(original_projection(ref=ref)))

        with mock.patch.object(canon, "attempt_usage_occurrences", side_effect=reversed_projection):
            self.production_tick()

        exported = [
            usage
            for usage in self.usage_events()
            if usage["role"] == "worker" and usage["session_id"] == session_id
        ]
        self.assertEqual(len(exported), 2)
        exported.sort(key=lambda usage: usage["report_generation"])
        for dimension in ("input", "output"):
            self.assertEqual(
                sum(usage["tokens"][dimension] for usage in exported), expected["final"][dimension]
            )
        self.assertEqual(self.staged_usage(), [])

    def test_two_codex_phases_are_authoritative_while_both_appends_are_unavailable(self) -> None:
        self._assert_two_pending_retained_worker_phases("codex")

    def test_two_claude_phases_are_authoritative_while_both_appends_are_unavailable(self) -> None:
        self._assert_two_pending_retained_worker_phases("claude")

    def test_a_degraded_predecessor_holds_a_later_phase_on_the_same_session(self) -> None:
        """The provider failure is recorded and phase one advances; its absent boundary may not be
        guessed when the retained session later becomes readable."""
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        journal = self.data_dir / "sessions" / "appears-later.jsonl"
        self.bind_source("worker", journal)
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        first = [usage for usage in self.usage_events() if usage["role"] == "worker"]
        self.assertEqual(first[0]["outcome"], "source_unreadable")

        self.tick()
        self._review_red()
        self._park_and_decide("rework")
        write_jsonl(journal, [codex_token_count(input_tokens=450, output_tokens=61)])
        self.host.commit = "retained-rework-c0ffee"
        self._report_done()

        with self.assertRaises(TaskError) as refused:
            self.tick()

        self.assertEqual(refused.exception.code, "audit_unavailable")
        self.assertIn("predecessor", refused.exception.message)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(len([usage for usage in self.usage_events() if usage["role"] == "worker"]), 1)

    def test_a_replayed_second_phase_reproduces_its_interval_rather_than_the_session_total(
        self,
    ) -> None:
        """Recovery re-enters the acceptance after the retained session has grown again."""
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal("worker.jsonl", input_tokens=100, output_tokens=10)
        self.bind_source("worker", journal)
        self._report_done()
        self.tick()
        self.tick()
        self._review_red()
        self._park_and_decide("rework")
        write_jsonl(journal, [codex_token_count(input_tokens=450, output_tokens=61)])
        self.host.commit = "retained-rework-c0ffee"
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        accounted = self.usage_events()

        write_jsonl(journal, [codex_token_count(input_tokens=900000, output_tokens=70000)])
        self.tick()
        self.tick()

        self.assertEqual(self.usage_events(), accounted, "the interval is the one the phase earned")

    def test_a_blocked_card_is_still_owed_its_account_by_the_next_production_tick(self) -> None:
        """The reviewer's reproduction: a terminal card leaves nothing behind that a per-card pass
        could ever find again.

        Blocked is outside `ACTIVE_STATES` and the dispatcher drops its record on the way, so the
        tick that follows builds a cycle this card is not in. The obligation is published from the
        pending set instead, which knows nothing about cards.
        """
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=42, output_tokens=7))
        restore = self.refuse_usage_audit("append")
        self._report_blocked()

        self.assertEqual(self.tick()["to"], "blocked")

        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertNotIn(CARD_REF, self.runtime.production_state.load()["records"] or {})
        staged = self.staged_usage()
        self.assertEqual(len(staged), 1)
        self.assertEqual(self.usage_events(), [], "nothing is published while the append is refused")

        restore()
        tick = self.production_tick()

        self.assertEqual(
            tick["actions"][0]["step"],
            "attempt-usage-recovery",
            "the obligations are settled before the tick does anything else",
        )
        self.assertEqual(
            [action["action"] for action in self.usage_actions(tick)], ["attempt-usage-published"]
        )
        self.assertEqual(self.usage_actions(tick)[0]["published"], 1)
        published = self.usage_events()
        self.assertEqual(len(published), 1, "exactly one published event for the finished phase")
        self.assertEqual(published[0], staged[0]["data"], "and it is exactly the record that was staged")
        self.assertEqual(published[0]["tokens"]["input"], 42)
        self.assertEqual(self.staged_usage(), [], "zero obligations left owed")

    def test_a_done_card_is_still_owed_its_review_account_by_the_next_production_tick(self) -> None:
        """The other terminal route: a green verdict with no observer retires the card at once."""
        self.start_dispatcher()
        self.unobserved_card()
        self._run_worker_to_validate()
        self.tick()
        self.bind_source("review", self.codex_journal("review-1.jsonl", input_tokens=5100, output_tokens=210))
        restore = self.refuse_usage_audit("append")
        self._review_green()

        self.assertEqual(self.tick()["to"], "done")

        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        staged = self.staged_usage()
        self.assertEqual([record["data"]["phase"] for record in staged], ["review"])
        self.assertEqual([usage["phase"] for usage in self.usage_events()], ["worker"])

        restore()
        tick = self.production_tick()

        self.assertEqual(
            [action["action"] for action in self.usage_actions(tick)], ["attempt-usage-published"]
        )
        review = [usage for usage in self.usage_events() if usage["phase"] == "review"]
        self.assertEqual(len(review), 1, "exactly one published event for the finished review phase")
        self.assertEqual(review[0], staged[0]["data"])
        self.assertEqual(review[0]["tokens"]["output"], 210)
        self.assertEqual(self.staged_usage(), [], "zero obligations left owed")

    def test_a_red_verdict_parked_for_a_decision_is_owed_its_account_the_same_way(self) -> None:
        """Assessment is an active state, so this one could reach the card again — and still does
        not have to: the same global pass answers for it."""
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        self.bind_source("review", self.codex_journal("review-1.jsonl", input_tokens=310, output_tokens=17))
        restore = self.refuse_usage_audit("append")
        self._review_red()

        self.assertEqual(self.tick()["to"], "assessment")

        staged = self.staged_usage()
        self.assertEqual([record["data"]["phase"] for record in staged], ["review"])

        restore()
        self.production_tick()

        review = [usage for usage in self.usage_events() if usage["phase"] == "review"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0], staged[0]["data"])
        self.assertEqual(self.staged_usage(), [])

    def test_a_dispatcher_that_lost_its_record_still_owes_the_staged_account(self) -> None:
        """The pass takes no dispatcher record, no board lookup and no card state as input."""
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10))
        restore = self.refuse_usage_audit("append")
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        staged = self.staged_usage()
        self.assertEqual(len(staged), 1)
        # The dispatcher restarts without its records, the way recovery finds the board.
        payload = self.runtime.production_state.load()
        payload["records"] = {}
        self.runtime.production_state.save(payload)

        restore()
        self.production_tick()

        self.assertEqual(len(self.usage_events()), 1)
        self.assertEqual(self.usage_events()[0], staged[0]["data"])
        self.assertEqual(self.staged_usage(), [])

    def test_an_obligation_stays_pending_and_exact_until_a_tick_can_publish_it(self) -> None:
        """A publication failure owes the same record again, and fabricates nothing in its place."""
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10))
        restore = self.refuse_usage_audit("append")
        self._report_blocked()
        self.assertEqual(self.tick()["to"], "blocked")
        staged = self.staged_usage()

        refused = [self.production_tick(), self.production_tick()]

        for tick in refused:
            actions = self.usage_actions(tick)
            self.assertEqual([action["action"] for action in actions], ["attempt-usage-still-pending"])
            self.assertEqual(actions[0]["status"], "degraded")
            self.assertEqual(actions[0]["published"], 0)
            self.assertEqual(actions[0]["pending"], 1)
            self.assertEqual(actions[0]["pending_refs"], [CARD_REF])
        self.assertEqual(self.usage_events(), [], "a refused publication publishes nothing at all")
        self.assertEqual(self.staged_usage(), staged, "the same record, unchanged, still owed")

        restore()
        tick = self.production_tick()

        self.assertEqual(self.usage_actions(tick)[0]["published"], 1)
        self.assertEqual(len(self.usage_events()), 1)
        self.assertEqual(self.usage_events()[0], staged[0]["data"])
        self.assertEqual(self.staged_usage(), [])

    def test_a_pending_set_that_cannot_be_read_degrades_the_tick_rather_than_ending_it(self) -> None:
        """The pass runs before everything, so it may not be the thing that kills the tick."""
        self.start_dispatcher()
        self.tick()

        def refuse(*, ref: str = ""):
            raise OSError("the pending directory is unreadable")

        self.writer.board_host.canon.attempt_usage_occurrences = refuse

        tick = self.production_tick()

        actions = self.usage_actions(tick)
        self.assertEqual([action["action"] for action in actions], ["attempt-usage-pending-unreadable"])
        self.assertEqual(actions[0]["status"], "degraded")
        self.assertIn("still owed", actions[0]["reason"])

    def test_a_tick_that_owes_nothing_publishes_nothing_and_says_nothing(self) -> None:
        """Replay: the pass is idempotent, and a settled occurrence is not re-published."""
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10))
        self._report_done()
        self.assertEqual(self.tick()["to"], "ci")
        accounted = self.usage_events()
        self.assertEqual(len(accounted), 1)

        ticks = [self.production_tick(), self.production_tick()]

        self.assertEqual([self.usage_actions(tick) for tick in ticks], [[], []])
        self.assertEqual(self.usage_events(), accounted)
        self.assertEqual(self.staged_usage(), [])

    def test_every_published_account_comes_from_the_one_attribution_rule(self) -> None:
        """Codex and Claude, a first phase and a retained one, a live acceptance, a staged
        obligation and the recovery that publishes it: one attribution function answers for all of
        them, and no path computes an account of its own.

        The rule is instrumented rather than described, because "centralized" is only true if every
        published account can be traced back to a call of it.
        """
        produced: list[dict[str, dict[str, int | None]]] = []
        original_rule = attempt_usage_module.phase_interval

        def recording(session_totals, baseline):
            attributed = original_rule(session_totals, baseline)
            produced.append(
                {
                    "tokens": attributed.tokens.to_json(),
                    "session_totals": attributed.session_totals.to_json(),
                    "phase_baseline": attributed.phase_baseline.to_json(),
                }
            )
            return attributed

        self.claude_worker()
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        with mock.patch.object(attempt_usage_module, "phase_interval", recording):
            self.tick()
            journal = self.claude_journal("worker.jsonl", "msg_1", input_tokens=8, output_tokens=90)
            self.bind_source("worker", journal, adapter="claude")
            self._report_done()
            self.assertEqual(self.tick()["to"], "ci")
            self.tick()
            self.bind_source(
                "review", self.codex_journal("review-1.jsonl", input_tokens=200, output_tokens=20)
            )
            self._review_red()
            self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
            write_jsonl(
                journal,
                [
                    claude_assistant("msg_1", input_tokens=8, output_tokens=90),
                    claude_assistant("msg_2", input_tokens=5, output_tokens=40),
                ],
            )
            self.host.commit = "one-rule-rework-c0ffee"
            restore = self.refuse_usage_audit("append")
            self._report_done()
            self.assertEqual(self.tick()["to"], "ci")
            restore()
            self.production_tick()

        published = self.usage_events()
        collected = [usage for usage in published if usage["outcome"] == "collected"]
        self.assertEqual(
            {(usage["adapter"], usage["role"]) for usage in collected},
            {("claude", "worker"), ("codex", "reviewer")},
            "both adapters and both roles reached the seam",
        )
        self.assertEqual(
            [usage["phase_baseline"]["output"] for usage in collected if usage["role"] == "worker"],
            [0, 90],
            "a first phase and a retained phase, the second one recovered from a staged obligation",
        )
        self.assertEqual(
            len(produced), len(collected), "one attribution per collected phase, and no other arithmetic"
        )
        for usage in collected:
            with self.subTest(role=usage["role"], generation=usage["report_generation"]):
                self.assertIn(
                    {account: usage[account] for account in ACCOUNTS},
                    produced,
                    "the published accounts are the ones the rule produced",
                )

    def test_the_occurrence_is_readable_by_kind_without_parsing_any_prose(self) -> None:
        self.start_dispatcher()
        self.tick()
        self._report_done()
        self.tick()

        by_kind = self.writer.audit.events(CARD_REF, kind=EventKind.ATTEMPT_USAGE.value)
        typed = [
            event
            for event in self.writer.board_host.canon.events(ref=CARD_REF)
            if event.kind is EventKind.ATTEMPT_USAGE
        ]

        self.assertEqual(len(by_kind), 1)
        self.assertEqual(len(typed), 1)
        self.assertEqual(typed[0].data["phase"], "worker")


if __name__ == "__main__":
    unittest.main()
