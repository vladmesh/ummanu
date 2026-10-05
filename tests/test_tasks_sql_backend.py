"""`TaskReader` and `TaskWriter` on the PostgreSQL backend, against a real `postgres:16`.

This is the store-specific half of the card contract: the writer's mutations land —
request claim, card effect and event together — as one transaction (`docs/BOARD_STORE.md` §7.1),
and the reads that have no case in `tests/test_tasks.py`.  The reader's and writer's general cases
live there and run on this same store (`tests/sql_backend_fixtures.py` `card_store`).
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.fakes.tasks import open_sprint, reader_seed, writer_seed
from tests.sql_backend_fixtures import CardStoreCase
from ummanu.board import backend
from ummanu.board.sql_cards import _COLUMN_ID_BY_STATE
from ummanu.tasks import TaskError, TaskReader, TaskWriter


class SqlBoardCase(CardStoreCase):
    """One migrated database and one seeded board per test."""

    def client_for(self, seed) -> object:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        return self.card_store(seed, instance_dir=self.tmpdir.name)


class SqlTaskReaderTests(SqlBoardCase):
    """AC2: everything the reader returns today, returned from PostgreSQL in the same shape."""

    def setUp(self) -> None:
        self.client = self.client_for(reader_seed())
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]

    def test_export_carries_the_same_projection(self) -> None:
        rows = {row["reference"]: row for row in self.reader.export()}

        self.assertEqual(set(rows), {"ummanu-468", "old-1"})
        self.assertEqual(rows["ummanu-468"]["column"], "Ready")
        self.assertEqual(rows["ummanu-468"]["swimlane"], "Ummanu")
        self.assertEqual(rows["ummanu-468"]["project"], "ummanu")
        self.assertEqual(rows["ummanu-468"]["task_type"], "code")

    def test_a_tasks_row_states_task_whatever_its_bag_says(self) -> None:
        """secretary-1678: the table is the record type; a stale bag value does not rename the row."""
        key = self.client.key_of("ummanu-468")
        for stale in ("product", "issue", "epic"):
            with self.subTest(stale=stale), self.client.transaction():
                self.client._execute(
                    "UPDATE tasks SET extensions = jsonb_set(coalesce(extensions, '{}'::jsonb), "
                    "'{extra,record_type}', to_jsonb(%s::text), true) WHERE board_key = %s",
                    (stale, key),
                )
            self.assertEqual(self.client.metadata(key)["record_type"], "task")
            rows = {row["reference"]: row for row in self.reader.export()}
            self.assertEqual(rows["ummanu-468"]["metadata"]["record_type"], "task")

    def test_a_tasks_row_refuses_another_record_type_on_write(self) -> None:
        """secretary-1678: a write cannot store a Product's or Issue's kind in a `tasks` row's bag."""
        key = self.client.key_of("ummanu-468")
        for declared in ("product", "issue", "epic"):
            with self.subTest(declared=declared), self.assertRaisesRegex(TaskError, "is a task"):
                self.client.save_metadata(key, record_type=declared)
        self.client.save_metadata(key, record_type="task")
        stored = self.client._query("SELECT extensions FROM tasks WHERE board_key = %s", (key,))[0][0]
        bag = stored if isinstance(stored, dict) else json.loads(stored or "{}")
        self.assertIn((bag.get("extra") or {}).get("record_type"), {None, "task"})

    def test_restore_snapshot_returns_every_card_by_reference(self) -> None:
        snapshot = self.reader.restore_snapshot()

        self.assertEqual(set(snapshot), {"ummanu-468", "old-1"})
        self.assertIn("comments", snapshot["ummanu-468"])

    def test_steward_signal_cards_report_the_bounded_view(self) -> None:
        cards = self.reader.steward_signal_cards(project="ummanu")

        self.assertEqual(
            cards,
            [
                {
                    "reference": "ummanu-468",
                    "state": "ready",
                    "column": "Ready",
                    "project": "ummanu",
                    "date_moved": None,
                    "steward_report": "1",
                }
            ],
        )


