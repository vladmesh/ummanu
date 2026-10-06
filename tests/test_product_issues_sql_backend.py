"""Product/Issue atomicity probes on PostgreSQL 16.

The shared Product/Issue contract (tests/test_product_issues.py) runs on the store itself since
secretary-1670, so this module holds only the probes that name tables or inject a failure between
two statements of one transaction.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unittest
from dataclasses import replace
from unittest import mock

from tests import test_product_issues as shared
from tests.product_issue_fixtures import ProductIssueFixture
from ummanu.board import backend
from ummanu.cli import main
from ummanu.tasks import TaskError


class SqlProductIssueTransactionTests(ProductIssueFixture, unittest.TestCase):
    """What one Product/Issue mutation's PostgreSQL transaction commits, and what it rolls back."""

    def _counts(self, request_id: str, *, reference: str = "") -> tuple[int, int, int, int]:
        client = self.client
        product_id = reference.removeprefix("product:") if reference.startswith("product:") else ""
        issue_id = reference.removeprefix("issue:") if reference.startswith("issue:") else ""
        return (
            int(client._query("SELECT count(*) FROM requests WHERE request_id = %s", (request_id,))[0][0]),
            int(client._query("SELECT count(*) FROM board_events WHERE request_id = %s", (request_id,))[0][0]),
            int(client._query("SELECT count(*) FROM products WHERE product_id = %s", (product_id,))[0][0]),
            int(client._query("SELECT count(*) FROM issues WHERE issue_id = %s", (issue_id,))[0][0]),
        )

    def _create_product(self, request_id: str = "product") -> dict:
        return self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id=request_id,
        )

    def test_failure_after_claim_rolls_back_and_same_request_retries_once(self) -> None:
        original = self.client.records.create

        def fail_after_stage(**values):
            original(**values)
            raise TaskError("backend_error", "injected after claim", 1)

        with (
            mock.patch.object(self.client.records, "create", side_effect=fail_after_stage),
            self.assertRaises(TaskError) as raised,
        ):
            self._create_product("claim-failure")
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("claim-failure", reference="product:ummanu"), (0, 0, 0, 0))
        self._create_product("claim-failure")
        self.assertEqual(self._counts("claim-failure", reference="product:ummanu"), (1, 1, 1, 0))

    def test_failure_after_entity_and_relationship_rolls_back(self) -> None:
        original = self.client.records.save_metadata

        def fail_after_entity(task_id, values):
            original(task_id, values)
            raise TaskError("backend_error", "injected after entity", 1)

        with (
            mock.patch.object(self.client.records, "save_metadata", side_effect=fail_after_entity),
            self.assertRaises(TaskError) as raised,
        ):
            self._create_product("entity-failure")
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("entity-failure", reference="product:ummanu"), (0, 0, 0, 0))
        self.assertEqual(self.client._query("SELECT count(*) FROM product_projects"), [(0,)])
        self._create_product("entity-failure")
        self.assertEqual(self._counts("entity-failure", reference="product:ummanu"), (1, 1, 1, 0))

    def test_failure_after_comment_rolls_back_comment_effect_and_claim(self) -> None:
        self._create_product("seed-product")
        issue = self.store.create_issue(
            product="ummanu", issue_kind="bug", priority="P2", title="Crash",
            description="", actor="po", request_id="seed-issue",
        )
        original = self.client.records.create_comment

        def fail_after_comment(task_id, content):
            original(task_id, content)
            raise TaskError("backend_error", "injected after comment", 1)

        with (
            mock.patch.object(self.client.records, "create_comment", side_effect=fail_after_comment),
            self.assertRaises(TaskError) as raised,
        ):
            self.store.update_priority(
                reference=issue["ref"], priority="P0", reason="urgent", actor="po",
                request_id="comment-failure",
            )
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("comment-failure"), (0, 0, 0, 0))
        self.assertEqual(self.client._query("SELECT count(*) FROM issue_comments"), [(0,)])
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P2")
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po",
            request_id="comment-failure",
        )
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P0")
        self.assertEqual(self._counts("comment-failure"), (1, 1, 0, 0))
        self.assertEqual(self.client._query("SELECT count(*) FROM issue_comments"), [(1,)])

    def test_failure_after_event_insert_rolls_back_every_row(self) -> None:
        audit = self.store.audit
        original = audit._write_board_event

        def fail_after_event(request_id, event):
            original(request_id, event)
            raise RuntimeError("injected after event")

        with (
            mock.patch.object(audit, "_write_board_event", side_effect=fail_after_event),
            self.assertRaises(TaskError) as raised,
        ):
            self._create_product("event-failure")
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._counts("event-failure", reference="product:ummanu"), (0, 0, 0, 0))
        self._create_product("event-failure")
        self.assertEqual(self._counts("event-failure", reference="product:ummanu"), (1, 1, 1, 0))

    def test_unknown_product_and_issue_metadata_round_trips_without_overriding_known_fields(self) -> None:
        with self.client.transaction():
            product_key = self.client.call(
                "createTask", project_id=1, title="Ummanu", description="", column_id=1,
                swimlane_id=0, reference="product:ummanu",
            )
            self.client.call(
                "saveTaskMetadata", task_id=product_key,
                values={"record_type": "product", "product_id": "ummanu",
                        "product_projects": '["ummanu"]', "future_product": "kept",
                        "created_empty": "", "swimlane": "observed-product-lane"},
            )
            issue_key = self.client.call(
                "createTask", project_id=1, title="Crash", description="", column_id=1,
                swimlane_id=0, reference="issue:abc",
            )
            self.client.call(
                "saveTaskMetadata", task_id=issue_key,
                values={"record_type": "issue", "issue_product": "ummanu",
                        "issue_kind": "bug", "issue_priority": "P2", "future_issue": "kept",
                        "created_empty": "", "swimlane": "observed-issue-lane"},
            )
        product_meta = self.client.call("getTaskMetadata", task_id=product_key)
        self.assertEqual(product_meta["future_product"], "kept")
        self.assertEqual(product_meta["created_empty"], "")
        self.assertEqual(product_meta["swimlane"], "observed-product-lane")
        issue_meta = self.client.call("getTaskMetadata", task_id=issue_key)
        self.assertEqual(issue_meta["future_issue"], "kept")
        self.assertEqual(issue_meta["created_empty"], "")
        self.assertEqual(issue_meta["swimlane"], "observed-issue-lane")
        self.assertEqual(issue_meta["issue_priority"], "P2")

        with self.client.transaction():
            self.client.call(
                "saveTaskMetadata",
                task_id=product_key,
                values={"product_id": "ummanu", "future_product": "", "swimlane": ""},
            )
            self.client.call(
                "saveTaskMetadata",
                task_id=issue_key,
                values={"issue_priority": "P1", "future_issue": "", "swimlane": ""},
            )
        product_meta = self.client.call("getTaskMetadata", task_id=product_key)
        issue_meta = self.client.call("getTaskMetadata", task_id=issue_key)
        self.assertEqual(product_meta["future_product"], "")
        self.assertEqual(product_meta["swimlane"], "")
        self.assertEqual(product_meta["product_id"], "ummanu")
        self.assertEqual(issue_meta["future_issue"], "")
        self.assertEqual(issue_meta["swimlane"], "")
        self.assertEqual(issue_meta["issue_priority"], "P1")

    def test_stamped_comments_claim_requests_and_refuse_foreign_entities(self) -> None:
        self._create_product("product-create")
        issue = self.store.create_issue(
            product="ummanu", issue_kind="bug", priority="P2", title="Crash",
            description="", actor="po", request_id="issue-create",
        )
        product_key = backend.record_key("product", "ummanu")
        issue_key = backend.record_key("issue", issue["ref"].removeprefix("issue:"))

        ordinary = self.client.call(
            "createComment", task_id=product_key, content="ordinary product note"
        )
        ordinary_issue = self.client.call(
            "createComment", task_id=issue_key, content="ordinary issue note"
        )
        stamped_body = "[product:note]\ncreated\n[request-id:product-create]"
        stamped = self.client.call("createComment", task_id=product_key, content=stamped_body)
        replay = self.client.call("createComment", task_id=product_key, content=stamped_body)
        self.assertEqual(replay, stamped)
        self.assertEqual(
            self.client._query(
                "SELECT body, request_id FROM product_comments ORDER BY comment_id"
            ),
            [("ordinary product note", None), (stamped_body, "product-create")],
        )

        self.store.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po",
            request_id="issue-priority",
        )
        self.store.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po",
            request_id="issue-priority",
        )
        self.assertEqual(
            self.client._query(
                "SELECT request_id, issue_ref FROM issue_comments WHERE request_id = %s",
                ("issue-priority",),
            ),
            [("issue-priority", issue["ref"])],
        )
        self.assertEqual(
            self.client._query(
                "SELECT request_id FROM issue_comments WHERE comment_id = %s", (ordinary_issue,)
            ),
            [(None,)],
        )

        with self.assertRaises(TaskError), self.client.transaction():
            self.client.call(
                "createComment",
                task_id=issue_key,
                content="[issue:note]\nforeign\n[request-id:product-create]",
            )
        self.assertEqual(
            self.client._query(
                "SELECT count(*) FROM issue_comments WHERE request_id = %s", ("product-create",)
            ),
            [(0,)],
        )
        self.assertIsInstance(ordinary, int)

    def test_reconcile_lanes_is_a_structural_no_op_on_sql(self) -> None:
        self._create_product("lane-product")
        self.store.create_issue(
            product="ummanu", issue_kind="bug", priority="P2", title="Crash",
            description="", actor="po", request_id="lane-issue",
        )

        planned = self.store.reconcile_lanes()
        applied = self.store.reconcile_lanes(apply=True)

        self.assertEqual(planned["moves"], [])
        self.assertEqual(planned["moved"], 0)
        self.assertEqual(applied["moves"], [])
        self.assertEqual(applied["moved"], 0)

    def test_board_key_lookup_is_indexed_and_collision_refuses(self) -> None:
        self._create_product("first")
        first_key = backend.record_key("product", "ummanu")
        statements: list[str] = []
        query = self.client._query

        def traced(sql, params=()):
            statements.append(sql)
            return query(sql, params)

        with mock.patch.object(self.client, "_query", side_effect=traced):
            self.client.call("getTaskMetadata", task_id=first_key)
        # The lookup stays on the indexed key; since the reads went set-based it names the keys
        # as one array parameter, which the unique index on `board_key` answers the same way.
        self.assertTrue(any("WHERE board_key = ANY(%s::bigint[])" in sql for sql in statements))
        self.assertFalse(any(sql.strip() == "SELECT product_id FROM products" for sql in statements))

        with (
            mock.patch("ummanu.board.sql_product_issues.record_key", return_value=first_key),
            self.assertRaises(TaskError),
        ):
            self.store.create_product(
                product_id="other", projects=["ummanu"], title="Other", description="",
                actor="po", request_id="collision",
            )
        self.assertEqual(self.record_count("product:other"), 0)

    def test_record_observation_includes_archived_product_and_closed_issue(self) -> None:
        self._create_product("visible-product")
        issue = self.store.create_issue(
            product="ummanu", issue_kind="bug", priority="P2", title="Crash",
            description="", actor="po", request_id="visible-issue",
        )
        self.store.close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="closed-issue"
        )
        self.client.call("closeTask", task_id=backend.record_key("product", "ummanu"))

        self.assertEqual(self.record_count("product:ummanu"), 1)
        self.assertEqual(self.record_count(issue["ref"]), 1)
        self.assertTrue(self.store.show_product("ummanu")["closed"])
        self.assertTrue(self.store.show_issue(issue["ref"])["closed"])


