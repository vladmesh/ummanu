from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.observer_identity import as_observer
from tests.product_issue_fixtures import ProductIssueFixture
from ummanu.cli import main
from ummanu.tasks import TaskError, TaskWriter


class ProductIssueSwimlaneTests(ProductIssueFixture, unittest.TestCase):
    """The lane a Product or Issue row takes: the one named after its product."""

    def test_a_record_takes_the_lane_named_after_its_product(self) -> None:
        """Both records land in the `ummanu` lane, and that lane is not the board's first.

        The rule this replaces took the board's first active lane, so it answered lane 4 here by
        coincidence of position rather than by the product.  The board is therefore reordered so
        that the coincidence cannot hold: the product lane is now last, and still chosen.
        """
        store = self.store_with_lanes(
            [
                {"id": 9, "name": "service-template", "position": 1},
                {"id": 7, "name": "codegen-orchestrator", "position": 2},
                {"id": 4, "name": "ummanu", "position": 3},
            ]
        )

        product = self.create_product(
            store=store,
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="live-product",
        )
        issue = self.create_issue(
            store=store,
            product="ummanu",
            issue_kind="bug",
            priority="P1",
            title="Crash",
            description="",
            actor="po",
            request_id="live-issue",
        )

        self.assertEqual(product["id"], "ummanu")
        self.assertEqual(self.lane_binding("product:ummanu", store=store), "ummanu")
        self.assertEqual(self.lane_binding(issue["ref"], store=store), "ummanu")
        self.assertEqual(
            self.request_state(store=store)["transactions"], {"ok": True, "pending": 0}
        )

    def test_the_lane_does_not_depend_on_swimlane_order_or_on_a_default_lane(self) -> None:
        """The same product takes the same lane whatever order the board lists, `Default` first.

        The live Pipeline board has lanes named after projects and a `Default swimlane`; the old
        rule made the record follow whichever of them happened to be first.
        """
        chosen = []
        for order in (
            [
                {"id": 1, "name": "Default swimlane", "position": 1},
                {"id": 4, "name": "ummanu", "position": 2},
                {"id": 9, "name": "service-template", "position": 3},
            ],
            [
                {"id": 9, "name": "service-template", "position": 1},
                {"id": 4, "name": "ummanu", "position": 2},
                {"id": 1, "name": "Default swimlane", "position": 3},
            ],
            # Board order need not agree with the positions the board reports, either.
            [
                {"id": 4, "name": "ummanu", "position": 7},
                {"id": 1, "name": "Default swimlane", "position": 0},
            ],
        ):
            with self.subTest(first=order[0]["name"]), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                (root / "projects").mkdir()
                (root / "projects" / "ummanu.yaml").write_text("id: ummanu\n", encoding="utf-8")
                store = self.store_with_lanes(order, root=root)

                self.create_product(
                    store=store,
                    product_id="ummanu",
                    projects=["ummanu"],
                    title="Ummanu",
                    description="",
                    actor="po",
                    request_id="ordered-product",
                )

                chosen.append(self.lane_binding("product:ummanu", store=store))
        self.assertEqual(chosen, ["ummanu", "ummanu", "ummanu"])

    def test_a_product_without_a_lane_gets_one_named_after_it(self) -> None:
        """`codegen` is bound to two projects and has no lane of its own on the live board.

        Its lane cannot be derived from those bindings, which is why the rule names the lane after
        the product and creates it on demand.
        """
        (self.root / "projects" / "codegen-orchestrator.yaml").write_text(
            "id: codegen-orchestrator\n",
            encoding="utf-8",
        )
        (self.root / "projects" / "service-template.yaml").write_text(
            "id: service-template\n",
            encoding="utf-8",
        )
        store = self.store_with_lanes(self.existing_project_lanes())

        self.create_product(
            store=store,
            product_id="codegen",
            projects=["codegen-orchestrator", "service-template"],
            title="Codegen",
            description="",
            actor="po",
            request_id="codegen-product",
        )
        issue = self.create_issue(
            store=store,
            product="codegen",
            issue_kind="feature",
            priority="P2",
            title="Template drift",
            description="",
            actor="po",
            request_id="codegen-issue",
        )

        self.assertEqual(self.lane_binding("product:codegen", store=store), "codegen")
        self.assertEqual(self.lane_binding(issue["ref"], store=store), "codegen")
        self.assertEqual(
            self.request_state(store=store)["transactions"], {"ok": True, "pending": 0}
        )

    def test_a_board_without_swimlanes_gets_the_product_lane(self) -> None:
        store = self.store_with_lanes([])

        self.create_product(
            store=store,
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="plain-product",
        )

        self.assertEqual(self.lane_binding("product:ummanu", store=store), "ummanu")
        self.assertEqual(
            self.request_state(store=store)["transactions"], {"ok": True, "pending": 0}
        )

    def test_a_repeated_delivery_lands_in_the_lane_the_first_one_chose(self) -> None:
        """The lane is a function of the record, so a redelivered create cannot move it.

        Between the two deliveries the board gains a `Default swimlane` and puts it first, which
        under the lane rule this replaces would have been the lane of the second attempt.
        """
        store = self.store_with_lanes(self.existing_project_lanes())
        self.create_product(
            store=store,
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="redelivered-product",
        )
        issue = self.create_issue(
            store=store,
            product="ummanu",
            issue_kind="bug",
            priority="P0",
            title="Crash",
            description="",
            actor="po",
            request_id="redelivered-issue",
        )

        self.add_external_lane(
            {"id": 33, "name": "Default swimlane", "position": 0}, store=store, first=True
        )
        again = self.create_issue(
            store=store,
            product="ummanu",
            issue_kind="bug",
            priority="P0",
            title="Crash",
            description="",
            actor="po",
            request_id="redelivered-issue",
        )
        product_again = self.create_product(
            store=store,
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="redelivered-product",
        )

        self.assertEqual(again["ref"], issue["ref"])
        self.assertEqual(product_again["id"], "ummanu")
        self.assertEqual(self.lane_binding("product:ummanu", store=store), "ummanu")
        self.assertEqual(self.lane_binding(issue["ref"], store=store), "ummanu")
        self.assertEqual(
            self.request_state(store=store),
            {
                "transactions": {"ok": True, "pending": 0},
                "audit": {"ok": True, "pending": 0},
            },
        )


