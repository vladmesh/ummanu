"""The board import's write order against the schema's own foreign keys (`board.import_order`).

Four recovery drills of sprint 1476 each stopped on one more foreign key the import wrote out of
order -- an Issue before its Product (ummanu-27), then an issue comment before the `requests` row it
claims (ummanu-45, `issue_comment_claims_its_request`). These tests build the foreign-key graph from
`ummanu.board.schema`'s SQLAlchemy metadata, named and unnamed constraints alike, and hold the one
declared order to it. A `DEFERRABLE INITIALLY DEFERRED` key is checked at commit, so inside the
import's one transaction it holds whatever the order; those edges are left out on purpose.

A table or a foreign key added to the schema without a place in the order fails here, locally,
instead of in the next drill.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest import mock

import sqlalchemy as sa

from ummanu import restore, task_restore
from ummanu.board import schema
from ummanu.board.import_order import (
    IMPORT_PHASES,
    NOT_IMPORTED,
    foreign_key_edges,
    order_violations,
    phase_rank,
    record_rank,
)
from ummanu.board.sql_sprints import SqlSprintRecords
from ummanu.task_restore import RestoreCardObligation, _foreign_key_rank


def _copy(metadata: sa.MetaData) -> sa.MetaData:
    """An independent copy of the schema, to plant a constraint in without touching the real one."""
    copied = sa.MetaData()
    for table in metadata.sorted_tables:
        table.to_metadata(copied)
    return copied


class SchemaForeignKeyOrderTests(unittest.TestCase):
    def test_every_immediate_foreign_key_between_imported_tables_is_in_order(self) -> None:
        self.assertEqual(order_violations(schema.metadata), [])

    def test_the_graph_holds_the_named_claim_keys(self) -> None:
        edges = {edge.name: edge for edge in foreign_key_edges(schema.metadata)}
        claim = edges["issue_comment_claims_its_request"]
        self.assertEqual((claim.child, claim.parent, claim.deferred), ("issue_comments", "requests", False))
        # The one claim key PostgreSQL checks at commit, and so the one the order may ignore.
        self.assertTrue(edges["product_comment_claims_its_request"].deferred)
        # Unnamed keys are in the graph too, under a name built from their columns.
        self.assertIn("issues(product_id)", edges)
        self.assertEqual(edges["issues(product_id)"].parent, "products")

    def test_every_schema_table_is_placed_or_named_as_not_imported(self) -> None:
        placed = {table for phase in IMPORT_PHASES for table in phase.tables}
        self.assertEqual(placed | NOT_IMPORTED, set(schema.metadata.tables))
        self.assertEqual(placed & NOT_IMPORTED, set())

    def test_history_last_is_the_ummanu_45_defect(self) -> None:
        """The order before this card: the exported requests were restored after every comment."""
        history_last = (*IMPORT_PHASES[1:], IMPORT_PHASES[0])
        problems = order_violations(schema.metadata, history_last)
        self.assertTrue(
            any(problem.startswith("issue_comment_claims_its_request: issue_comments") for problem in problems),
            problems,
        )

    def test_issue_before_product_is_the_ummanu_27_defect(self) -> None:
        product, issue = phase_rank("product"), phase_rank("issue")
        swapped = list(IMPORT_PHASES)
        swapped[product], swapped[issue] = swapped[issue], swapped[product]
        self.assertIn(
            "issues(product_id): issues is written in phase 'issue' before its parent products is "
            "first written in phase 'product'",
            order_violations(schema.metadata, swapped),
        )

    def test_a_planted_foreign_key_the_order_violates_fails(self) -> None:
        planted = _copy(schema.metadata)
        planted.tables["requests"].append_constraint(
            sa.ForeignKeyConstraint(
                ["request_id"], ["issue_comments.request_id"], name="planted_request_names_a_comment"
            )
        )
        self.assertEqual(
            order_violations(planted),
            [
                (
                    "planted_request_names_a_comment: requests is written in phase 'history' before "
                    "its parent issue_comments is first written in phase 'card_comments'"
                )
            ],
        )

    def test_a_planted_deferred_foreign_key_is_satisfied_by_the_transaction(self) -> None:
        planted = _copy(schema.metadata)
        planted.tables["requests"].append_constraint(
            sa.ForeignKeyConstraint(
                ["request_id"],
                ["issue_comments.request_id"],
                name="planted_deferred",
                deferrable=True,
                initially="DEFERRED",
            )
        )
        self.assertEqual(order_violations(planted), [])

    def test_a_planted_table_without_a_place_fails(self) -> None:
        planted = _copy(schema.metadata)
        sa.Table(
            "issue_attachments",
            planted,
            sa.Column("attachment_id", sa.BigInteger, primary_key=True),
            sa.Column("issue_id", sa.Text, sa.ForeignKey("issues.issue_id")),
        )
        self.assertEqual(
            order_violations(planted),
            ["issue_attachments: not placed in the import order and not named as not imported"],
        )


def _obligation(record_type: str) -> RestoreCardObligation:
    metadata = {"record_type": record_type} if record_type else {}
    return RestoreCardObligation({"reference": record_type or "card"}, metadata, "", 1, 0, {})


class RecordRankTests(unittest.TestCase):
    def test_the_card_sweeps_rank_records_by_their_phase(self) -> None:
        ranks = [_foreign_key_rank(_obligation(kind)) for kind in ("product", "issue", "task", "")]
        self.assertEqual(
            ranks, [phase_rank("product"), phase_rank("issue"), phase_rank("card"), phase_rank("card")]
        )
        self.assertLess(ranks[0], ranks[1])
        self.assertLess(ranks[1], ranks[2])
        self.assertLess(phase_rank("history"), ranks[0])

    def test_an_unknown_kind_is_a_task_card(self) -> None:
        self.assertEqual(record_rank("something-new"), phase_rank("card"))

    def test_a_stable_sort_keeps_the_export_order_within_a_kind(self) -> None:
        items = [_obligation(kind) for kind in ("task", "issue", "product", "issue", "task")]
        for index, item in enumerate(items):
            item.card["reference"] = f"{item.metadata['record_type']}-{index}"
        ordered = [item.card["reference"] for item in sorted(items, key=_foreign_key_rank)]
        self.assertEqual(ordered, ["product-2", "issue-1", "issue-3", "task-0", "task-4"])


#: Each write step `_import_normalized_board` calls, where it lives, and the phase it writes. The
#: card restore runs the three record phases from `product` on (`RecordRankTests`).
_STEPS = (
    (restore, "_restore_board_history", "history"),
    (task_restore, "restore_cards_batched", "product"),
    (restore, "_restore_card_comments_batched", "card_comments"),
    (task_restore, "close_restored_cards_batched", "closure"),
    (restore, "_reconcile_restored_order", "order"),
    (restore, "_import_sprints", "sprints"),
    (SqlSprintRecords, "restore_close_facts", "sprint_close"),
    (restore.OriginOutboxStore, "restore", "origin_returns"),
    (restore.OwnerEventStore, "restore", "owner_events"),
)


class ImportFollowsTheOrderTests(unittest.TestCase):
    """`_import_normalized_board` runs its write steps in the declared phase order."""

    def test_write_steps_run_in_phase_order(self) -> None:
        called: list[str] = []

        def step(name: str, answer: object = None) -> mock.Mock:
            def record(*_args: object, **_kwargs: object) -> object:
                called.append(name)
                return answer

            return mock.Mock(side_effect=record)

        writer = mock.Mock()
        writer.reconcile.return_value = (0, [])
        writer.audit.pending_events.return_value = []
        writer.audit.events.return_value = []
        reader = mock.Mock()
        reader._board.return_value = (1, {}, {})
        reader.restore_snapshot.return_value = {}
        with ExitStack() as stack:
            for target, value in (
                ("ummanu.restore.file_lock", mock.Mock(return_value=nullcontext())),
                ("ummanu.sprints.sprint_admission_lock", mock.Mock(return_value=nullcontext())),
                ("ummanu.restore._normalized_cards", mock.Mock(return_value=[])),
                ("ummanu.restore._normalized_sprints", mock.Mock(return_value=[])),
                ("ummanu.restore.read_checkpoint_outbox", mock.Mock(return_value=[])),
                ("ummanu.restore.read_checkpoint_owner_events", mock.Mock(return_value=[])),
                ("ummanu.restore.read_checkpoint_close_facts", mock.Mock(return_value={})),
                ("ummanu.board.sql_sprints.SqlSprintRecords.export_close_facts", mock.Mock(return_value={})),
                ("ummanu.restore._checkpoint_po", mock.Mock(return_value=None)),
                ("ummanu.restore.OriginOutboxStore.export", mock.Mock(return_value=[])),
                ("ummanu.restore._check_sql_sprint_current_tasks", mock.Mock()),
                ("ummanu.restore._check_restored_observers", mock.Mock()),
                ("ummanu.restore._check_restored_executors", mock.Mock()),
                ("ummanu.restore._check_restored_admission", mock.Mock()),
                ("ummanu.restore.TaskReader", mock.Mock(return_value=reader)),
                ("ummanu.restore.TaskWriter", mock.Mock(return_value=writer)),
                ("ummanu.restore._existing_board_cards", mock.Mock(return_value={})),
                ("ummanu.restore._existing_sprints", mock.Mock(return_value={})),
                ("ummanu.restore._restore_request_prefix", mock.Mock(return_value="restore:t:")),
                ("ummanu.restore._validate_deferred_restore_comments", mock.Mock()),
                ("ummanu.restore._ensure_restore_swimlanes", mock.Mock(return_value=({}, {}))),
                ("ummanu.restore._require_card_snapshot", mock.Mock()),
                ("ummanu.restore._restored_order_mismatch", mock.Mock(return_value=False)),
                ("ummanu.restore._update_restore_state", mock.Mock()),
                ("ummanu.task_restore.commit_restored_cards", mock.Mock()),
            ):
                stack.enter_context(mock.patch(target, value))
            for module, name, phase in _STEPS:
                stack.enter_context(mock.patch.object(module, name, step(phase)))
            client = mock.Mock()
            client.sprints = SqlSprintRecords(client)
            restore._import_normalized_board(Path("/nonexistent-restore-data"), client=client)

        self.assertEqual(called, [phase for _module, _name, phase in _STEPS])
        ranks = [phase_rank(phase) for phase in called]
        self.assertEqual(ranks, sorted(ranks))


if __name__ == "__main__":
    unittest.main()