class SqlProductIssueAppendTransactionTests(ProductIssueFixture, unittest.TestCase):
    """An `issue append` that fails after its description write leaves neither the block nor the claim."""

    ORIGINAL = shared.ProductIssueDescriptionAppendTests.ORIGINAL
    _open_issue = shared.ProductIssueDescriptionAppendTests._open_issue
    _append = shared.ProductIssueDescriptionAppendTests._append

    def _claims(self, request_id: str) -> tuple[int, int]:
        return tuple(
            int(
                self.client._query(f"SELECT count(*) FROM {table} WHERE request_id = %s", (request_id,))[0][0]
            )
            for table in ("requests", "board_events")
        )

    def test_failure_after_description_update_rolls_back_the_block_and_the_claim(self) -> None:
        issue = self._open_issue()
        original = self.client.records.update

        def fail_after_update(task_id, fields):
            original(task_id, fields)
            raise TaskError("backend_error", "injected after description", 1)

        with (
            mock.patch.object(self.client.records, "update", side_effect=fail_after_update),
            self.assertRaises(TaskError) as raised,
        ):
            self._append(issue["ref"], "block", request_id="update-failure")
        self.assertNotEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self._claims("update-failure"), (0, 0))
        self.assertEqual(self.issue(issue["ref"])["description"], self.ORIGINAL)

        self._append(issue["ref"], "block", request_id="update-failure")
        self.assertEqual(self._claims("update-failure"), (1, 1))
        self.assertEqual(self.issue(issue["ref"])["description"].count("[issue:appended "), 1)