class SqlTaskWriterTests(SqlBoardCase):
    """AC3: one transaction per protocol mutation, and the request-id refusal it must keep."""

    def setUp(self) -> None:
        self.client = self.client_for(writer_seed())
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)  # type: ignore[arg-type]
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]

    # --- the transition's transaction boundary ---------------------------------------

    def _place(self, reference: str, state: str) -> None:
        """Put a card in a column through the client's own protocol, not through its rows."""
        card = self.reader.show(reference)
        self.client.call(
            "moveTaskPosition",
            project_id=1,
            task_id=int(card["id"].rsplit("_", 1)[1]),
            column_id=_COLUMN_ID_BY_STATE[state],
            position=1,
            swimlane_id=0,
        )

    def _set_metadata(self, reference: str, **values: str) -> None:
        card = self.reader.show(reference)
        self.client.call(
            "saveTaskMetadata", task_id=int(card["id"].rsplit("_", 1)[1]), values=dict(values)
        )

    @contextlib.contextmanager
    def _drops_the_call_after(self, method: str):
        """The round trip after `method` is lost; `method` itself already landed."""
        served = self.client.call
        armed = False

        def call(name: str, /, **params):
            nonlocal armed
            if armed:
                armed = False
                raise TaskError("backend_unavailable", "the board is unavailable", 1)
            result = served(name, **params)
            if name == method:
                armed = True
            return result

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    @contextlib.contextmanager
    def _loses_the_reply_to(self, method: str):
        """`method` is applied and then its reply is lost, once."""
        served = self.client.call
        lost = False

        def call(name: str, /, **params):
            nonlocal lost
            result = served(name, **params)
            if name == method and not lost:
                lost = True
                raise TaskError("backend_unavailable", "the board is unavailable", 1)
            return result

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    def assertNoRepairIsOwed(self, error: TaskError) -> None:
        """The refusal says the mutation did not happen, and the audit agrees with it.

        The point of the check is the *absence* of a repair obligation. `audit_pending` promises
        the caller that a board write is committed and that `reconcile` owes it a repair; after a
        rollback both halves are false, and a caller that believed the sentence would wait for a
        repair `SqlTaskAudit.reconcile` can never perform.
        """
        self.assertNotEqual(error.code, "audit_pending")
        self.assertEqual(error.code, "backend_error")
        self.assertEqual(error.exit_code, 1)
        self.assertIn("no repair is owed", str(error))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(self.writer.reconcile(), (0, 0))

    def assertNothingSurvived(self, request_id: str) -> None:
        """Neither half of the mutation is there: no request row, and no published event."""
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id = %s", (request_id,)
            ),
            [(0,)],
        )
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM board_events WHERE request_id = %s", (request_id,)
            ),
            [(0,)],
        )

    def test_a_failure_after_the_column_move_leaves_neither_the_move_nor_a_staged_request(
        self,
    ) -> None:
        """§7.1 for the transition, at the first of its three post-effect points.

        `moveTaskPosition` returned and the round trip after it was lost: over a transport that
        leaves a card in Validate beside a staged request.  Here the move is a statement of the
        same transaction as the claim, so the rollback takes both and there is nothing to
        recover.
        """
        self._place("ummanu-468", "in_progress")

        with self._drops_the_call_after("moveTaskPosition"), self.assertRaises(TaskError) as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="ummanu-468",
                target="validate",
                reason="submit",
                request_id="rq-move-lost-read-back",
            )

        self.assertNoRepairIsOwed(raised.exception)
        self.assertEqual(self.reader.show("ummanu-468")["state"], "in_progress")
        self.assertNothingSurvived("rq-move-lost-read-back")

    def test_a_failure_after_the_claim_metadata_write_leaves_neither_the_claim_nor_a_staged_request(
        self,
    ) -> None:
        """The second point: the claim's own board work landed and then the reply was lost.

        Over a transport that is a card in In progress, its claim metadata half-written, and the
        event held open.  The metadata write here is issued inside the transition's transaction, so it rolls back with
        the column effect: the card is still Ready and still unclaimed.
        """
        self._set_metadata("ummanu-468", claim="")

        with (
            self._loses_the_reply_to("saveTaskMetadata"),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="ummanu-468",
                worker="codex-terra",
                request_id="rq-claim-lost-metadata-reply",
            )

        self.assertNoRepairIsOwed(raised.exception)
        card = self.reader.show("ummanu-468")
        self.assertEqual(card["state"], "ready")
        self.assertIsNone(card["claim"]["worker"])
        self.assertNothingSurvived("rq-claim-lost-metadata-reply")

    def test_a_failure_after_the_ready_cleanup_leaves_neither_the_reset_nor_a_staged_request(
        self,
    ) -> None:
        """The third point: the Ready reset landed and then the reply was lost.

        Over a transport that is a card in Ready whose reset is owed, held by a pending record.  Under one transaction the reset, the
        column effect and the claim are undone together, so the card keeps the routing the reset
        would have cleared.
        """
        self._place("ummanu-468", "in_progress")
        self._set_metadata(
            "ummanu-468", resolved_head="codex-terra", resolved_review_head="codex-reviewer"
        )

        with (
            self._loses_the_reply_to("saveTaskMetadata"),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="ummanu-468",
                target="ready",
                reason="",
                request_id="rq-ready-lost-reset-reply",
            )

        self.assertNoRepairIsOwed(raised.exception)
        card = self.reader.show("ummanu-468")
        self.assertEqual(card["state"], "in_progress")
        self.assertEqual(card["routing"]["resolved_worker_head"], "codex-terra")
        self.assertEqual(card["routing"]["resolved_review_head"], "codex-reviewer")
        self.assertEqual(card["claim"]["worker"], "codex-terra")
        self.assertNothingSurvived("rq-ready-lost-reset-reply")

    # --- Done retention, the fourth path with a board effect -------------------------

    def _move_time(self, reference: str) -> int:
        candidates = self.reader.done_retention_candidates()
        row = next(candidate for candidate in candidates if candidate["reference"] == reference)
        self.assertIsInstance(row["date_moved"], int)
        return int(row["date_moved"])

    def test_historical_done_row_without_observed_move_time_is_skipped(self) -> None:
        """Migration does not invent a timestamp for an episode the SQL store never observed."""
        self.client._execute(
            "UPDATE tasks SET state = 'done', date_moved = NULL WHERE task_ref = %s",
            ("ummanu-468",),
        )
        self.client._commit_unless_nested()
        self.assertEqual(
            self.reader.done_retention_candidates(),
            [{"reference": "ummanu-468", "date_moved": None}],
        )
        result = self.writer.retire_done(
            reference="ummanu-468", expected_date_moved=100, cutoff=101,
            retention_days=14, request_id="rq-retire-unknown",
        )
        self.assertTrue(result["skipped"])
        self.assertNothingSurvived("rq-retire-unknown")

    def test_a_lost_close_reply_in_done_retention_leaves_neither_the_close_nor_a_staged_request(
        self,
    ) -> None:
        """The counterpart `DoneRetentionTests.test_lost_close_reply_recovers_through_generic_reconcile` had none of.

        That case is retention's half of the §7.3 class: `closeTask` landed, its reply was lost,
        and an archived card survived beside a staged request for `reconcile` to settle.  Until
        secretary-1591 `retire_done` staged its request and issued the close outside
        `_mutation()`, so on this backend both committed at `_depth == 0` and the same
        half-applied state was reachable the moment the store could name a Done episode.  The
        whole of it — the freshness guard, the close, its proof and the record — is now one
        transaction, so the lost reply takes the close with it.
        """
        self._place("ummanu-468", "done")
        moved_at = self._move_time("ummanu-468")

        with (
            self._loses_the_reply_to("closeTask"),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.retire_done(
                reference="ummanu-468",
                expected_date_moved=moved_at,
                cutoff=moved_at + 1,
                retention_days=14,
                request_id="rq-retire-lost-close-reply",
            )

        self.assertNoRepairIsOwed(raised.exception)
        card = self.reader.show("ummanu-468")
        self.assertEqual(card["state"], "done")
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM tasks WHERE task_ref = %s AND archived", ("ummanu-468",)
            ),
            [(0,)],
        )
        self.assertNothingSurvived("rq-retire-lost-close-reply")

    def test_done_retention_that_completes_closes_the_card_and_commits_its_record(self) -> None:
        """The positive control for the case above: with no failure the same path retires.

        Without it the rollback proof would be satisfied by a fixture that never reached the
        close at all, which is exactly the vacuity the parked-case block is about.
        """
        self._place("ummanu-468", "done")
        moved_at = self._move_time("ummanu-468")

        result = self.writer.retire_done(
            reference="ummanu-468",
            expected_date_moved=moved_at,
            cutoff=moved_at + 1,
            retention_days=14,
            request_id="rq-retire-committed",
        )

        self.assertTrue(result["retired"])
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM tasks WHERE task_ref = %s AND archived", ("ummanu-468",)
            ),
            [(1,)],
        )
        self.assertEqual(
            self.client._query(
                "SELECT status FROM requests WHERE request_id = %s", ("rq-retire-committed",)
            ),
            [("committed",)],
        )
        replay = self.writer.retire_done(
            reference="ummanu-468",
            expected_date_moved=moved_at,
            cutoff=moved_at + 1,
            retention_days=14,
            request_id="rq-retire-committed",
        )
        self.assertTrue(replay["skipped"])
        self.assertFalse(replay["retired"])
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id = %s",
                ("rq-retire-committed",),
            ),
            [(1,)],
        )

    def test_done_retention_fresh_guard_uses_the_real_move_episode(self) -> None:
        self._place("ummanu-468", "done")
        moved_at = self._move_time("ummanu-468")

        result = self.writer.retire_done(
            reference="ummanu-468", expected_date_moved=moved_at, cutoff=moved_at,
            retention_days=14, request_id="rq-retire-fresh",
        )

        self.assertTrue(result["skipped"])
        self.assertFalse(result["retired"])
        self.assertEqual(
            self.client._query(
                "SELECT archived FROM tasks WHERE task_ref = %s", ("ummanu-468",)
            ),
            [(False,)],
        )
        self.assertNothingSurvived("rq-retire-fresh")

    def test_a_comment_lands_with_its_request_row_committed(self) -> None:
        result = self.writer.comment(
            role="po", actor="operator", reference="ummanu-468", body="hello", request_id="rq-1"
        )

        self.assertEqual(result["action"], "commented")
        self.assertFalse(result["replayed"])
        rows = self.client._query(
            "SELECT status, operation, ref FROM requests WHERE request_id = %s", ("rq-1",)
        )
        self.assertEqual(rows, [("committed", "commented", "ummanu-468")])
        task_id = self.client.call(
            "getTaskByReference", project_id=1, reference="ummanu-468"
        )["id"]
        bodies = [row["comment"] for row in self.client.call("getAllComments", task_id=task_id)]
        self.assertIn("hello", "\n".join(bodies))

    def test_the_same_request_id_replays_instead_of_writing_twice(self) -> None:
        self.writer.comment(
            role="po", actor="operator", reference="ummanu-468", body="once", request_id="rq-2"
        )
        again = self.writer.comment(
            role="po", actor="operator", reference="ummanu-468", body="once", request_id="rq-2"
        )

        self.assertTrue(again["replayed"])
        task_id = self.client.call(
            "getTaskByReference", project_id=1, reference="ummanu-468"
        )["id"]
        bodies = [row["comment"] for row in self.client.call("getAllComments", task_id=task_id)]
        self.assertEqual(sum("once" in body for body in bodies), 1)

    def test_cross_project_suffix_collision_is_isolated_end_to_end(self) -> None:
        """The two live refs share public number 1 but never share protocol identity or effects."""
        with self.client.transaction():
            self.client._execute(
                "INSERT INTO projects (project_id, enabled, registry_present) VALUES "
                "('butler', true, true), ('codegen-product-kit', true, true) "
                "ON CONFLICT (project_id) DO NOTHING"
            )
        keys = {}
        for ref, project in (
            ("butler-1", "butler"),
            ("codegen-product-kit-1", "codegen-product-kit"),
        ):
            keys[ref] = self.client.call(
                "createTask",
                project_id=1,
                title=ref,
                description=f"description for {ref}",
                column_id=_COLUMN_ID_BY_STATE["ready"],
                reference=ref,
            )
            self.client.call("saveTaskMetadata", task_id=keys[ref], values={"project": project})

        self.assertNotEqual(keys["butler-1"], keys["codegen-product-kit-1"])
        self.client.call(
            "updateTask",
            id=keys["codegen-product-kit-1"],
            reference="codegen-product-kit-1",
            title="kit collision updated",
        )
        self.assertEqual(
            self.client._query(
                "SELECT board_key FROM tasks WHERE task_ref = 'codegen-product-kit-1'"
            ),
            [(keys["codegen-product-kit-1"],)],
        )
        self.assertEqual(
            self.client._query(
                "SELECT task_ref, task_number FROM tasks WHERE task_ref IN (%s, %s) ORDER BY task_ref",
                ("butler-1", "codegen-product-kit-1"),
            ),
            [("butler-1", 1), ("codegen-product-kit-1", 1)],
        )
        self.writer.comment(
            role="po", actor="operator", reference="butler-1", body="butler only",
            request_id="collision-comment-butler",
        )
        replay = self.writer.comment(
            role="po", actor="operator", reference="butler-1", body="butler only",
            request_id="collision-comment-butler",
        )
        self.assertTrue(replay["replayed"])
        self.writer.comment(
            role="po", actor="operator", reference="codegen-product-kit-1", body="kit only",
            request_id="collision-comment-kit",
        )
        self.client.call(
            "moveTaskPosition", project_id=1, task_id=keys["codegen-product-kit-1"],
            column_id=_COLUMN_ID_BY_STATE["in_progress"], position=1,
        )
        self.writer.archive(
            role="po", actor="operator", reference="butler-1", reason="fixture archive",
            request_id="collision-archive-butler",
        )

        exported = {row["reference"]: row for row in self.reader.export()}
        self.assertEqual(
            sorted(ref for ref in exported if ref in keys),
            ["butler-1", "codegen-product-kit-1"],
        )
        self.assertTrue(exported["butler-1"]["closed"])
        self.assertFalse(exported["codegen-product-kit-1"]["closed"])
        self.assertEqual(exported["codegen-product-kit-1"]["column"], "In progress")
        self.assertIn("butler only", "\n".join(c["text"] for c in exported["butler-1"]["comments"]))
        self.assertNotIn("kit only", "\n".join(c["text"] for c in exported["butler-1"]["comments"]))
        self.assertIn(
            "kit only",
            "\n".join(c["text"] for c in exported["codegen-product-kit-1"]["comments"]),
        )
        audit_refs = self.client._query(
            "SELECT ref, count(*) FROM requests WHERE ref IN (%s, %s) GROUP BY ref ORDER BY ref",
            ("butler-1", "codegen-product-kit-1"),
        )
        self.assertEqual(audit_refs, [("butler-1", 2), ("codegen-product-kit-1", 1)])

        from ummanu.data import export_board

        artifact = export_board(
            Path(self.tmpdir.name) / "export-data",
            instance_dir=Path(self.tmpdir.name),
            reader=self.reader,
            sprint_client=self.client,
        )
        document = json.loads(artifact.path.read_text(encoding="utf-8"))
        exported_refs = [card["reference"] for card in document["cards"]]
        self.assertEqual(exported_refs.count("butler-1"), 1)
        self.assertEqual(exported_refs.count("codegen-product-kit-1"), 1)
        exported_cards = {card["reference"]: card for card in document["cards"]}
        self.assertIn(
            "butler only",
            "\n".join(c["text"] for c in exported_cards["butler-1"]["comments"]),
        )
        audit = json.loads((artifact.path.parent / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(event["ref"] for event in audit["events"] if event.get("ref") in keys),
            ["butler-1", "butler-1", "codegen-product-kit-1"],
        )

    def test_a_request_id_reused_for_another_operation_is_refused(self) -> None:
        from ummanu.tasks import TaskError

        self.writer.comment(
            role="po", actor="operator", reference="ummanu-468", body="first", request_id="rq-3"
        )
        with self.assertRaises(TaskError) as raised:
            self.writer.comment(
                role="po",
                actor="operator",
                reference="ummanu-468",
                body="second",
                request_id="rq-3",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertIn("another operation or payload", str(raised.exception))

    def _create(self, *, request_id: str, title: str = "A created card") -> dict:
        """A create against an open sprint the *store* holds, not only the sprint reader.

        `tasks.sprint_ref` is a foreign key here (§3.3), so the row a fake could invent by mocking
        `SprintReader.show` has to exist for the card to be storable at all.  Sprints on
        this backend are a later card; this is the one row that card's absence makes necessary.
        """
        now = datetime.now(UTC)
        with self.client.transaction():
            self.client._execute(
                "INSERT INTO sprints (ref, board_key, goal, definition_of_done, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, 'open', %s, %s) ON CONFLICT (ref) DO NOTHING",
                ("sprint:test", backend.record_key("sprint", "sprint:test"), "a goal", "a definition", now, now),
            )
        with (
            mock.patch("ummanu.sprints.sprint_guard_index_initialized", return_value=True),
            open_sprint() as sprint,
        ):
            return self.writer.create(
                role="observer",
                actor="observer",
                project="ummanu",
                task_type="code",
                title=title,
                request_id=request_id,
                sprint=sprint,
            )

    def test_a_created_card_names_its_reference_in_its_own_request_row(self) -> None:
        """§3.9's `requests.ref`, written by the transaction that chose the reference.

        A create claims its request id before the reference exists — the reference comes from the
        board's high-water mark inside the mutation — so the claim wrote `ref = NULL` and the
        statement that finally named it only replaced `intent`.  The column stayed NULL for the
        life of every created card, which made the record's own subject index answer nothing.
        """
        result = self._create(request_id="rq-create-1")

        reference = result["task"]["ref"]
        self.assertEqual(
            self.client._query(
                "SELECT status, operation, ref FROM requests WHERE request_id = %s", ("rq-create-1",)
            ),
            [("committed", "created", reference)],
        )

    def test_a_create_that_fails_after_its_claim_leaves_no_row_of_any_kind(self) -> None:
        """The boundary the same defect sat on: one transaction from the claim to the record.

        The claim used to commit on its own, before the card was written and long before the
        record was, so a failure in between left a staged `requests` row and, on the file journal
        of the time, a pending file to reconcile.  §7.3 says that class of half-applied write does not
        exist on this backend; it only actually did not once the whole create became one
        transaction.

        What it still asserts about the *refusal* is the create's own contract and not this
        card's: `audit_pending` here says "backend write committed; audit repair is required"
        beside two `count(*) = 0` checks that prove the opposite, and secretary-1591 repaired that
        sentence only for the two paths its observer decision named — the transition and Done
        retention (`TaskWriter._post_effect_refusal`).  The create, `_write_effect` and
        `_marker_write` still answer the old sentence, and the report of that round carries it as
        a finding rather than changing it here.
        """
        with (
            mock.patch.object(
                type(self.client),
                "_rpc_saveTaskMetadata",
                side_effect=TaskError("backend_error", "metadata refused", 1),
            ),
            self.assertRaises(TaskError) as raised,
        ):
            self._create(request_id="rq-create-2", title="A card that must not survive")

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id = %s", ("rq-create-2",)
            ),
            [(0,)],
        )
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM tasks WHERE title = %s", ("A card that must not survive",)
            ),
            [(0,)],
        )

    def test_a_failed_mutation_leaves_neither_effect_nor_claim(self) -> None:
        from ummanu.tasks import TaskError

        with self.assertRaises(TaskError):
            self.writer.comment(
                role="nobody", actor="x", reference="ummanu-468", body="no", request_id="rq-4"
            )
        self.assertEqual(
            self.client._query("SELECT count(*) FROM requests WHERE request_id = %s", ("rq-4",)),
            [(0,)],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
