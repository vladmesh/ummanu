"""SQL atomicity probes for Sprint mutations and the Sprint transport namespace, on PostgreSQL 16.

The shared Sprint contract (tests/test_sprints.py and its siblings) runs on the store itself since
secretary-1670, so this module holds only the probes that name tables or inject a failure between
two statements of one transaction.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.fakes.sprints import SprintFixture
from tests.observer_identity import as_observer
from tests.sprint_close_fixtures import close_decisions
from tests.sql_backend_fixtures import PostgresBoard
from ummanu import sprints
from ummanu.board import sql_sprints
from ummanu.board.close_checkpoint import CloseCheckpointError, require_close_set_parity
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.sql_sprints import sprint_key
from ummanu.data import export_board, init_layout
from ummanu.product_issues import ProductIssueStore
from ummanu.restore import RestoreError, import_normalized_board
from ummanu.sprint_observer import head_choice, none_choice
from ummanu.sprints import SprintReader, SprintWriter
from ummanu.tasks import TaskError, TaskReader, TaskWriter


class SqlSprintAtomicityTests(SprintFixture):
    """Real-connection rollback probes for every multi-row Sprint mutation family."""

    def setUp(self) -> None:
        SprintFixture.setUp(self)
        self.client = self.make_ownership_client()
        self.writer = SprintWriter(self.client, data_dir=self.tmp.name, instance=self.instance)

    def _create(self, request_id: str = "atomic-create") -> dict:
        return self.writer.create(
            role="po",
            actor="operator",
            goal="atomic sprint",
            definition_of_done="every row commits together",
            repositories=[str(Path(self.tmp.name) / "repo")],
            product="ummanu",
            issues=["issue:open"],
            projects=["ummanu"],
            observer=head_choice("codex-observer"),
            reference="sprint:atomic",
            request_id=request_id,
        )

    def _assert_no_request(self, request_id: str) -> None:
        self.assertEqual(
            self.client._query("SELECT count(*) FROM requests WHERE request_id=%s", (request_id,)),
            [(0,)],
        )
        self.assertEqual(
            self.client._query("SELECT count(*) FROM board_events WHERE request_id=%s", (request_id,)),
            [(0,)],
        )

    def _export_mixed_close(self):
        products = ProductIssueStore(self.client, data_dir=self.tmp.name, instance=self.instance)
        issue = products.create_issue(
            product="ummanu", issue_kind="feature", priority="P1", title="Confirmed issue",
            description="Closed independently", actor="operator", request_id="create-confirmed-issue",
        )["ref"]
        reference = self.writer.create(
            role="po", actor="operator", goal="Preserve mixed close", reference="sprint:mixed",
            repositories=[str(Path(self.tmp.name) / "repo")], product="ummanu", issues=["issue:open", issue],
            projects=["ummanu"], observer=head_choice("codex-observer"), request_id="create-mixed-close",
        )["sprint"]["ref"]
        products.close_issue(reference=issue, reason="invalid", actor="operator", request_id="close-confirmed-issue")
        tasks = TaskWriter(self.client, data_dir=self.tmp.name)
        with as_observer(reference):
            cards = [tasks.create(
                role="observer", actor="observer", project="ummanu", task_type="code", title=name,
                target="ready", sprint=reference, request_id=f"create-mixed-{name}",
            )["task"]["ref"] for name in ("drop", "confirm")]
        with mock.patch.object(sql_sprints, "_now", return_value=datetime(2026, 9, 1, 0, 0, 0, 12, tzinfo=UTC)):
            self.writer.close(
                role="po", actor="operator", reference=reference, request_id="close-mixed",
                decisions={
                    "issues": [{"ref": "issue:open", "verdict": "open", "reason": "carry forward"},
                               {"ref": issue, "verdict": "already_closed", "actual": "invalid", "reason": "confirmed"}],
                    "cards": [{"ref": cards[0], "verdict": "drop", "reason": "deferred"},
                              {"ref": cards[1], "verdict": "already_moved", "actual": "ready", "reason": "confirmed"}],
                },
            )
        data = Path(self.tmp.name) / "capture"
        init_layout(data)
        export_board(data, instance_dir=self.instance, reader=TaskReader(self.client), sprint_client=self.client)
        facts = self.client.sprints.export_close_facts()
        rows = facts[reference]["decisions"]
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["request_id"] for row in rows}, {"close-mixed"})
        self.assertEqual({row["decided_at"] for row in rows}, {"2026-09-01T00:00:00.000012Z"})
        self.assertEqual({row["actual"] for row in rows}, {None, "invalid", "ready"})
        return data, facts

    def test_native_mixed_close_import_remaps_ids_and_caller_rollback_removes_all_facts(self):
        data, expected = self._export_mixed_close()
        for shape in ("fresh", "ownership"):
            with self.subTest(target=shape):
                if shape == "fresh":
                    config = PostgresBoard.shared().fresh_database()
                    self.addCleanup(PostgresBoard.shared().release_database, config.dbname)
                else:
                    seeded = self.make_empty_sprint_client()
                    config = PostgresBoard.shared().config(seeded.credentials.dbname)
                app = SqlCardClient(config.for_role("app"), self.instance)
                self.addCleanup(app.close)
                before = TaskReader(app).export()
                history = TaskWriter(app, data_dir=data).audit.events()
                if shape == "ownership":
                    with self.assertRaisesRegex(RuntimeError, "undo fixture"), app.transaction():
                        app._execute("UPDATE issues SET priority='P2' WHERE issue_id='open'")
                        conflicting = TaskReader(app).export()
                        with self.assertRaisesRegex(RestoreError, "different metadata"), app.transaction():
                            import_normalized_board(data, client=app, instance=self.instance)
                        self.assertEqual(TaskReader(app).export(), conflicting)
                        self.assertEqual(app.sprints.export_close_facts(), {})
                        self.assertEqual(TaskWriter(app, data_dir=data).audit.events(), history)
                        raise RuntimeError("undo fixture")
                with self.assertRaisesRegex(RuntimeError, "caller rollback"), app.transaction():
                    import_normalized_board(data, client=app, instance=self.instance)
                    require_close_set_parity(expected, app.sprints.export_close_facts())
                    raise RuntimeError("caller rollback")
                self.assertEqual(app.sprints.export_close_facts(), {})
                self.assertEqual(TaskReader(app).export(), before)
                self.assertEqual(TaskWriter(app, data_dir=data).audit.events(), history)
                import_normalized_board(data, client=app, instance=self.instance)
                actual = app.sprints.export_close_facts()
                require_close_set_parity(expected, actual)
                self.assertNotEqual(expected["sprint:mixed"]["decisions"][0]["decision_id"], actual["sprint:mixed"]["decisions"][0]["decision_id"])
                import_normalized_board(data, client=app, instance=self.instance)
                self.assertEqual(app.sprints.export_close_facts(), actual)

    def test_close_import_refuses_conflicting_extra_or_changed_columns_without_overwriting(self):
        data, expected = self._export_mixed_close()
        seeded = self.make_empty_sprint_client()
        config = PostgresBoard.shared().config(seeded.credentials.dbname)
        app = SqlCardClient(config.for_role("app"), self.instance)
        self.addCleanup(app.close)
        import_normalized_board(data, client=app, instance=self.instance)
        for damage in ("reason", "column", "extra", "ownership"):
            with self.subTest(damage=damage), self.assertRaisesRegex(RuntimeError, "undo fixture"), app.transaction():
                if damage == "reason":
                    app._execute("UPDATE sprint_decisions SET reason='conflicting stored reason' WHERE subject_kind='card'")
                elif damage == "column":
                    app._execute("UPDATE sprints SET closeout_document='retained-document'")
                elif damage == "ownership":
                    app._execute("UPDATE products SET title='Conflicting product' WHERE product_id='ummanu'")
                else:
                    app._execute("INSERT INTO sprint_issues(sprint_ref,issue_id,ordinal) VALUES ('sprint:mixed','done',99)")
                    app._execute(
                        "INSERT INTO sprint_decisions(sprint_ref,subject_kind,issue_id,verdict,actual,reason,request_id,decided_at) "
                        "VALUES ('sprint:mixed','issue','done','already_closed','resolved','retained extra','close-mixed',now())"
                    )
                before = app.sprints.export_close_facts()
                board_before = TaskReader(app).export()
                with self.assertRaises((RestoreError, CloseCheckpointError)), app.transaction():
                    import_normalized_board(data, client=app, instance=self.instance)
                self.assertEqual(app.sprints.export_close_facts(), before)
                self.assertEqual(TaskReader(app).export(), board_before)
                raise RuntimeError("undo fixture")
        require_close_set_parity(expected, app.sprints.export_close_facts())

    def test_reopened_sprint_retains_close_facts_through_normal_import(self):
        self._create()
        self.writer.close(
            role="po", actor="operator", reference="sprint:atomic", request_id="close-before-reopen",
            decisions={"issues": [{"ref": "issue:open", "verdict": "open", "reason": "retained"}], "cards": []},
            reason="retained close reason",
        )
        self.writer.reopen(
            role="po", actor="operator", reference="sprint:atomic", observer=head_choice("codex-observer"),
            request_id="reopen-with-facts",
        )
        data = Path(self.tmp.name) / "capture"
        init_layout(data)
        export_board(data, instance_dir=self.instance, reader=TaskReader(self.client), sprint_client=self.client)
        seeded = self.make_empty_sprint_client()
        config = PostgresBoard.shared().config(seeded.credentials.dbname)
        target = SqlCardClient(config.for_role("app"), self.instance)
        self.addCleanup(target.close)
        import_normalized_board(data, client=target, instance=self.instance)
        require_close_set_parity(self.client.sprints.export_close_facts(), target.sprints.export_close_facts())
        self.assertEqual(SprintReader(target).show("sprint:atomic")["status"], "open")

    def test_native_legacy_sprint_close_with_zero_decisions_has_explicit_coverage(self):
        self.writer.restore_create(
            reference="sprint:legacy", goal="Legacy sprint", observer=none_choice(),
            request_id="restore-legacy-sprint",
        )
        self.writer.close(role="po", actor="operator", reference="sprint:legacy", request_id="close-legacy-sprint")
        data = Path(self.tmp.name) / "capture"
        init_layout(data)
        export_board(data, instance_dir=self.instance, reader=TaskReader(self.client), sprint_client=self.client)
        seeded = self.make_empty_sprint_client()
        config = PostgresBoard.shared().config(seeded.credentials.dbname)
        target = SqlCardClient(config.for_role("app"), self.instance)
        self.addCleanup(target.close)
        import_normalized_board(data, client=target, instance=self.instance)
        facts = target.sprints.export_close_facts()["sprint:legacy"]
        self.assertEqual(facts, {"close_reason": None, "closeout_document": None, "decisions": []})

    def test_staged_persisted_projection_and_comment_marker_actor_stamp(self) -> None:
        reference = "sprint:1000"
        written = datetime(2026, 9, 1, 0, 0, 1, 900000, tzinfo=UTC)
        with self.client.transaction(), mock.patch.object(sql_sprints, "_now", return_value=written):
            key = self.client.sprints.create(title="projection", description="", reference=reference)
            staged = self.client.sprints.row_by_reference(reference)
            self.assertIn(staged, self.client.sprints.rows())
            self.assertEqual(self.client.sprints.comments_of([key]), {key: []})
            self.client.sprints.save_metadata(key, {
                "sprint_goal": "projection", "sprint_definition_of_done": "round trip", "sprint_status": "open",
            })
            persisted = self.client.sprints.row_by_reference(reference)
            self.assertEqual(persisted, staged)
            self.assertEqual(list(persisted), ["id", "reference", "title", "description", "column_id", "position",
                                               "swimlane_id", "date_creation", "date_modification", "is_active"])
            self.assertNotIn(key, self.client.sprints.staged)
            self.assertEqual(self.client.sprints.metadata_of([key])[key]["sprint_goal"], "projection")
            identifier = self.client.sprints.create_comment(key, "[]\r\nraw", created_at=written.isoformat())
        self.assertEqual(self.client._query(
            "SELECT marker, body, created_at FROM sprint_comments WHERE comment_id=%s", (identifier,)
        ), [("", "[]\r\nraw", written)])
        comment = self.client.sprints.comments_of([key])[key][0]
        self.assertEqual(comment, {"id": identifier, "date_creation": "1788220801", "comment": "[]\r\nraw"})
        self.writer.comment(role="po", actor="operator", reference=reference, body="stamped", request_id="projection-note")
        self.writer.comment(role="po", actor="operator", reference=reference, body="stamped", request_id="projection-note")
        self.assertEqual(self.client._query(
            "SELECT marker, body, actor_role, actor_id FROM sprint_comments WHERE request_id=%s", ("projection-note",)
        ), [("po", "[po]\nstamped", "po", "operator")])

    def _read_state(self):
        """Exact persisted rows and files, after fixture setup and outside the read trap."""
        tables = ("projects", "sprints", "sprint_repositories", "sprint_issues", "sprint_projects",
                  "sprint_comments", "sprint_resumes", "sprint_budget_events", "requests", "board_events")
        rows = {table: self.client._query(f"SELECT to_jsonb(row) FROM {table} AS row ORDER BY to_jsonb(row)::text")
                for table in tables}
        guard = Path(self.tmp.name) / "sprints"
        files = {str(p.relative_to(guard)): p.read_bytes() for p in guard.rglob("*") if p.is_file()}
        return rows, files

    @contextlib.contextmanager
    def _read_trap(self):
        """Instrument actual statements and reject all execute/audit/RPC mutations."""
        queries, writes = [], []
        query = self.client._query
        call = self.client.call

        def read(statement, *args, **kwargs):
            text = str(statement)
            queries.append(text)
            self.assertRegex(text.lstrip().upper(), r"^(SELECT|WITH)\b")
            self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE|FOR\s+UPDATE)\b", text, re.IGNORECASE))
            return query(statement, *args, **kwargs)

        def rpc(method, **params):
            self.assertTrue(method.startswith("get"), f"mutating read RPC: {method}")
            return call(method, **params)

        def forbid(*args, **kwargs):
            writes.append((args, kwargs))
            raise AssertionError("Sprint read attempted a SQL or audit mutation")

        with (mock.patch.object(self.client, "_query", side_effect=read),
              mock.patch.object(self.client, "_execute", side_effect=forbid),
              mock.patch.object(self.client, "call", side_effect=rpc),
              mock.patch.object(self.writer.audit, "stage", side_effect=forbid),
              mock.patch.object(self.writer.audit, "append", side_effect=forbid)):
            yield queries, writes
        self.assertEqual(writes, [])

    def test_fresh_provisioned_reader_construction_list_and_show_write_nothing(self) -> None:
        # PostgreSQL exposes a virtual board sentinel even with no Sprint rows. This test does
        # not pretend that its project lookup is the missing-board protocol regression.
        before = self._read_state()
        with self._read_trap() as (queries, writes):
            reader = SprintReader(self.client, data_dir=self.tmp.name)
            self.assertEqual(queries, [])
            self.assertEqual(reader.list(), [])
            self.assertEqual(reader.list(create=False), [])
            with self.assertRaises(TaskError) as refused:
                reader.show("sprint:missing")
            self.assertEqual((refused.exception.code, refused.exception.message, refused.exception.exit_code),
                             ("not_found", "sprint was not found", 2))
            self.assertTrue(queries)
            self.assertEqual(writes, [])
        self.assertEqual(self._read_state(), before)

    def test_established_default_reads_linked_cards_comments_and_freshness_write_nothing(self) -> None:
        self._create()
        # This fixture intentionally has no seeded Cards. Arrange the linked card through
        # the public writer before starting statement/audit measurement.
        with as_observer("sprint:atomic"):
            TaskWriter(self.client, data_dir=self.tmp.name).create(
                role="observer", actor="observer", project="ummanu", task_type="research", title="read card",
                reference="ummanu-1", sprint="sprint:atomic", request_id="read-card",
            )
        self.writer.comment(role="po", actor="operator", reference="sprint:atomic", body="read comment", request_id="read-comment")
        self.writer.resume(role="po", actor="operator", reference="sprint:atomic", entry=self._resume_entry(), request_id="read-resume")
        before = self._read_state()
        with self._read_trap() as (queries, writes):
            reader = SprintReader(self.client, data_dir=self.tmp.name)
            listed = reader.list()
            self.assertEqual([s["ref"] for s in listed], ["sprint:atomic"])
            default = reader.show("sprint:atomic")
            self.assertEqual([card["ref"] for card in default["cards"]], ["ummanu-1"])
            self.assertEqual([entry["body"] for entry in default["comments"]],
                             ["[po]\nread comment", "[sprint:resume]\ncontinue"])
            self.assertIn("resume_freshness", default)
            explicit = reader.show("sprint:atomic", include_cards=True, include_comments=False, include_resume_freshness=True)
            self.assertEqual(json.dumps(default), json.dumps(explicit))
            comments = reader.show("sprint:atomic", include_comments=True)
            self.assertEqual([entry["body"] for entry in comments["comments"]],
                             ["[po]\nread comment", "[sprint:resume]\ncontinue"])
            shallow = reader.show("sprint:atomic", include_cards=False, include_resume_freshness=False)
            self.assertNotIn("cards", shallow)
            self.assertNotIn("resume_freshness", shallow)
            self.assertTrue(queries)
            self.assertEqual(writes, [])
        self.assertEqual(self._read_state(), before)

    def test_explicit_writer_create_and_restore_initialize_once_and_replay(self) -> None:
        with mock.patch.object(sprints, "ensure_sprint_board", wraps=sprints.ensure_sprint_board) as ensure:
            first = self._create()
            replay = self._create()
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(json.dumps(first), json.dumps(replay))
        self.assertEqual(self.client._query("SELECT count(*) FROM sprints"), [(1,)])
        options = {"reference": "sprint:restored", "goal": "restored", "status": "closed",
                   "observer": {"kind": "none"}, "request_id": "restore-positive"}
        with mock.patch.object(sprints, "ensure_sprint_board", wraps=sprints.ensure_sprint_board) as ensure:
            restored = self.writer.restore_create(**options)
            replay = self.writer.restore_create(**options)
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(json.dumps(restored), json.dumps(replay))
        self.assertEqual(self.client._query("SELECT count(*) FROM sprints"), [(2,)])
        self.assertEqual(self.client._query("SELECT count(*) FROM requests WHERE request_id='restore-positive' AND status='committed'"), [(1,)])

    def test_sql_local_run_authority_survives_new_reader_and_rollback(self) -> None:
        from ummanu.sprints import SprintReader

        entries = [{"project": "ummanu", "argv": ["docker", "run", "two words"], "rationale": "owner's probe"}]
        options = {"role": "po", "actor": "operator", "goal": "declared", "product": "ummanu",
                   "issues": ["issue:open"], "projects": ["ummanu"], "observer": {"kind": "none"},
                   "reference": "sprint:atomic", "request_id": "local-sql", "local_run_exceptions": entries}
        original = self.client.sprints._replace_relations
        with mock.patch.object(self.client.sprints, "_replace_relations", side_effect=self._after(original)), self.assertRaises(TaskError):
            self.writer.create(**options)
        self._assert_no_request("local-sql")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprints"), [(0,)])
        first = self.writer.create(**options)
        self.assertEqual(self.client._query("SELECT local_run_exceptions FROM sprints WHERE ref=%s", ("sprint:atomic",)), [(entries,)])
        self.assertEqual(SprintReader(self.client).show("sprint:atomic")["local_run_exceptions"], entries)
        self.assertEqual(self.writer.create(**options)["event_id"], first["event_id"])
        with self.assertRaises(TaskError):
            self.writer.create(**{**options, "local_run_exceptions": []})

    def test_released_staged_create_without_field_replays_at_empty_default(self) -> None:
        intent = self.writer._create_intent(
            role="po", actor="operator", goal="old staged request", definition_of_done="",
            repositories=[], product="ummanu", issues=["issue:open"], reservations=["ummanu"],
            reference="sprint:atomic", observer={"kind": "none"},
        ).to_document()
        self.assertNotIn("local_run_exceptions", intent)
        event = self.writer._event("created", "po", "operator", "sprint:atomic", "old-staged", {"intent": intent})
        self.writer.audit.stage("old-staged", event)
        options = {"role": "po", "actor": "operator", "goal": "old staged request", "product": "ummanu",
                   "issues": ["issue:open"], "projects": ["ummanu"], "observer": {"kind": "none"},
                   "reference": "sprint:atomic", "request_id": "old-staged", "local_run_exceptions": []}
        result = self.writer.create(**options)
        self.assertEqual(result["event_id"], event["event_id"])
        self.assertEqual(result["sprint"]["local_run_exceptions"], [])
        self.assertEqual(self.writer.create(**options)["event_id"], event["event_id"])

    @staticmethod
    def _after(original):
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("deterministic failure point")

        return fail

    @staticmethod
    def _resume_entry() -> dict[str, str]:
        return {
            "selected_step": "continue", "selected_why": "safe",
            "rejected_alternatives": "none", "current_task": "none",
            "dod_state": "pending", "next_safe_step": "run tests",
            "recorded_at": "2026-09-08T00:00:00Z",
        }

    def test_create_rolls_back_after_request_claim_and_retries_once(self) -> None:
        with (
            mock.patch.object(
                self.client.sprints, "create", side_effect=RuntimeError("after claim")
            ),
            self.assertRaises(TaskError),
        ):
            self._create()
        self._assert_no_request("atomic-create")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprints"), [(0,)])
        self._create()
        self.assertEqual(self.client._query("SELECT count(*) FROM sprints"), [(1,)])

    def test_create_rolls_back_entity_relations_and_reservation(self) -> None:
        original = self.client.sprints._replace_relations
        with mock.patch.object(
            self.client.sprints, "_replace_relations", side_effect=self._after(original)
        ), self.assertRaises(TaskError):
            self._create()
        self._assert_no_request("atomic-create")
        for table in ("sprints", "sprint_repositories", "sprint_issues", "sprint_projects"):
            self.assertEqual(self.client._query(f"SELECT count(*) FROM {table}"), [(0,)])
        self._create()
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_projects WHERE reserved"), [(1,)])

    def test_comment_rolls_back_body_claim_and_event(self) -> None:
        self._create()
        original = self.client.sprints.create_comment
        with mock.patch.object(
            self.client.sprints, "create_comment", side_effect=self._after(original)
        ), self.assertRaises(TaskError):
            self.writer.comment(
                role="po", actor="operator", reference="sprint:atomic",
                body="one body", request_id="atomic-comment",
            )
        self._assert_no_request("atomic-comment")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(0,)])
        self.writer.comment(
            role="po", actor="operator", reference="sprint:atomic",
            body="one body", request_id="atomic-comment",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(1,)])

    def test_comment_rolls_back_after_claim_before_body_and_retries(self) -> None:
        self._create()
        with mock.patch.object(
            self.client.sprints, "create_comment", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.comment(
                role="po", actor="operator", reference="sprint:atomic",
                body="one body", request_id="atomic-comment-claim",
            )
        self._assert_no_request("atomic-comment-claim")
        self.writer.comment(
            role="po", actor="operator", reference="sprint:atomic",
            body="one body", request_id="atomic-comment-claim",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(1,)])

    def test_resume_rolls_back_resume_comment_claim_and_event(self) -> None:
        self._create()
        entry = self._resume_entry()
        original = self.client.sprints.create_comment
        with mock.patch.object(
            self.client.sprints, "create_comment", side_effect=self._after(original)
        ), self.assertRaises(TaskError):
            self.writer.resume(
                role="po", actor="operator", reference="sprint:atomic",
                entry=entry, request_id="atomic-resume",
            )
        self._assert_no_request("atomic-resume")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_resumes"), [(0,)])
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(0,)])
        self.writer.resume(
            role="po", actor="operator", reference="sprint:atomic",
            entry=entry, request_id="atomic-resume",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_resumes"), [(1,)])

    def test_resume_rolls_back_after_claim_before_relations_and_retries(self) -> None:
        self._create()
        with mock.patch.object(
            self.client.sprints, "_apply", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.resume(
                role="po", actor="operator", reference="sprint:atomic",
                entry=self._resume_entry(), request_id="atomic-resume-claim",
            )
        self._assert_no_request("atomic-resume-claim")
        self.writer.resume(
            role="po", actor="operator", reference="sprint:atomic",
            entry=self._resume_entry(), request_id="atomic-resume-claim",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_resumes"), [(1,)])

    def test_budget_rolls_back_occurrence_claim_and_event(self) -> None:
        self._create()
        original = self.client.sprints._apply
        with (
            mock.patch.object(self.client.sprints, "_apply", side_effect=self._after(original)),
            self.assertRaises(TaskError),
        ):
            self.writer.record_budget(
                role="po", actor="operator", reference="sprint:atomic",
                event_type="red_ci", request_id="atomic-budget",
            )
        self._assert_no_request("atomic-budget")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_budget_events"), [(0,)])
        self.writer.record_budget(
            role="po", actor="operator", reference="sprint:atomic",
            event_type="red_ci", request_id="atomic-budget",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_budget_events"), [(1,)])

    def test_budget_rolls_back_after_claim_before_occurrence_and_retries(self) -> None:
        self._create()
        with mock.patch.object(
            self.client.sprints, "_apply", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.record_budget(
                role="po", actor="operator", reference="sprint:atomic",
                event_type="red_ci", request_id="atomic-budget-claim",
            )
        self._assert_no_request("atomic-budget-claim")
        self.writer.record_budget(
            role="po", actor="operator", reference="sprint:atomic",
            event_type="red_ci", request_id="atomic-budget-claim",
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_budget_events"), [(1,)])

    def test_close_rolls_back_decisions_transition_and_releases(self) -> None:
        self._create()
        decisions = close_decisions(self.writer, "sprint:atomic")
        original = self.writer._transition_host
        with (
            mock.patch.object(
                self.writer, "_transition_host", side_effect=self._after(original)
            ),
            self.assertRaises(TaskError),
        ):
            self.writer.close(
                role="po", actor="operator", reference="sprint:atomic",
                decisions=decisions, request_id="atomic-close",
            )
        self._assert_no_request("atomic-close")
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_decisions"), [(0,)])
        self.assertEqual(self.client._query("SELECT status FROM sprints"), [("open",)])
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_projects WHERE reserved"), [(1,)])
        self.writer.close(
            role="po", actor="operator", reference="sprint:atomic",
            decisions=decisions, request_id="atomic-close",
        )
        self.assertEqual(self.client._query("SELECT status FROM sprints"), [("closed",)])

    def test_close_rolls_back_after_claim_before_decisions_and_retries(self) -> None:
        self._create()
        decisions = close_decisions(self.writer, "sprint:atomic")
        with mock.patch.object(
            self.client.sprints, "save_close", side_effect=RuntimeError("after claim")
        ), self.assertRaises(TaskError):
            self.writer.close(
                role="po", actor="operator", reference="sprint:atomic",
                decisions=decisions, request_id="atomic-close-claim",
            )
        self._assert_no_request("atomic-close-claim")
        self.writer.close(
            role="po", actor="operator", reference="sprint:atomic",
            decisions=decisions, request_id="atomic-close-claim",
        )
        self.assertEqual(self.client._query("SELECT status FROM sprints"), [("closed",)])

    def test_reservation_rolls_back_at_both_boundaries_and_retries(self) -> None:
        self.writer.restore_create(
            reference="sprint:atomic", goal="reservation target", request_id="seed-reservation"
        )
        for suffix, side_effect in (
            ("claim", RuntimeError("after claim")),
            ("relations", self._after(self.client.sprints._replace_relations)),
        ):
            with self.subTest(boundary=suffix):
                request_id = f"atomic-reservation-{suffix}"
                with mock.patch.object(
                    self.client.sprints, "_replace_relations", side_effect=side_effect
                ), self.assertRaises(TaskError):
                    self.writer.restore(
                        reference="sprint:atomic",
                        values={"sprint_reservations": json.dumps(["ummanu"])},
                        request_id=request_id,
                    )
                self._assert_no_request(request_id)
                self.writer.restore(
                    reference="sprint:atomic",
                    values={"sprint_reservations": json.dumps(["ummanu"])},
                    request_id=request_id,
                )
                self.assertEqual(
                    self.client._query("SELECT count(*) FROM sprint_projects WHERE reserved"), [(1,)]
                )
                self.client._execute("DELETE FROM sprint_projects")
                self.client.connection.commit()

    def _reservation_rows(self, reference: str) -> list[tuple]:
        return self.client._query(
            "SELECT project_id, reserved, ordinal, released_at IS NOT NULL "
            "FROM sprint_projects WHERE sprint_ref=%s ORDER BY project_id",
            (reference,),
        )

    def _seed_two_reservations(self, reference: str = "sprint:reservation-replace") -> str:
        self.writer.restore_create(
            reference=reference,
            goal="reservation replacement",
            request_id=f"seed-{reference}",
        )
        self.writer.restore(
            reference=reference,
            values={"sprint_reservations": json.dumps(["ummanu", "other"])},
            request_id=f"seed-reservations-{reference}",
        )
        return reference

    def test_restore_replaces_the_active_reservation_set_and_replay_is_exact(self) -> None:
        reference = self._seed_two_reservations()

        first = self.writer.restore(
            reference=reference,
            values={"sprint_reservations": json.dumps(["other"])},
            request_id="narrow-reservations",
        )
        rows = self._reservation_rows(reference)
        second = self.writer.restore(
            reference=reference,
            values={"sprint_reservations": json.dumps(["other"])},
            request_id="narrow-reservations",
        )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(self._reservation_rows(reference), rows)
        self.assertEqual(
            rows,
            [("other", True, 0, False), ("ummanu", False, 0, True)],
        )
        self.assertEqual(first["sprint"]["reservations"], ["other"])

    def test_reservation_narrowing_rolls_back_at_both_boundaries(self) -> None:
        for suffix in ("claim", "relations"):
            with self.subTest(boundary=suffix):
                self.client = self.make_ownership_client()
                self.writer = SprintWriter(
                    self.client, data_dir=self.tmp.name, instance=self.instance
                )
                reference = self._seed_two_reservations(f"sprint:narrow-{suffix}")
                before = self._reservation_rows(reference)
                original = self.client.sprints._replace_relations
                effect = RuntimeError("after claim") if suffix == "claim" else self._after(original)
                request_id = f"narrow-rollback-{suffix}"

                with mock.patch.object(
                    self.client.sprints, "_replace_relations", side_effect=effect
                ), self.assertRaises(TaskError):
                    self.writer.restore(
                        reference=reference,
                        values={"sprint_reservations": json.dumps(["other"])},
                        request_id=request_id,
                    )

                self._assert_no_request(request_id)
                self.assertEqual(self._reservation_rows(reference), before)
                self.writer.restore(
                    reference=reference,
                    values={"sprint_reservations": '["other"]'},
                    request_id=request_id,
                )
                self.assertEqual(
                    self._reservation_rows(reference),
                    [("other", True, 0, False), ("ummanu", False, 0, True)],
                )

    def test_restore_cannot_take_a_project_reserved_by_another_sprint(self) -> None:
        holder = self._seed_two_reservations("sprint:reservation-holder")
        contender = "sprint:reservation-contender"
        self.writer.restore_create(
            reference=contender, goal="contender", request_id="seed-contender"
        )

        with self.assertRaises(TaskError) as raised:
            self.writer.restore(
                reference=contender,
                values={"sprint_reservations": json.dumps(["other"])},
                request_id="contested-reservation",
            )

        self.assertEqual(raised.exception.code, "backend_error")
        self._assert_no_request("contested-reservation")
        self.assertEqual(self._reservation_rows(contender), [])
        self.assertEqual(
            [row for row in self._reservation_rows(holder) if row[0] == "other"],
            [("other", True, 1, False)],
        )

    def test_restore_create_rolls_back_at_both_boundaries_and_retries(self) -> None:
        for suffix in ("claim", "relations"):
            with self.subTest(boundary=suffix):
                reference = f"sprint:restore-{suffix}"
                request_id = f"atomic-restore-create-{suffix}"
                target = "create" if suffix == "claim" else "_replace_relations"
                original = getattr(self.client.sprints, target)
                effect = RuntimeError("after claim") if suffix == "claim" else self._after(original)
                with mock.patch.object(
                    self.client.sprints, target, side_effect=effect
                ), self.assertRaises(TaskError):
                    self.writer.restore_create(
                        reference=reference, goal="restored", repositories=["/repo"],
                        request_id=request_id,
                    )
                self._assert_no_request(request_id)
                self.assertEqual(
                    self.client._query("SELECT count(*) FROM sprints WHERE ref=%s", (reference,)), [(0,)]
                )
                self.writer.restore_create(
                    reference=reference, goal="restored", repositories=["/repo"],
                    request_id=request_id,
                )
                self.assertEqual(
                    self.client._query("SELECT count(*) FROM sprints WHERE ref=%s", (reference,)), [(1,)]
                )

    def test_reopen_rolls_back_at_both_boundaries_and_retries(self) -> None:
        for suffix in ("claim", "entity"):
            with self.subTest(boundary=suffix):
                client = self.make_ownership_client()
                writer = SprintWriter(client, data_dir=self.tmp.name, instance=self.instance)
                ref = writer.create(
                    role="po", actor="operator", goal="reopen", repositories=["/repo"],
                    product="ummanu", issues=["issue:open"], projects=["ummanu"],
                    observer=head_choice("codex-observer"), reference=f"sprint:reopen-{suffix}",
                    request_id=f"seed-reopen-{suffix}",
                )["sprint"]["ref"]
                writer.close(
                    role="po", actor="operator", reference=ref,
                    decisions=close_decisions(writer, ref), request_id=f"close-reopen-{suffix}",
                )
                original = writer._transition_host
                effect = RuntimeError("after claim") if suffix == "claim" else self._after(original)
                request_id = f"atomic-reopen-{suffix}"
                with mock.patch.object(
                    writer, "_transition_host", side_effect=effect
                ), self.assertRaises(TaskError):
                    writer.reopen(
                        role="po", actor="operator", reference=ref,
                        observer=head_choice("codex-observer"), request_id=request_id,
                    )
                self.assertEqual(
                    client._query("SELECT count(*) FROM requests WHERE request_id=%s", (request_id,)), [(0,)]
                )
                self.assertEqual(client._query("SELECT status FROM sprints WHERE ref=%s", (ref,)), [("closed",)])
                writer.reopen(
                    role="po", actor="operator", reference=ref,
                    observer=head_choice("codex-observer"), request_id=request_id,
                )
                self.assertEqual(client._query("SELECT status FROM sprints WHERE ref=%s", (ref,)), [("open",)])


class SqlTransportNamespaceTests(SprintFixture):
    def test_canonical_and_unicode_digit_refs_are_distinct_and_lossless(self) -> None:
        writer = SprintWriter(self.client, data_dir=self.tmp.name)
        ascii_ref = writer.restore_create(
            reference="sprint:1", goal="ASCII", request_id="ascii-ref"
        )["sprint"]["ref"]
        unicode_ref = writer.restore_create(
            reference="sprint:١", goal="Unicode", request_id="unicode-ref"
        )["sprint"]["ref"]

        self.assertEqual((ascii_ref, unicode_ref), ("sprint:1", "sprint:١"))
        self.assertEqual(
            self.client._query(
                "SELECT ref, sprint_number FROM sprints "
                "WHERE ref IN (%s,%s) ORDER BY ref",
                (ascii_ref, unicode_ref),
            ),
            [("sprint:1", 1), ("sprint:١", None)],
        )
        self.assertNotEqual(sprint_key(ascii_ref), sprint_key(unicode_ref))

    def test_a_leading_zero_ref_is_rejected_before_sql_persistence(self) -> None:
        writer = SprintWriter(self.client, data_dir=self.tmp.name)

        with self.assertRaisesRegex(TaskError, "must be canonical") as raised:
            writer.restore_create(
                reference="sprint:01", goal="alias", request_id="leading-zero-ref"
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(
            self.client._query("SELECT count(*) FROM sprints WHERE ref='sprint:01'"), [(0,)]
        )
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM requests WHERE request_id='leading-zero-ref'"
            ),
            [(0,)],
        )

    def test_ummanu_5_and_sprint_5_never_cross_dispatch(self) -> None:
        self.client = self.make_ownership_client()
        card_key = self.client.call(
            "createTask", project_id=1, title="card five", reference="ummanu-5", column_id=2
        )
        self.client.call(
            "saveTaskMetadata", task_id=card_key,
            values={"project": "ummanu", "task_type": "code", "worker_profile": "codex-high"},
        )
        self.client.call("createComment", task_id=card_key, content="card comment")
        writer = SprintWriter(self.client, data_dir=self.tmp.name)
        writer.restore_create(
            reference="sprint:5", goal="goal", definition_of_done="dod",
            request_id="transport-sprint-create",
        )

        key = sprint_key("sprint:5")
        self.assertNotEqual(key, card_key)
        self.assertEqual(
            self.client.call("getTaskMetadata", task_id=card_key)["worker_profile"], "codex-high"
        )
        self.assertEqual(
            [row["comment"] for row in self.client.call("getAllComments", task_id=card_key)],
            ["card comment"],
        )
        self.assertEqual(self.client.call("getTaskMetadata", task_id=key)["sprint_goal"], "goal")

        self.client.call(
            "saveTaskMetadata", task_id=card_key, values={"worker_profile": "claude-opus"}
        )
        self.client.call("createComment", task_id=card_key, content="second card comment")
        self.client.call("updateTask", id=card_key, title="card five edited")
        self.client.call("closeTask", task_id=card_key)

        self.assertEqual(
            self.client._query("SELECT title, archived FROM tasks WHERE task_ref='ummanu-5'"),
            [("card five edited", True)],
        )
        self.assertEqual(
            self.client._query("SELECT body FROM task_comments WHERE task_ref='ummanu-5' ORDER BY comment_id"),
            [("card comment",), ("second card comment",)],
        )
        self.assertEqual(self.client._query("SELECT count(*) FROM sprint_comments"), [(0,)])
        self.assertEqual(self.client._query("SELECT status FROM sprints WHERE ref='sprint:5'"), [("open",)])

    def test_restore_current_task_never_attaches_or_reparents_a_card(self) -> None:
        self.client = self.make_sprint_client()
        writer = SprintWriter(self.client, data_dir=self.tmp.name)
        writer.restore_create(
            reference="sprint:900", goal="cursor target", request_id="cursor-create"
        )

        with self.assertRaisesRegex(TaskError, "not a Card linked to sprint:900") as raised:
            writer.restore(
                reference="sprint:900",
                values={"sprint_current_task": "ummanu-12"},
                request_id="cursor-restore",
            )
        self.assertEqual(raised.exception.code, "backend_error")

        self.assertEqual(
            self.client._query("SELECT sprint_ref FROM tasks WHERE task_ref='ummanu-12'"),
            [(None,)],
        )
        self.assertEqual(
            self.client._query("SELECT current_task_ref FROM sprints WHERE ref='sprint:900'"),
            [(None,)],
        )
        self.assertEqual(
            self.client._query("SELECT count(*) FROM requests WHERE request_id='cursor-restore'"),
            [(0,)],
        )