class SqlBackendProductIssueKeyTests(unittest.TestCase):
    def test_postgres_serves_product_issue_and_sprint(self) -> None:
        self.assertIn(backend.PRODUCT_ISSUE, backend.POSTGRES_SERVES)
        self.assertIn(backend.SPRINT, backend.POSTGRES_SERVES)

    def test_record_keys_are_stable_disjoint_and_not_card_numbers(self) -> None:
        numbered_sprint = backend.record_key("sprint", "sprint:1596")
        custom_sprint = backend.record_key("sprint", "sprint:canary")
        product = backend.record_key("product", "ummanu")
        issue = backend.record_key("issue", "ummanu")
        self.assertEqual(product, backend.record_key("product", "ummanu"))
        self.assertEqual(backend.record_key_kind(product), "product")
        self.assertEqual(backend.record_key_kind(issue), "issue")
        self.assertEqual(backend.record_key_kind(numbered_sprint), "sprint")
        self.assertEqual(backend.record_key_kind(custom_sprint), "sprint")
        self.assertEqual(len({1596, numbered_sprint, custom_sprint, product, issue}), 5)
        self.assertIsNone(backend.record_key_kind(1596))
        self.assertNotEqual(product, issue)


class SqlIssueDescriptionEditTests(ProductIssueFixture, unittest.TestCase):
    ORIGINAL = shared.ProductIssueDescriptionAppendTests.ORIGINAL
    _open_issue = shared.ProductIssueDescriptionAppendTests._open_issue
    _append = shared.ProductIssueDescriptionAppendTests._append
    _claims = SqlProductIssueAppendTransactionTests._claims

    def edit(self, ref, description, *, request_id="edit", **values):
        return self.store.edit_description(reference=ref, description=description,
                                           reason="correct", actor="po", request_id=request_id, **values)

    def invoke(self, ref, flags, *, role="po", request_id="cli"):
        out, err = io.StringIO(), io.StringIO()
        with (mock.patch("ummanu.product_issue_commands.board_client", return_value=self.client),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            code = main(["issue", "edit", "--ref", ref, "--role", role, "--actor", "po",
                         "--reason", "correct", "--request-id", request_id, "--instance", str(self.root),
                         "--data-dir", str(self.root / "data"), *flags])
        return code, out.getvalue(), err.getvalue()

    def test_cli_edit_exact_readback_preserves_other_fields_comments_and_append_history(self):
        issue = self._open_issue()
        appended = self._append(issue["ref"], "prior evidence", request_id="append")
        self.store.update_priority(reference=issue["ref"], priority="P1", reason="urgent", actor="po",
                                   request_id="priority")
        before = self.issue(issue["ref"])
        text = "  Полная замена\n\nlast line  \n"
        body = self.root / "description.md"
        body.write_text(text, encoding="utf-8")
        code, out, _ = self.invoke(issue["ref"], ["--body-file", str(body)])
        self.assertEqual(code, 0)
        shown = json.loads(out)
        self.assertEqual(shown["description"], text)
        for field in ("title", "product", "kind", "priority", "closed", "close_reason", "history"):
            if field == "history":
                self.assertEqual(shown[field]["comments"], before[field]["comments"])
                self.assertEqual(shown[field]["audit"][:-1], before[field]["audit"])
            else:
                self.assertEqual(shown[field], before[field])
        event = self.store._host().canon.committed("cli")
        self.assertEqual(event.data["edit"], {
            "description_sha256_was": hashlib.sha256(appended["description"].encode()).hexdigest(),
            "description_sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
        self.assertNotIn("append", event.data)
        self.assertEqual(self._claims("cli"), (1, 1))
        self.assertEqual(self.edit(issue["ref"], "", request_id="clear")["description"], "")
        self.edit(issue["ref"], text, request_id="restore-text")
        self.assertEqual(self.invoke(issue["ref"], ["--description", text])[0], 0)
        self.assertEqual(self._claims("cli"), (1, 1))
        self.assertEqual(self.invoke(issue["ref"], ["--description", "changed"])[0], 2)
        self.assertEqual(self.issue(issue["ref"])["description"], text)
        self.assertEqual(self._claims("cli"), (1, 1))
        # Append remains a supported operation after a full edit and retains its released evidence.
        after = self._append(issue["ref"], "later evidence", request_id="later-append")
        self.assertTrue(after["description"].startswith(text))
        self.assertIn("append", self.store._host().canon.committed("later-append").data)
        self.assertEqual(self.edit(issue["ref"], text, request_id="cli")["description"], after["description"])

    def test_refusals_do_not_write_and_do_not_claim_request_ids(self):
        issue = self._open_issue()
        before = self.audit_events()
        invalid = self.root / "invalid.md"
        invalid.write_bytes(b"\xff")
        for index, flags in enumerate((["--body-file", str(self.root / "missing")],
                                       ["--body-file", str(invalid)], [],
                                       ["--description", "a", "--body-file", str(invalid)])):
            self.assertEqual(self.invoke(issue["ref"], flags, request_id=f"bad-{index}")[0], 2)
            self.assertEqual(self._claims(f"bad-{index}"), (0, 0))
        for role, expected in (("observer", 3), ("dispatcher", 2)):
            self.assertEqual(self.invoke(issue["ref"], ["--description", "wrong"], role=role,
                                         request_id=role)[0], expected)
            self.assertEqual(self._claims(role), (0, 0))
            with self.assertRaises(TaskError) as refused:
                self.edit(issue["ref"], "wrong", request_id=role, role=role)
            self.assertEqual(refused.exception.code, "role_forbidden")
            self.assertEqual(self._claims(role), (0, 0))
        with self.assertRaises(TaskError) as caught:
            self.store.edit_description(reference=issue["ref"], description="wrong", reason="  ", actor="po",
                                        request_id="empty-reason")
        self.assertEqual(caught.exception.code, "validation")
        self.assertEqual(self._claims("empty-reason"), (0, 0))
        self.assertEqual(self.issue(issue["ref"])["description"], self.ORIGINAL)
        self.assertEqual(self.audit_events(), before)
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        closed = self.issue(issue["ref"])
        self.assertEqual(self.invoke(issue["ref"], ["--description", "wrong"], request_id="closed-edit")[0], 3)
        self.assertEqual(self._claims("closed-edit"), (0, 0))
        self.assertEqual(self.issue(issue["ref"]), closed)

    def test_cross_operation_reuse_and_replay_after_newer_edit_or_close(self):
        issue = self._open_issue()
        self._append(issue["ref"], "evidence", request_id="append")
        self.store.update_priority(reference=issue["ref"], priority="P1", reason="correct", actor="po",
                                   request_id="priority")
        for request_id in ("append", "priority"):
            with self.assertRaises(TaskError) as caught:
                self.edit(issue["ref"], "one", request_id=request_id)
            self.assertEqual(caught.exception.code, "validation")
        first = self.edit(issue["ref"], "one")
        self.edit(issue["ref"], "two", request_id="newer")
        self.assertEqual(self.edit(issue["ref"], "one")["description"], "two")
        self.assertEqual(self.store.retry_transaction("edit")["description"], "two")
        for callback in (lambda: self._append(issue["ref"], "one", request_id="edit", reason="correct"),
                         lambda: self.store.update_priority(reference=issue["ref"], priority=first["priority"],
                                                            reason="correct", actor="po", request_id="edit")):
            with self.assertRaises(TaskError) as caught:
                callback()
            self.assertEqual(caught.exception.code, "validation")
        self.assertEqual(self._claims("edit"), (1, 1))
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        replay = self.edit(issue["ref"], "one")
        self.assertTrue(replay["closed"])
        self.assertEqual(replay["description"], "two")

    def test_native_replace_refuses_other_fields_and_stale_description_evidence(self):
        from ummanu.board import Actor, DescriptionEdit, EntityKind, EventKind, RelatedRefs, Replace

        issue = self._open_issue()
        host = self.store._host()
        current = host.read(EntityKind.ISSUE, issue["ref"])
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        evidence = DescriptionEdit(digest(current.description), digest("edit"))
        for successor in (replace(current, description="edit", title="changed"),
                          replace(current, description="edit", priority="P0")):
            with self.assertRaises(TaskError):
                self.store._host_mutation(lambda successor=successor: host.replace(Replace(successor, Actor("po", "po"), "correct",
                                                                      request_id="refused", description_edit=evidence)))
            self.assertEqual(self._claims("refused"), (0, 0))
        # A staged occurrence's before hash cannot overwrite a newer description during recovery.
        successor = replace(current, description="edit")
        event = host._entity_event(EventKind.ENTITY_UPDATED,
                                   successor, Actor("po", "po"), "correct", host._related(successor, RelatedRefs()),
                                   "staged", edit=evidence)
        self.edit(issue["ref"], "newer", request_id="newer")
        with self.client.transaction():
            host.canon.stage("staged", event)
        with self.assertRaises(TaskError):
            self.store.retry_transaction("staged")
        self.assertEqual(self.issue(issue["ref"])["description"], "newer")
        self.assertEqual(self._claims("staged"), (1, 0))

    def test_backend_failure_rolls_back_description_claim_and_event_then_retries_once(self):
        issue = self._open_issue()
        for stage in ("description", "event"):
            owner = self.client.records if stage == "description" else self.store.audit
            method = "update" if stage == "description" else "_write_board_event"
            original = getattr(owner, method)

            def fail(*args, original=original, **kwargs):
                original(*args, **kwargs)
                raise TaskError("backend_error", "injected after write", 1)

            before = self.issue(issue["ref"])
            with mock.patch.object(owner, method, side_effect=fail), self.assertRaises(TaskError) as caught:
                self.edit(issue["ref"], stage, request_id=stage)
            self.assertNotEqual(caught.exception.code, "audit_pending")
            self.assertEqual(self.issue(issue["ref"]), before)
            self.assertEqual(self._claims(stage), (0, 0))
            self.assertEqual(self.edit(issue["ref"], stage, request_id=stage)["description"], stage)
            self.assertEqual(self.edit(issue["ref"], stage, request_id=stage)["description"], stage)
            self.assertEqual(self._claims(stage), (1, 1))

    def test_pending_edit_recovery_confirms_the_exact_description_without_rewriting_it(self):
        from ummanu.board import Actor, DescriptionEdit, EntityKind, EventKind, RelatedRefs

        issue = self._open_issue()
        host = self.store._host()
        current = host.read(EntityKind.ISSUE, issue["ref"])
        desired = replace(current, description="pending text")
        evidence = DescriptionEdit(hashlib.sha256(current.description.encode()).hexdigest(),
                                   hashlib.sha256(desired.description.encode()).hexdigest())
        event = host._entity_event(EventKind.ENTITY_UPDATED, desired, Actor("po", "po"), "correct",
                                   host._related(desired, RelatedRefs()), "pending-edit", edit=evidence)
        with self.client.transaction():
            host.canon.stage("pending-edit", event)
            row = self.client.call("getTaskByReference", project_id=1, reference=issue["ref"])
            self.client.call("updateTask", id=row["id"], description=desired.description)
        self.assertEqual(self._claims("pending-edit"), (1, 0))
        with mock.patch.object(self.client.records, "update", side_effect=AssertionError("recovery rewrote text")):
            recovered = self.edit(issue["ref"], desired.description, request_id="pending-edit")
        self.assertEqual(recovered["description"], desired.description)
        self.assertEqual(self._claims("pending-edit"), (1, 1))
        self.assertEqual(host.canon.committed("pending-edit"), event)

    def test_cli_closed_and_all_filter_products_without_changing_inclusive_store_api(self):
        issue = self._open_issue()
        self.store.create_product(product_id="other", projects=["ummanu"], title="Other", description="",
                                  actor="po", request_id="other-product")
        other = self.store.create_issue(product="other", issue_kind="bug", priority="P2", title="Other issue",
                                        description="", actor="po", request_id="other-issue")
        closed = self.store.create_issue(product="ummanu", issue_kind="bug", priority="P1", title="Closed",
                                         description="", actor="po", request_id="closed-issue")
        self.store.close_issue(reference=closed["ref"], reason="resolved", actor="po", request_id="close")
        self.assertEqual({i["ref"] for i in self.store.list_issues(include_closed=True)},
                         {issue["ref"], other["ref"], closed["ref"]})
        for flags, expected in (([], {issue["ref"]}), (["--closed"], {closed["ref"]}),
                                 (["--all"], {issue["ref"], closed["ref"]})):
            output = io.StringIO()
            with (mock.patch("ummanu.product_issue_commands.board_client", return_value=self.client),
                  contextlib.redirect_stdout(output)):
                code = main(["issue", "list", "--product", "ummanu", "--instance", str(self.root),
                             "--data-dir", str(self.root / "data"), *flags])
            self.assertEqual(code, 0)
            self.assertEqual({i["ref"] for i in json.loads(output.getvalue())}, expected)
