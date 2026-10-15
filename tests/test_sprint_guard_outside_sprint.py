"""The sprint write guard for PO cards linked to no sprint (secretary-1641).

A PO create, move or edit of a card outside every sprint is not refused because an open sprint reserves
its project, for every kind and with no override; running it is the dispatcher's admission
(`tests.test_dispatcher_sprint_admission`). A PO write to a card of the holding sprint still needs the
override, and an index that cannot be verified still fails closed.

Kept out of `tests.test_sprints`, whose method inventory `tests.test_sprint_fixture_guards` freezes.
The cases write cards, and cards have one implementation, so the board is a real store seeded with the
Sprint fixture's rows (`tests/sql_backend_fixtures.py`).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes.sprints import ProductSprintSeed, SprintBackendFixture
from tests.observer_identity import bind_observer
from tests.sql_backend_fixtures import card_store
from ummanu.sprints import SprintReader, SprintWriter, refresh_active_sprint_projects
from ummanu.tasks import TaskError, TaskWriter, task_audit_for


class OutOfSprintWriteGuardTests(SprintBackendFixture, unittest.TestCase):
    BACKEND = "postgres"

    def make_sprint_client(self):
        return card_store(self, ProductSprintSeed(), instance_dir=self.tmp.name)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = self.make_sprint_client()
        self.sprints = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        # The same open sprint `SprintSingleWriterGuardTests` seeds: it reserves both projects.
        self.ref = self.sprints.restore_create(
            reference="sprint:guard",
            goal="single writer",
            repositories=["ummanu", "other"],
            request_id="seed-guard-sprint",
        )["sprint"]["ref"]
        self.sprints.restore(
            reference=self.ref,
            values={"sprint_reservations": json.dumps(["ummanu", "other"])},
            request_id="seed-guard-reservations",
        )
        refresh_active_sprint_projects(self.tmp.name, SprintReader(self.client))  # type: ignore[arg-type]
        bind_observer(self, self.ref)

    def test_po_writes_a_card_outside_the_sprint_of_every_kind_without_an_override(self) -> None:
        """secretary-1641: a card linked to no sprint is not the holding sprint's work.

        Create, edit and move all reach the narrowed rule through the one guard, for every kind, and
        none of them needs or records an override; whether the card runs is the dispatcher's
        admission. The observer and the other roles are unchanged (see the tests around this one).
        """
        for kind in ("code", "research", "infra"):
            with self.subTest(kind=kind):
                card = self.tasks.create(
                    role="po",
                    actor="operator",
                    project="ummanu",
                    task_type=kind,
                    title=f"outside {kind}",
                    request_id=f"outside-create-{kind}",
                )["task"]
                self.assertEqual((card["state"], card["type"], card.get("sprint") or ""), ("ready", kind, ""))
                edited = self.tasks.edit(
                    role="po",
                    actor="operator",
                    reference=card["ref"],
                    description="revised",
                    request_id=f"outside-edit-{kind}",
                )
                self.assertEqual(edited["task"]["description"], "revised")
                for target in ("blocked", "ready"):
                    moved = self.tasks.move(
                        role="po",
                        actor="operator",
                        reference=card["ref"],
                        target=target,
                        reason="",
                        request_id=f"outside-move-{kind}-{target}",
                    )
                    self.assertEqual(moved["task"]["state"], target)
        guard_events = [
            event
            for event in task_audit_for(self.client).events()
            if event["kind"] in {"sprint_guard_denied", "sprint_guard_override"}
        ]
        self.assertEqual(guard_events, [])

    def test_po_writes_to_a_card_of_the_holding_sprint_still_need_the_override(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="research",
            title="owned",
            sprint=self.ref,
        )["task"]
        writes = {
            "edit": lambda: self.tasks.edit(
                role="po", actor="operator", reference=card["ref"], description="outside edit"
            ),
            "move": lambda: self.tasks.move(
                role="po", actor="operator", reference=card["ref"], target="blocked", reason=""
            ),
            "link": lambda: self.tasks.create(
                role="po",
                actor="operator",
                project="ummanu",
                task_type="research",
                title="linked",
                sprint=self.ref,
            ),
        }
        for name, write in writes.items():
            with self.subTest(write=name):
                with self.assertRaisesRegex(TaskError, self.ref) as denied:
                    write()
                self.assertEqual(denied.exception.code, "sprint_write_forbidden")

    def test_an_unverifiable_index_still_fails_closed_for_a_card_outside_the_sprint(self) -> None:
        card = self.tasks.create(
            role="po", actor="operator", project="ummanu", task_type="code", title="outside"
        )["task"]
        (Path(self.tmp.name) / "sprints" / "active-repositories.json").unlink()
        original = self.client.call

        def unavailable(method: str, **params: object) -> object:
            if method in {"getAllTasks", "searchTasks"} or (
                method == "getTaskByReference" and params.get("reference") == self.ref
            ):
                raise TaskError("backend_unavailable", "board store is unavailable", 1)
            return original(method, **params)

        writes = {
            "create": lambda: self.tasks.create(
                role="po", actor="operator", project="ummanu", task_type="infra", title="blocked"
            ),
            "edit": lambda: self.tasks.edit(
                role="po", actor="operator", reference=card["ref"], description="blocked"
            ),
            "move": lambda: self.tasks.move(
                role="po", actor="operator", reference=card["ref"], target="blocked", reason=""
            ),
        }
        for name, write in writes.items():
            with self.subTest(write=name):
                with (
                    mock.patch.object(self.client, "call", side_effect=unavailable),
                    self.assertRaisesRegex(TaskError, "cannot verify") as raised,
                ):
                    write()
                self.assertEqual(raised.exception.code, "sprint_guard_unavailable")

    def test_comments_on_a_reserved_project_come_from_the_sprint_roles_and_the_cards_own_executor(
        self,
    ) -> None:
        sprint_card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="research",
            title="owned",
            sprint=self.ref,
        )["task"]
        outside = self.tasks.create(
            role="po", actor="operator", project="ummanu", task_type="research", title="outside"
        )["task"]
        allowed = [
            ("po", sprint_card),
            ("observer", sprint_card),
            ("dispatcher", sprint_card),
            ("owner", sprint_card),
            ("worker", sprint_card),
            ("reviewer", sprint_card),
            ("po", outside),
        ]
        for role, card in allowed:
            with self.subTest(role=role, card=card["ref"]):
                self.tasks.comment(role=role, actor=role, reference=card["ref"], body=f"{role} note")
        refused = [("retro", sprint_card), ("steward", sprint_card), ("worker", outside)]
        for role, card in refused:
            with self.subTest(role=role, card=card["ref"]):
                with self.assertRaisesRegex(TaskError, self.ref) as denied:
                    self.tasks.comment(role=role, actor=role, reference=card["ref"], body=f"{role} note")
                self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        markers = {
            comment.get("marker") for comment in self.tasks.reader.show(sprint_card["ref"])["comments"]
        }
        self.assertEqual(markers, {"po", "observer", "dispatcher", "owner", "worker", "reviewer"})


if __name__ == "__main__":
    unittest.main()
