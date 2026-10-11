"""Restore-only card batching over a real card store, and a production-shaped durability sample.

The restore writes through the card client the recovery binds, which is the PostgreSQL store, and
records its obligations in that store's audit (`SqlTaskAudit`). Each case gets a store of its own
(`tests/sql_backend_fixtures.py`).

The partial-RPC ambiguity cases the JSON-RPC board had -- a create or initialization batch whose
aggregate reply is lost after a prefix was applied, or answered malformed -- are not asked here:
`import_normalized_board` runs the whole restore in one enclosing transaction on the store, so a
failure leaves nothing applied to recover from. Nor is a per-request byte preflight: the store
posts no documents to bound.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.tasks import CardSeed
from tests.restore_fixtures import _restore_card
from tests.sql_backend_fixtures import card_store
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.task_restore import (
    commit_restored_cards,
    restore_cards_batched,
)
from ummanu.tasks import TaskError, TaskReader

#: The methods that change the board.
_WRITES = {"createTask", "saveTaskMetadata", "moveTaskPosition", "closeTask", "createComment"}


def _cards(count: int = 3) -> list[dict[str, object]]:
    return [
        _restore_card(
            task_id=index + 1,
            reference=f"ummanu-{index + 1}",
            title=f"Card {index + 1}",
            position=index * 3 + 1,
        )
        for index in range(count)
    ]


def _production_task_cards(limit: int) -> list[dict[str, object]]:
    """The first `limit` task rows of a 40-card sample from the sanitized recovery shape."""
    projection = json.loads(
        (Path(__file__).parent / "fixtures" / "recovery-card-shape-40.json").read_text(encoding="utf-8")
    )
    cards: list[dict[str, object]] = []
    for row in projection["rows"]:
        if str(row["record_type"]) != "task":
            continue
        index = len(cards) + 1
        card = _restore_card(
            task_id=int(row["ordinal"]),
            reference=f"ummanu-{index:04d}",
            title=f"Sanitized task {index}",
            column=str(row["column"]),
            swimlane=str(row["swimlane"]),
            position=int(row["actual_position"]),
        )
        card["closed"] = bool(row["closed"])
        cards.append(card)
        if len(cards) == limit:
            break
    return cards


class BulkCardRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        # No cards, and the one lane the fixture cards name.
        self.store = card_store(self, CardSeed(next_key=1))
        self.writer = SimpleNamespace(client=self.store, audit=SqlTaskAudit(self.store))

    def _batch(self, cards, *, existing=None) -> None:
        board_id, columns, swimlanes = TaskReader(self.store)._board()
        restore_cards_batched(
            self.writer,
            cards,
            board_id=board_id,
            columns=columns,
            swimlanes=swimlanes,
            existing=existing if existing is not None else {},
            request_prefix="restore:test:",
        )

    def _live(self) -> dict[str, dict[str, object]]:
        return {
            str(reference): {"id": card["id"]}
            for reference, card in TaskReader(self.store).restore_snapshot().items()
        }

    def _writes(self) -> list[str]:
        return [method for method, _params in self.store.calls if method in _WRITES]

    def test_a_restored_batch_commits_one_postgres_event_per_card(self) -> None:
        cards = _cards()
        self._batch(cards)
        commit_restored_cards(self.writer, cards, self._live(), request_prefix="restore:test:")

        events = self.writer.audit.events(kind="restored_bulk")
        self.assertEqual(sorted(event["ref"] for event in events), ["ummanu-1", "ummanu-2", "ummanu-3"])
        live = self._live()
        for event in events:
            with self.subTest(ref=event["ref"]):
                self.assertEqual(event["task_id"], live[event["ref"]]["id"])
                self.assertTrue(str(event["task_id"]).startswith("task_postgres_"))
                self.assertEqual(event["backend"]["kind"], "postgres")
        self.assertEqual(self.writer.audit.pending_events(), [])

    def test_every_obligation_is_staged_before_the_create_batch(self) -> None:
        cards = _cards()
        served = self.store.call_batch
        seen: list[int] = []

        def assert_staged(calls):
            batch = list(calls)
            if batch and batch[0][0] == "createTask":
                seen.append(len(self.writer.audit.pending_events()))
            return served(batch)

        with mock.patch.object(self.store, "call_batch", side_effect=assert_staged):
            self._batch(cards)
        self.assertEqual(seen, [len(cards)])

    def test_duplicate_or_wrong_existing_reference_fails_closed_without_mutation(self) -> None:
        cards = _cards(1)
        for defect in ("duplicate", "content"):
            with self.subTest(defect=defect):
                self.setUp()
                self.store.add_card(
                    1,
                    "ummanu-1",
                    title="wrong" if defect == "content" else "Card 1",
                    description="body",
                    lane="Ummanu",
                )
                row = self.store.row(1)
                existing = {"ummanu-1": row}
                if defect == "duplicate":
                    # Two rows of the enumeration naming one reference: the store's key refuses
                    # that state, so it is the enumeration the caller hands in that carries it.
                    existing["duplicate-slot"] = dict(row, id=99)
                self.store.calls.clear()
                with self.assertRaisesRegex(TaskError, "duplicate reference|different content"):
                    self._batch(cards, existing=existing)
                self.assertEqual(self._writes(), [])
                self.assertEqual(self.writer.audit.pending_events(), [])

    def test_definite_create_rejection_is_not_reported_as_uncertain(self) -> None:
        served = self.store.call

        def refuse_create(method: str, **params: object) -> object:
            if method == "createTask":
                return False
            return served(method, **params)

        with (
            mock.patch.object(self.store, "call", side_effect=refuse_create),
            self.assertRaises(TaskError) as raised,
        ):
            self._batch(_cards(1))
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertIn("create for ummanu-1", raised.exception.message)
        self.assertNotIn("uncertain", raised.exception.message)
        self.assertEqual(self.writer.audit.pending_events(), [])

    def test_audit_append_failure_replays_without_backend_mutation(self) -> None:
        cards = _cards()
        self._batch(cards)
        live = self._live()
        with (
            mock.patch.object(self.writer.audit, "append", side_effect=OSError("full")),
            self.assertRaisesRegex(TaskError, "audit repair"),
        ):
            commit_restored_cards(self.writer, cards, live, request_prefix="restore:test:")
        existing = {str(row["reference"]): row for row in self.store.restore_card_rows()}
        self.store.calls.clear()

        self._batch(cards, existing=existing)
        commit_restored_cards(self.writer, cards, self._live(), request_prefix="restore:test:")

        self.assertEqual(
            [method for method in self._writes() if method in {"createTask", "saveTaskMetadata", "moveTaskPosition"}],
            [],
        )
        self.assertEqual(len(self.writer.audit.events(kind="restored_bulk")), len(cards))
        self.assertEqual(self.writer.audit.pending_events(), [])


class ProductionShapeCardBenchmark(unittest.TestCase):
    def test_real_task_audit_durability_sample(self) -> None:
        cards = _production_task_cards(40)
        store = card_store(self, CardSeed(next_key=1))
        for lane in sorted({str(card["swimlane"]) for card in cards if card["swimlane"]}):
            store.call("addSwimlane", project_id=1, name=lane)
        writer = SimpleNamespace(client=store, audit=SqlTaskAudit(store))
        board_id, columns, swimlanes = TaskReader(store)._board()
        started = time.monotonic()
        restore_cards_batched(
            writer,
            cards,
            board_id=board_id,
            columns=columns,
            swimlanes=swimlanes,
            existing={},
            request_prefix="restore:durable:",
        )
        seconds = time.monotonic() - started
        self.assertEqual(len(writer.audit.pending_events()), 40)
        print(
            "BULK_CARD_RESTORE_DURABLE_SAMPLE durability=SqlTaskAudit cards=40 "
            f"seconds={seconds:.3f} per_card_ms={seconds / 40 * 1000:.3f}"
        )


if __name__ == "__main__":
    unittest.main()