class ProductIssueStoreTests(ProductIssueFixture, unittest.TestCase):
    """The ProductIssueStore contract, on the store."""

    def test_released_writes_publish_complete_typed_product_issue_events(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="typed-product",
        )
        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="bug",
            priority="P2",
            title="Crash",
            description="",
            actor="po",
            request_id="typed-issue",
        )
        self.store.update_priority(
            reference=issue["ref"],
            priority="P0",
            reason="urgent",
            actor="po",
            request_id="typed-priority",
        )
        self.store.close_issue(
            reference=issue["ref"],
            reason="resolved",
            actor="po",
            request_id="typed-close",
        )
        events = self.audit_events()
        self.assertEqual(
            [event["kind"] for event in events],
            ["entity.created", "entity.created", "entity.updated", "issue.closed"],
        )
        self.assertTrue(all(event["record_type"] == "board.protocol_event" for event in events))
        self.assertEqual(events[1]["related_refs"], ["product:ummanu"])
        self.assertEqual(events[-1]["transition"], {"source": "open", "target": "closed"})
        self.assertEqual(events[-1]["data"]["close_reason"], "resolved")

    def test_an_observer_s_issue_records_its_actor_role_and_sprint(self) -> None:
        """The bound observer files for its sprint's product; the event says who, as what, from where."""
        self.store.create_product(
            product_id="ummanu", projects=["ummanu"], title="Ummanu", description="", actor="po"
        )
        sprint = {"ref": "sprint:1465", "product": "ummanu"}
        with (
            as_observer("sprint:1465"),
            mock.patch("ummanu.sprints.SprintReader.show", return_value=sprint),
        ):
            issue = self.store.create_issue(
                product="",
                issue_kind="improvement",
                priority="P3",
                title="Deferred finding",
                description="outside the sprint's DoD, with evidence",
                actor="observer",
                role="observer",
                request_id="observer-finding",
            )
        self.assertEqual(issue["product"], "ummanu")
        created = [
            event
            for event in self.audit_events()
            if event.get("kind") == "entity.created" and event.get("ref") == issue["ref"]
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["actor"], {"role": "observer", "id": "observer"})
        self.assertEqual(created[0]["related_refs"], ["sprint:1465", "product:ummanu"])
        # The PO's issue keeps its own role, and a PO write in the observer's name files nothing.
        po_issue = self.store.create_issue(
            product="ummanu", issue_kind="bug", priority="P2", title="PO", description="", actor="po"
        )
        po_created = [e for e in self.audit_events() if e.get("ref") == po_issue["ref"]]
        self.assertEqual(po_created[0]["actor"], {"role": "po", "id": "po"})
        before = self.audit_events()
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="ummanu", issue_kind="bug", priority="P2", title="x", description="", actor="observer"
            )
        self.assertEqual(raised.exception.code, "role_masquerade")
        self.assertEqual(self.audit_events(), before)

    def test_product_and_issue_lists_use_complete_set_and_show_audit_history(self) -> None:
        product = self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="product-create",
        )
        self.assertEqual(product["id"], "ummanu")
        self.assertEqual([item["id"] for item in self.store.list_products()], ["ummanu"])

        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="feature",
            priority="P2",
            title="Foundation",
            description="",
            actor="po",
            request_id="issue-create",
        )
        self.store.update_priority(
            reference=issue["ref"],
            priority="P1",
            reason="urgent",
            actor="po",
            request_id="priority",
        )
        self.store.close_issue(
            reference=issue["ref"],
            reason="resolved",
            actor="po",
            request_id="close",
        )

        self.assertEqual(self.store.list_issues(), [])
        self.assertEqual(
            [item["ref"] for item in self.store.list_issues(include_closed=True)], [issue["ref"]]
        )
        shown = self.issue(issue["ref"])
        self.assertTrue(shown["closed"])
        self.assertEqual(shown["close_reason"], "resolved")
        self.assertIn(
            "[issue:priority]\nurgent\n[request-id:priority]",
            self.issue_comments(issue["ref"]),
        )
        self.assertEqual(
            [entry["kind"] for entry in self.issue_history(issue["ref"])["audit"]],
            ["entity.created", "entity.updated", "issue.closed"],
        )
        self.assertEqual(self.issue_product_binding(issue["ref"]), "ummanu")
        self.assertEqual(self.product_project_binding("ummanu"), ["ummanu"])

    def test_issue_needs_all_required_values_and_archive_cannot_bypass_close(self) -> None:
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="",
                issue_kind="feature",
                priority="P2",
                title="x",
                description="",
                actor="po",
            )
        self.assertEqual(raised.exception.code, "validation")

        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
        )
        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="bug",
            priority="P0",
            title="Crash",
            description="",
            actor="po",
        )
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        with self.assertRaises(TaskError) as raised:
            writer.archive(role="po", actor="po", reference=issue["ref"], reason="bypass")
        self.assertEqual(raised.exception.code, "transition_forbidden")
        shown = self.issue(issue["ref"])
        self.assertFalse(shown["closed"])
        self.assertIsNone(shown["close_reason"])

    def test_issue_close_has_one_terminal_reason_and_audit_event(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
        )
        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="bug",
            priority="P0",
            title="Crash",
            description="",
            actor="po",
        )
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")

        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="invalid", actor="po", request_id="retry")

        self.assertEqual(raised.exception.code, "closed")
        shown = self.store.show_issue(issue["ref"])
        self.assertEqual(shown["close_reason"], "resolved")
        self.assertEqual(
            [event["kind"] for event in shown["history"]["audit"]], ["entity.created", "issue.closed"]
        )

    def test_issue_and_task_column_guards_are_fail_closed(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
        )
        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="question",
            priority="P3",
            title="Question",
            description="",
            actor="po",
        )
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        # The observer is a bound head here: what is being tested is the column guard, and an
        # observer nobody bound never reaches it.
        with as_observer("sprint:issues"):
            for role in ("po", "dispatcher", "worker", "reviewer", "steward", "retro", "observer"):
                for target in ("ready", "in_progress", "validate", "blocked", "done"):
                    with self.subTest(role=role, target=target), self.assertRaises(TaskError) as raised:
                        writer.move(role=role, actor=role, reference=issue["ref"], target=target, reason="")
                    # The observer files issues and moves none (secretary-1765): its refusal is
                    # about the role, before the column guard every other role meets.
                    expected = "role_forbidden" if role == "observer" else "transition_forbidden"
                    self.assertEqual(raised.exception.code, expected)
        # A role outside the proposal roles cannot create in Issues. The steward was that role
        # here until secretary-1709 made it a proposal role; the PO takes its place.
        with self.assertRaises(TaskError) as raised:
            writer.create(
                role="po",
                actor="po",
                project="ummanu",
                task_type="research",
                title="Wrong column",
                target="issues",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")

    def test_missing_issue_arguments_are_structured(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["issue", "create", "--role", "po"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "usage")

    def test_product_ids_outside_the_machine_id_grammar_are_refused_before_any_write(self) -> None:
        before = (self.audit_events(), self.request_state())
        for field, product_id, projects in (
            ("product id", "продукт", ["ummanu"]),
            ("product id", "café", ["ummanu"]),
            ("product id", "two words", ["ummanu"]),
            ("product id", "under_score", ["ummanu"]),
            ("project id", "ummanu", ["codegen_orchestrator"]),
        ):
            with self.subTest(product_id=product_id, projects=projects):
                with self.assertRaisesRegex(TaskError, rf"{field} .* must match \[a-z0-9\]\[a-z0-9-\]\*") as raised:
                    self.store.create_product(
                        product_id=product_id,
                        projects=projects,
                        title="Продукт",
                        description="",
                        actor="po",
                    )
                self.assertEqual(raised.exception.code, "validation")
        self.assertEqual((self.audit_events(), self.request_state()), before)
        self.assert_product_absent("ummanu")

        product = self.store.create_product(
            product_id="ummanu", projects=["ummanu"], title="Продукт", description="", actor="po"
        )
        self.assertEqual((product["id"], product["title"]), ("ummanu", "Продукт"))

    def test_request_id_conflicts_are_rejected_before_a_second_write(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="product",
        )
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="ummanu",
                projects=["ummanu"],
                title="Changed",
                description="",
                actor="po",
                request_id="product",
            )
        self.assertEqual(raised.exception.code, "validation")
        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="bug",
            priority="P2",
            title="Crash",
            description="",
            actor="po",
            request_id="issue",
        )
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="ummanu",
                issue_kind="bug",
                priority="P2",
                title="Changed",
                description="",
                actor="po",
                request_id="issue",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority"
        )
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(
                reference=issue["ref"], priority="P0", reason="changed", actor="po", request_id="priority"
            )
        self.assertEqual(raised.exception.code, "validation")
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="invalid", actor="po", request_id="close")
        self.assertEqual(raised.exception.code, "validation")

    def test_committed_priority_replay_survives_a_later_close(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
        )
        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="bug",
            priority="P2",
            title="Crash",
            description="",
            actor="po",
        )
        self.store.update_priority(
            reference=issue["ref"],
            priority="P0",
            reason="urgent",
            actor="po",
            request_id="priority",
        )
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")

        replayed = self.store.update_priority(
            reference=issue["ref"],
            priority="P0",
            reason="urgent",
            actor="po",
            request_id="priority",
        )

        self.assertTrue(replayed["closed"])
        self.assertEqual(replayed["priority"], "P0")

    def test_priority_update_on_a_closed_issue_preserves_closed_refusal(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
        )
        issue = self.store.create_issue(
            product="ummanu",
            issue_kind="bug",
            priority="P2",
            title="Crash",
            description="",
            actor="po",
        )
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po")

        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po")

        self.assertEqual(raised.exception.code, "closed")
        self.assertEqual(raised.exception.exit_code, 3)

    def test_committed_request_replay_does_not_repeat_the_completed_operation(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="cleanup",
        )
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="cleanup",
        )
        self.assertEqual(self.record_count("product:ummanu"), 1)
        self.assertEqual([product["id"] for product in self.store.list_products()], ["ummanu"])
        self.assertEqual([event["kind"] for event in self.store.audit.events()], ["entity.created"])

    def test_request_id_never_becomes_a_pending_filename(self) -> None:
        self.store.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="../../outside",
        )
        names = [path.name for path in (self.root / "data" / "board").rglob("*.json")]
        self.assertTrue(all("outside" not in name for name in names))

    def test_generic_pending_request_id_blocks_product_before_backend_write(self) -> None:
        generic = {
            "event_id": "generic-shared",
            "request_id": "shared",
            "kind": "commented",
            "payload": {"body_sha256": "x"},
            "ref": "ummanu-468",
        }
        self.store.audit.stage("shared", generic)
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="ummanu",
                projects=["ummanu"],
                title="Ummanu",
                description="",
                actor="po",
                request_id="shared",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.store.audit.pending_event("shared"), generic)
        self.assertEqual(self.record_count("product:ummanu"), 0)
        self.assert_product_absent("ummanu")

    def test_pending_product_identity_blocks_a_second_request_before_create(self) -> None:
        request_id = "first-product"
        intent = {
            "record_type": "product",
            "product_id": "ummanu",
            "product_projects": '["ummanu"]',
            "title": "Ummanu",
            "description": "",
            "actor": "po",
        }
        event = self.store._transaction_event(
            kind="product_created",
            actor="po",
            reference="product:ummanu",
            request_id=request_id,
            intent=intent,
        )
        self.store.transactions.begin(request_id, kind="product_created", intent=intent, event=event)
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="ummanu",
                projects=["ummanu"],
                title="Other",
                description="",
                actor="po",
                request_id="second-product",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.record_count("product:ummanu"), 0)
        self.assert_product_absent("ummanu")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProductIssueDescriptionAppendTests(ProductIssueFixture, unittest.TestCase):
    """`issue append`: the only description change an Issue takes after create."""

    ORIGINAL = "Первый абзац.\n\n  indented line  \nno trailing newline"

    def _open_issue(self, description: str = ORIGINAL) -> dict:
        self.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="",
            actor="po",
            request_id="append-product",
        )
        return self.create_issue(
            product="ummanu",
            issue_kind="feature",
            priority="P2",
            title="Need",
            description=description,
            actor="po",
            request_id="append-issue",
        )

    def _second_issue(self, request_id: str, description: str = "") -> dict:
        return self.create_issue(
            product="ummanu",
            issue_kind="bug",
            priority="P3",
            title=f"Second {request_id}",
            description=description,
            actor="po",
            request_id=request_id,
        )

    def _append(self, reference: str, body: str, **values: object) -> dict:
        values.setdefault("reason", "new evidence")
        values.setdefault("actor", "po")
        return self.store.append_description(reference=reference, body=body, **values)

    def _updates(self, reference: str) -> list[dict]:
        return [
            event
            for event in self.audit_events()
            if event.get("ref") == reference and event.get("kind") == "entity.updated"
        ]

    def test_append_keeps_the_old_text_and_adds_one_dated_block_and_one_event(self) -> None:
        issue = self._open_issue()
        body = "Found later:\n- detail\n"

        shown = self._append(issue["ref"], body, request_id="append")

        description = shown["description"]
        self.assertTrue(description.startswith(self.ORIGINAL))
        self.assertRegex(
            description[len(self.ORIGINAL) :],
            r"\A\n\n---\n\n\[issue:appended \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z by po\]\n\n"
            r"Found later:\n- detail\n\Z",
        )
        self.assertEqual(self.issue(issue["ref"])["description"], description)
        self.assertEqual(shown["priority"], "P2")
        updates = self._updates(issue["ref"])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["request_id"], "append")
        self.assertEqual(updates[0]["actor"], {"role": "po", "id": "po"})
        self.assertEqual(updates[0]["reason"], "new evidence")
        self.assertEqual(
            updates[0]["data"]["append"],
            {
                "body_sha256": _sha256(body),
                "description_sha256_was": _sha256(self.ORIGINAL),
                "description_sha256": _sha256(description),
            },
        )
        self.assertEqual(
            [entry["kind"] for entry in self.issue_history(issue["ref"])["audit"]],
            ["entity.created", "entity.updated"],
        )
        self.assertEqual(self.issue_comments(issue["ref"]), [])

    def test_every_append_goes_after_the_whole_current_text(self) -> None:
        issue = self._open_issue("ends with a newline\n")

        first = self._append(issue["ref"], "one", request_id="first")["description"]
        second = self._append(issue["ref"], "\n\ntwo\n\n", request_id="second")["description"]

        self.assertTrue(first.startswith("ends with a newline\n\n---\n\n[issue:appended "))
        self.assertTrue(first.endswith(" by po]\n\none\n"))
        self.assertTrue(second.startswith(first + "\n---\n\n[issue:appended "))
        self.assertTrue(second.endswith(" by po]\n\ntwo\n"))
        self.assertEqual([event["request_id"] for event in self._updates(issue["ref"])], ["first", "second"])
        empty = self._second_issue("empty-issue")
        self.assertRegex(
            self._append(empty["ref"], "only", request_id="empty")["description"],
            r"\A---\n\n\[issue:appended [^\]]+ by po\]\n\nonly\n\Z",
        )

    def test_same_request_replays_once_and_a_changed_payload_is_refused(self) -> None:
        issue = self._open_issue()
        other = self._second_issue("other-issue")
        appended = self._append(issue["ref"], "block", request_id="append")["description"]

        self.assertEqual(self._append(issue["ref"], "block", request_id="append")["description"], appended)
        for changed in (
            {"reference": issue["ref"], "body": "another block", "reason": "new evidence"},
            {"reference": issue["ref"], "body": "block", "reason": "another reason"},
            {"reference": other["ref"], "body": "block", "reason": "new evidence"},
        ):
            with self.subTest(changed=changed), self.assertRaises(TaskError) as raised:
                self.store.append_description(actor="po", request_id="append", **changed)
            self.assertEqual(raised.exception.code, "validation")
        # The priority writer does not mistake an append of the same reason for its own replay.
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(
                reference=issue["ref"], priority="P2", reason="new evidence", actor="po", request_id="append"
            )
        self.assertEqual(raised.exception.code, "validation")

        self.assertEqual(self.issue(issue["ref"])["description"], appended)
        self.assertEqual(self.issue(other["ref"])["description"], "")
        self.assertEqual(len(self._updates(issue["ref"])), 1)
        self.assertEqual(self._updates(other["ref"]), [])
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        replayed = self._append(issue["ref"], "block", request_id="append")
        self.assertTrue(replayed["closed"])
        self.assertEqual(replayed["description"], appended)

    def test_refusals_write_no_block_and_no_event(self) -> None:
        issue = self._open_issue()
        closed = self._second_issue("closed-issue", description="closed text")
        self.store.close_issue(reference=closed["ref"], reason="wont_do", actor="po", request_id="close")
        before = self.audit_events()

        for index, (code, values) in enumerate(
            (
                ("validation", {"reference": issue["ref"], "body": " \n\t\n", "reason": "new evidence"}),
                ("validation", {"reference": issue["ref"], "body": "block", "reason": "  "}),
                ("not_found", {"reference": "issue:0000", "body": "block", "reason": "new evidence"}),
                ("validation", {"reference": "product:ummanu", "body": "block", "reason": "new evidence"}),
                ("closed", {"reference": closed["ref"], "body": "block", "reason": "new evidence"}),
            )
        ):
            with self.subTest(code=code, values=values), self.assertRaises(TaskError) as raised:
                self.store.append_description(actor="po", request_id=f"refused-{index}", **values)
            self.assertEqual(raised.exception.code, code)
            if code == "closed":
                self.assertEqual(raised.exception.exit_code, 3)

        self.assertEqual(self.audit_events(), before)
        self.assertEqual(self.issue(issue["ref"])["description"], self.ORIGINAL)
        self.assertEqual(self.issue(closed["ref"])["description"], "closed text")
        # A refusal claimed nothing: its request id is still free for a valid append.
        self._append(issue["ref"], "block", request_id="refused-0")
        self.assertEqual([event["request_id"] for event in self._updates(issue["ref"])], ["refused-0"])

    def test_cli_appends_a_body_file_as_po_and_refuses_another_role(self) -> None:
        issue = self._open_issue()
        block = self.root / "block.md"
        block.write_text("From the CLI\n", encoding="utf-8")
        arguments = [
            "--ref", issue["ref"], "--reason", "new evidence", "--body-file", str(block),
            "--actor", "po", "--request-id", "cli",
            "--instance", str(self.root), "--data-dir", str(self.root / "data"),
        ]  # fmt: skip
        refused_output, refused_errors, output = io.StringIO(), io.StringIO(), io.StringIO()
        with mock.patch(
            "ummanu.product_issue_commands.board_client", return_value=self._client_for(self.store)
        ):
            with contextlib.redirect_stdout(refused_output), contextlib.redirect_stderr(refused_errors):
                refused = main(["issue", "append", "--role", "worker", *arguments])
            self.assertEqual(self.issue(issue["ref"])["description"], self.ORIGINAL)
            with contextlib.redirect_stdout(output):
                code = main(["issue", "append", "--role", "po", *arguments])

        self.assertEqual(refused, 2)
        self.assertEqual(refused_output.getvalue(), "")
        self.assertEqual(json.loads(refused_errors.getvalue())["error"]["code"], "usage")
        self.assertEqual(code, 0)
        shown = json.loads(output.getvalue())
        self.assertTrue(shown["description"].startswith(self.ORIGINAL + "\n\n---\n\n[issue:appended "))
        self.assertTrue(shown["description"].endswith(" by po]\n\nFrom the CLI\n"))
        self.assertEqual(self.issue(issue["ref"])["description"], shown["description"])
        self.assertEqual([event["request_id"] for event in self._updates(issue["ref"])], ["cli"])


if __name__ == "__main__":
    unittest.main()
