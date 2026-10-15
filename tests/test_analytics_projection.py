"""Hermetic contracts for the sealed offline analytics projection."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from ummanu.board.analytics import AnalyticsProjectionError, project_analytics_checkpoint
from ummanu.board.models import TOKEN_DIMENSIONS, Actor, EntityKind, Event, EventKind
from ummanu.checkpoint import _write_analytics_manifest

CARD = "ummanu-1535"
SPRINT = "sprint:1419"
LEDGER_1418_ROUNDS = (
    ("ummanu-1418-01", "1418-round-01", 1, "rework"),
    ("ummanu-1418-02", "1418-round-02", 1, "rework"),
    ("ummanu-1418-03", "1418-round-03", 2, "rework"),
    ("ummanu-1418-04", "1418-round-04", 1, "rework"),
    ("ummanu-1418-05", "1418-round-05", 3, "rework"),
    ("ummanu-1418-06", "1418-round-06", 2, "rework"),
    ("ummanu-1418-07", "1418-round-07", 1, "reslice"),
    ("ummanu-1418-08", "1418-round-08", 2, "reslice"),
    ("ummanu-1418-09", "1418-round-09", 1, "reslice"),
    ("ummanu-1418-10", "1418-round-10", 4, "reslice"),
    ("ummanu-1418-11", "1418-round-11", 1, "reslice"),
)


def usage(*, event_id: str, attempt: int, attempt_id: str, role: str, card_ref: str = CARD) -> Event:
    phase = "worker" if role == "worker" else "review"
    return Event(
        event_id=event_id,
        kind=EventKind.ATTEMPT_USAGE,
        entity_kind=EntityKind.CARD,
        ref=card_ref,
        actor=Actor("dispatcher", "dispatcher"),
        reason="recorded provider phase usage",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        data={
            "attempt": attempt,
            "attempt_id": attempt_id,
            "phase": phase,
            "role": role,
            "report_generation": 1,
            "head": "head",
            "adapter": "codex",
            "model": "model",
            "model_source": "profile",
            "session_id": "session",
            "session_id_reason": "",
            "launch_id": "launch",
            "outcome": "collected",
            "detail": "",
            "source_kind": "test",
            "records": 1,
            "skipped_records": 0,
            "tokens": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 10, "output": 3},
            "session_totals": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 10, "output": 3},
            "phase_baseline": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 0, "output": 0},
        },
    )


def effect(*, event_id: str) -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.CARD_MOVED,
        entity_kind=EntityKind.CARD,
        ref=CARD,
        actor=Actor("dispatcher", "dispatcher"),
        reason="terminal lifecycle effect",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_state="validate",
        target_state="done",
    )


def report(*, event_id: str, specification_revision: str) -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.CARD_REPORTED,
        entity_kind=EntityKind.CARD,
        ref=CARD,
        actor=Actor("worker", "worker"),
        reason="done",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        data={
            "marker": "report:done",
            "status": "done",
            "body": "done",
            "body_sha256": "body",
            "description_sha256": "description",
            "specification_revision": specification_revision,
            "classification": None,
        },
    )


def outcome(
    *,
    event_id: str,
    attempt: int,
    attempt_id: str,
    worker_usage: str | None,
    review_usage: str | None,
    worker_completeness: str = "collected",
    review_completeness: str = "collected",
    sprint_ref: str | None = SPRINT,
    verdict: str = "green",
    card_ref: str = CARD,
    terminal_state: str = "done",
    disposition: str = "accept",
    effect_id: str | None = "effect",
) -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.ATTEMPT_OUTCOME,
        entity_kind=EntityKind.CARD,
        ref=card_ref,
        actor=Actor("dispatcher", "dispatcher"),
        reason="confirmed terminal lifecycle effect",
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        data={
            "version": 1,
            "attempt_id": attempt_id,
            "attempt": attempt,
            "report_generation": 1,
            "sprint_ref": sprint_ref,
            "specification_revision": None,
            "terminal_state": terminal_state,
            "verdict": verdict,
            "disposition": disposition,
            "blocked_reason": None,
            "source_event_ids": {
                "report": None,
                "verdict": None,
                "decision": None,
                "effect": effect_id,
                "worker_usage": worker_usage,
                "review_usage": review_usage,
            },
            "usage_completeness": {
                "worker": worker_completeness,
                "review": review_completeness,
            },
        },
    )


class AnalyticsProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.board = Path(self.tmpdir.name) / "copied-board"
        self.board.mkdir()

    def seal(
        self, events: list[dict], *, cards: list[dict] | None = None, sprints: list[dict] | None = None
    ) -> None:
        self.seal_at(self.board, events, cards=cards, sprints=sprints)

    @staticmethod
    def seal_at(
        board: Path, events: list[dict], *, cards: list[dict] | None = None, sprints: list[dict] | None = None
    ) -> None:
        cards = cards if cards is not None else [{"reference": CARD, "comments": ["ignored"]}]
        sprints = sprints if sprints is not None else [{"reference": SPRINT}]
        for name, values in (("cards.ndjson", cards), ("sprints.ndjson", sprints), ("events.ndjson", events)):
            (board / name).write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")
        (board / "audit.ndjson").write_text("", encoding="utf-8")
        (board / "export.json").write_text(
            json.dumps({"version": 1, "card_count": len(cards), "sprint_count": len(sprints)}) + "\n",
            encoding="utf-8",
        )
        _write_analytics_manifest(board)

    @staticmethod
    def record(event: Event, request: str) -> dict:
        return event.to_record(request)

    def valid_records(self) -> list[dict]:
        events: list[Event] = [effect(event_id="effect")]
        for attempt, attempt_id in ((1, "round-1"), (2, "round-2")):
            worker = usage(
                event_id=f"worker-{attempt}", attempt=attempt, attempt_id=attempt_id, role="worker"
            )
            reviewer = usage(
                event_id=f"review-{attempt}", attempt=attempt, attempt_id=attempt_id, role="reviewer"
            )
            events.extend(
                [
                    worker,
                    reviewer,
                    outcome(
                        event_id=f"outcome-{attempt}",
                        attempt=attempt,
                        attempt_id=attempt_id,
                        worker_usage=worker.event_id,
                        review_usage=reviewer.event_id,
                    ),
                ]
            )
        return [self.record(event, f"request-{number}") for number, event in enumerate(events, start=1)]

    def ledger_1418_records(self) -> tuple[list[dict], dict[tuple[str, str, int], dict[str, Event]]]:
        """Build the eleven-round red ledger only from typed event facts."""
        events: list[Event] = []
        expected: dict[tuple[str, str, int], dict[str, Event]] = {}
        for index, (card_ref, attempt_id, attempt, disposition) in enumerate(LEDGER_1418_ROUNDS, start=1):
            worker = usage(
                event_id=f"fixture-1418-worker-{index}",
                card_ref=card_ref,
                attempt=attempt,
                attempt_id=attempt_id,
                role="worker",
            )
            reviewer = usage(
                event_id=f"fixture-1418-reviewer-{index}",
                card_ref=card_ref,
                attempt=attempt,
                attempt_id=attempt_id,
                role="reviewer",
            )
            terminal_state = "in_progress" if disposition == "rework" else "blocked"
            result = outcome(
                event_id=f"fixture-1418-outcome-{index}",
                card_ref=card_ref,
                attempt=attempt,
                attempt_id=attempt_id,
                worker_usage=worker.event_id,
                review_usage=reviewer.event_id,
                verdict="red",
                terminal_state=terminal_state,
                disposition=disposition,
                effect_id=None,
            )
            events.extend((result, reviewer, worker))
            expected[(card_ref, attempt_id, 1)] = {"worker": worker, "reviewer": reviewer}
        return (
            [self.record(event, f"fixture-record-{number}") for number, event in enumerate(events, start=1)],
            expected,
        )

    def seal_1418_offline_copy(self, name: str, events: list[dict]) -> Path:
        source = Path(self.tmpdir.name) / f"{name}-source"
        offline_copy = Path(self.tmpdir.name) / f"{name}-offline-copy"
        source.mkdir()
        self.seal_at(
            source,
            events,
            cards=[{"reference": card_ref} for card_ref, *_ in LEDGER_1418_ROUNDS],
            sprints=[{"reference": SPRINT}],
        )
        shutil.copytree(source, offline_copy)
        return offline_copy

    def test_projects_a_typed_multi_round_cut_through_explicit_usage_ids(self) -> None:
        self.seal(self.valid_records())

        projection = project_analytics_checkpoint(self.board)

        self.assertFalse(projection.incomplete)
        self.assertEqual(len(projection.rows), 2)
        self.assertEqual(projection.rows[1]["attempt_id"], "round-2")
        self.assertEqual(projection.rows[1]["worker_usage"]["event_id"], "worker-2")
        self.assertEqual(projection.rows[1]["review_usage"]["phase"], "review")
        self.assertEqual(len(projection.ndjson().splitlines()), 2)

    def test_row_order_and_usage_joins_do_not_depend_on_append_order(self) -> None:
        records = self.valid_records()
        self.seal(list(reversed(records)))

        projection = project_analytics_checkpoint(self.board)

        self.assertEqual([row["attempt_id"] for row in projection.rows], ["round-1", "round-2"])
        self.assertEqual(projection.rows[0]["worker_usage"]["event_id"], "worker-1")

    def test_1418_shaped_red_ledger_is_complete_and_order_independent(self) -> None:
        """Eleven typed red rounds retain their explicit 22 phase facts offline."""
        records, expected = self.ledger_1418_records()
        event_rows = [Event.from_record(record) for record in records]
        self.assertEqual(sum(event.kind is EventKind.ATTEMPT_OUTCOME for event in event_rows), 11)
        self.assertEqual(sum(event.kind is EventKind.ATTEMPT_USAGE for event in event_rows), 22)

        projection = project_analytics_checkpoint(self.seal_1418_offline_copy("ordered", records))
        shuffled = project_analytics_checkpoint(
            self.seal_1418_offline_copy("shuffled", list(reversed(records)))
        )

        def semantic_rows(rows: tuple[dict, ...]) -> list[dict]:
            return [{name: value for name, value in row.items() if name != "checkpoint_id"} for row in rows]

        self.assertEqual(semantic_rows(projection.rows), semantic_rows(shuffled.rows))
        self.assertFalse(projection.incomplete)
        self.assertFalse(shuffled.incomplete)
        self.assertEqual(len(projection.rows), 11)
        self.assertEqual(len(projection.ndjson().splitlines()), 11)
        keys = {(row["card_ref"], row["attempt_id"], row["report_generation"]) for row in projection.rows}
        self.assertEqual(keys, set(expected))
        self.assertEqual(len(keys), 11)
        self.assertEqual({row["verdict"] for row in projection.rows}, {"red"})
        self.assertEqual(
            {
                disposition: sum(row["disposition"] == disposition for row in projection.rows)
                for disposition in ("rework", "reslice")
            },
            {"rework": 6, "reslice": 5},
        )

        usage_source_ids = [
            row["source_event_ids"][field]
            for row in projection.rows
            for field in ("worker_usage", "review_usage")
        ]
        self.assertEqual(len(usage_source_ids), 22)
        self.assertEqual(len(set(usage_source_ids)), 22)
        for row in projection.rows:
            key = (row["card_ref"], row["attempt_id"], row["report_generation"])
            self.assertEqual(row["usage_completeness"], {"worker": "collected", "review": "collected"})
            self.assertEqual(
                row["source_event_ids"],
                {
                    "report": None,
                    "verdict": None,
                    "decision": None,
                    "effect": None,
                    "worker_usage": expected[key]["worker"].event_id,
                    "review_usage": expected[key]["reviewer"].event_id,
                },
            )
            for role, field in (("worker", "worker_usage"), ("reviewer", "review_usage")):
                source = expected[key][role]
                joined = row[field]
                self.assertEqual(source.ref, row["card_ref"])
                self.assertEqual(source.data["attempt_id"], row["attempt_id"])
                self.assertEqual(source.data["attempt"], row["attempt"])
                self.assertEqual(source.data["report_generation"], row["report_generation"])
                self.assertEqual(joined, {"event_id": source.event_id, **source.data})

    def test_exact_duplicate_replay_is_one_row(self) -> None:
        records = self.valid_records()
        self.seal(records + [dict(records[-1])])

        projection = project_analytics_checkpoint(self.board)

        self.assertEqual(len(projection.rows), 2)

    def test_archived_and_live_card_rows_with_one_reference_are_membership(self) -> None:
        self.seal(
            self.valid_records(),
            cards=[
                {"reference": CARD, "archived": True},
                {"reference": CARD, "archived": False},
            ],
        )

        projection = project_analytics_checkpoint(self.board)

        self.assertEqual([row["attempt_id"] for row in projection.rows], ["round-1", "round-2"])

    def test_conflicting_natural_key_event_identity_and_request_owner_fail_closed(self) -> None:
        cases: dict[str, list[dict]] = {}
        natural = self.valid_records()
        conflict = outcome(
            event_id="outcome-conflict",
            attempt=1,
            attempt_id="round-1",
            worker_usage="worker-1",
            review_usage="review-1",
        )
        cases["analytics_conflicting_outcome_natural_key"] = natural + [
            self.record(conflict, "other-request")
        ]

        identity = self.valid_records()
        changed = dict(identity[0]) | {"request_id": "another-request"}
        cases["analytics_conflicting_event_identity"] = identity + [changed]

        request = self.valid_records()
        other = effect(event_id="another-effect")
        cases["analytics_conflicting_request_ownership"] = request + [self.record(other, "request-1")]

        for code, records in cases.items():
            with self.subTest(code=code):
                self.seal(records)
                with self.assertRaisesRegex(AnalyticsProjectionError, code):
                    project_analytics_checkpoint(self.board)

    def test_dangling_and_incompatible_refs_fail_closed(self) -> None:
        records = self.valid_records()
        records[-1] = dict(records[-1])
        records[-1]["data"] = dict(records[-1]["data"])
        records[-1]["data"]["source_event_ids"] = dict(records[-1]["data"]["source_event_ids"])
        records[-1]["data"]["source_event_ids"]["worker_usage"] = "missing-usage"
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_dangling_source_event_ref"):
            project_analytics_checkpoint(self.board)

        records = self.valid_records()
        records[-1] = dict(records[-1])
        records[-1]["data"] = dict(records[-1]["data"]) | {"sprint_ref": "sprint:missing"}
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_dangling_sprint_ref"):
            project_analytics_checkpoint(self.board)

        records = self.valid_records()
        records[-1] = dict(records[-1])
        records[-1]["data"] = dict(records[-1]["data"])
        records[-1]["data"]["source_event_ids"] = dict(records[-1]["data"]["source_event_ids"])
        records[-1]["data"]["source_event_ids"]["review_usage"] = "worker-2"
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_incompatible_usage_join"):
            project_analytics_checkpoint(self.board)

    def test_malformed_typed_schema_and_unknown_enum_fail_closed(self) -> None:
        records = self.valid_records()
        records[0] = dict(records[0]) | {"schema_version": 999}
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_invalid_typed_event"):
            project_analytics_checkpoint(self.board)

        self.seal(self.valid_records() + [["not an NDJSON object"]])
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_malformed_row"):
            project_analytics_checkpoint(self.board)

        records = self.valid_records()
        records[0] = dict(records[0]) | {"kind": "card.unknown"}
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_invalid_typed_event"):
            project_analytics_checkpoint(self.board)

    def test_legacy_and_no_outcome_evidence_remains_explicitly_incomplete(self) -> None:
        legacy = outcome(
            event_id="legacy-outcome",
            attempt=1,
            attempt_id="legacy-round",
            worker_usage=None,
            review_usage=None,
            worker_completeness="legacy",
            review_completeness="legacy",
            verdict="legacy",
        )
        self.seal(
            [self.record(effect(event_id="effect"), "effect-request"), self.record(legacy, "legacy-request")]
        )

        projection = project_analytics_checkpoint(self.board)

        self.assertTrue(projection.incomplete)
        self.assertIn("legacy_outcome", projection.incomplete_reasons)
        self.assertIsNone(projection.rows[0]["worker_usage"])
        self.assertEqual(projection.rows[0]["usage_completeness"]["worker"], "legacy")

        records = self.valid_records()
        records[1] = dict(records[1])
        records[1]["data"] = dict(records[1]["data"]) | {
            "outcome": "source_unreadable",
            "tokens": dict.fromkeys(TOKEN_DIMENSIONS),
            "session_totals": dict.fromkeys(TOKEN_DIMENSIONS),
            "phase_baseline": dict.fromkeys(TOKEN_DIMENSIONS),
        }
        records[3] = dict(records[3])
        records[3]["data"] = dict(records[3]["data"])
        records[3]["data"]["usage_completeness"] = {
            "worker": "degraded",
            "review": "collected",
        }
        self.seal(records)
        projection = project_analytics_checkpoint(self.board)
        self.assertEqual(projection.rows[0]["usage_completeness"]["worker"], "degraded")
        self.assertIsNone(projection.rows[0]["worker_usage"]["tokens"]["input"])

        self.seal([])
        projection = project_analytics_checkpoint(self.board)
        self.assertTrue(projection.incomplete)
        self.assertEqual(projection.incomplete_reasons, ("no_attempt_outcome_v1",))

    def test_v2_lineage_completeness_and_specification_binding_are_offline_verified(self) -> None:
        records = self.valid_records()
        source = report(event_id="report", specification_revision="spec-revision")
        records.append(self.record(source, "report-request"))
        first = dict(records[3])
        first["data"] = dict(first["data"])
        first["data"].update(
            {
                "version": 2,
                "specification_revision": "spec-revision",
                "source_event_ids": dict(first["data"]["source_event_ids"]) | {"report": "report"},
                "lineage_required": {
                    "specification_revision": True,
                    "report": True,
                    "verdict": False,
                    "decision": False,
                    "effect": True,
                    "worker_usage": True,
                    "review_usage": True,
                },
            }
        )
        records[3] = first
        records[0] = dict(records[0])
        records[0]["data"] = {
            "attempt_outcome_owed": {
                "attempt_id": "round-1",
                "attempt": 1,
                "report_generation": 1,
            }
        }
        self.seal(records)

        projection = project_analytics_checkpoint(self.board)

        self.assertEqual(projection.rows[0]["lineage_completeness"], {"complete": True, "missing": []})
        first["data"] = dict(first["data"]) | {
            "source_event_ids": dict(first["data"]["source_event_ids"]) | {"report": None}
        }
        records[3] = first
        self.seal(records)
        incomplete = project_analytics_checkpoint(self.board)
        self.assertEqual(
            incomplete.rows[0]["lineage_completeness"], {"complete": False, "missing": ["report"]}
        )
        self.assertIn("lineage_missing_report", incomplete.incomplete_reasons)

        first["data"] = dict(first["data"]) | {
            "source_event_ids": dict(first["data"]["source_event_ids"]) | {"report": "report"}
        }
        records[3] = first
        records[-1] = dict(records[-1])
        records[-1]["data"] = dict(records[-1]["data"]) | {"specification_revision": "other"}
        self.seal(records)
        with self.assertRaisesRegex(AnalyticsProjectionError, "analytics_incompatible_lineage_specification"):
            project_analytics_checkpoint(self.board)

    def test_projection_has_no_live_board_provider_or_comment_dependency(self) -> None:
        self.seal(self.valid_records())
        from ummanu import tasks
        from ummanu.board import analytics
        from ummanu.dispatch import attempt_usage

        verifier = analytics.verify_analytics_checkpoint
        with (
            mock.patch.object(
                tasks,
                "TaskReader",
                side_effect=AssertionError("projection must not read the live board"),
            ),
            mock.patch.object(
                attempt_usage,
                "collect_usage",
                side_effect=AssertionError("projection must not read provider usage"),
            ),
            mock.patch.object(analytics, "verify_analytics_checkpoint", wraps=verifier) as checked,
        ):
            projection = project_analytics_checkpoint(self.board)

        checked.assert_called_once_with(self.board)
        self.assertEqual(len(projection.rows), 2)
