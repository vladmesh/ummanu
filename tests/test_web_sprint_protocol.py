"""The sprint half of the transport-independent layer: one create, two reads, and no second rule.

Hermetic in the same strong sense the other two suites are: no live board, no network, no
dispatcher tick and no real head. The board is the Product/Issue fake the sprint tests already use,
the head registry is an installed pair, and the dispatcher's production state is a file. Everything
the layer concludes it concludes from the evidence a real installation leaves. The installation
itself is built by `tests/webproto_sprint_fixtures.py`, which the transport suite drives the same
layer through, so the two cannot end up testing two different installations.

Two properties are the ones that must fail rather than be believed, and they have their own cases:
a repeat of a request creates nothing (`IdempotencyTests`), and a role nobody pinned reaches the
entity as a field that was never written (`ExecutorPinTests`).
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import io
import json
import re
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from tests.observer_identity import bind_observer
from tests.sprint_close_fixtures import CLOSEOUT_BODY, init_state_repo
from tests.webproto_sprint_fixtures import (
    OBSERVER_PROFILE,
    REVIEWER_PROFILE,
    WORKER_PROFILE,
    SprintProtocolFixture,
)
from ummanu import sprints as sprints_module
from ummanu._fsutil import content_revision
from ummanu.board.audit_contract import PROTOCOL_EVENT_RECORD_TYPE
from ummanu.cli import main
from ummanu.config import validate
from ummanu.head_registry import generated_pair
from ummanu.knowledge_write import list_knowledge_documents
from ummanu.runtime.head.identity import publish_heartbeat
from ummanu.sprint_close import CLOSE_NOT_DONE
from ummanu.sprint_observer import EXECUTOR_PINNED, EXECUTOR_UNSET, REVIEWER_FIELD, WORKER_FIELD
from ummanu.sprints import SPRINT_BOARD_NAME, SPRINT_CLOSEOUT, _close_step_request_id
from ummanu.tasks import _STATE_BY_COLUMN, TaskError, TaskWriter, task_audit_for
from ummanu.webproto import section as section_module
from ummanu.webproto import sources, sprint_requests, store_io
from ummanu.webproto import sprint_reads as sprint_reads_module
from ummanu.webproto.boundary import GUARDED, operations
from ummanu.webproto.commands import _EXIT_BY_CODE, EXIT_CONFLICT, EXIT_PENDING
from ummanu.webproto.errors import (
    OperationPending,
    OwnerConflict,
    ReadError,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from ummanu.webproto.runs import RunStoreError
from ummanu.webproto.sprint_ops import (
    COMMENT_PENDING_REASON,
    PENDING_REASON,
    SPRINT_CLOSE_OPERATION,
    SPRINT_COMMENT_OPERATION,
    SprintOperationLayer,
)
from ummanu.webproto.sprint_reads import (
    ACCEPTANCE_ISSUE,
    COMMENT_ABSENT,
    COMMENT_SAVED,
    COMMENT_UNKNOWN,
    DELIVERY_ERROR,
    DELIVERY_HANDED_OVER,
    DELIVERY_NOT_DELIVERABLE,
    DELIVERY_SAVED,
    DELIVERY_STATES,
    DELIVERY_UNKNOWN,
    DELIVERY_WAITING,
    OBSERVER_NOT_STARTED,
    OBSERVER_RUNNING,
    OBSERVER_UNAVAILABLE,
    TRANSITION_ABSENT,
    TRANSITION_NOT_APPLICABLE,
    TRANSITION_RECORDED,
    TRANSITION_UNKNOWN,
    SprintReadLayer,
)
from ummanu.webproto.sprint_requests import SprintRequestStore

#: What a call to the board writes with. Reads of this layer make none of these, and the sprint
#: board a fresh installation does not have is one of the things they do not create.
_WRITE_METHODS = {
    "createTask",
    "updateTask",
    "saveTaskMetadata",
    "createProject",
    "createComment",
    "removeTask",
}


def _write_kinds() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The kinds `SprintWriter` gives `_write`, read from the writer instead of restated here.

    `docs/PROTOCOLS.md` publishes what a terminal sprint answers for each of them, and a list kept
    by hand beside that table is what let the table claim to be complete while it was not. The
    writer's own calls are the only enumeration that cannot fall behind the writer.
    """
    module = ast.parse(inspect.getsource(sprints_module))
    writer = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "SprintWriter"
    )
    kinds: set[str] = set()
    underivable: set[str] = set()
    for node in ast.walk(writer):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr != "_write":
            continue
        if not isinstance(function.value, ast.Name) or function.value.id != "self":
            continue
        kind = node.args[0] if node.args else None
        if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
            kinds.add(kind.value)
        else:
            underivable.add(ast.unparse(kind) if kind is not None else "<no positional kind>")
    return tuple(sorted(kinds)), tuple(sorted(underivable))


def _board_id(board: Any, name: str) -> int:
    """Which board of this client carries `name`, as the client itself answers."""
    return int(board.call("getProjectByName", name=name)["id"])


@contextlib.contextmanager
def _board_missing(board: Any, name: str) -> Iterator[None]:
    """The client answers that it has no board by this name, as a board that is gone does."""
    original = board.call

    def call(method: str, **params: Any) -> Any:
        if method == "getProjectByName" and params.get("name") == name:
            return None
        return original(method, **params)

    board.call = call  # type: ignore[method-assign]
    try:
        yield
    finally:
        board.call = original  # type: ignore[method-assign]


class CreateTests(SprintProtocolFixture):
    def test_a_sprint_opens_with_what_it_was_opened_with(self) -> None:
        document = self.create()
        self.assertEqual(document["kind"], "sprint_created")
        self.assertTrue(document["created"])
        self.assertEqual(document["request_id"], "req-1")
        value = document["sprint"]["sprint"]["value"]
        self.assertEqual(value["goal"], "Give webproto a sprint create")
        self.assertEqual(value["definition_of_done"], "the operation exists and is tested")
        self.assertEqual(value["product"], "ummanu")
        self.assertEqual(value["issues"], ["issue:open"])
        self.assertEqual(value["reservations"], ["ummanu"])
        self.assertEqual(value["status"], "open")
        self.assertEqual(
            document["sprint"]["observer"]["declared"]["profile"], OBSERVER_PROFILE
        )

    def test_the_create_leaves_the_audit_and_the_reservation_index_the_writer_leaves(self) -> None:
        """The existing audit and reservations are kept because the existing writer keeps them."""
        from ummanu.sprints import SPRINT_CREATED, active_sprint_projects

        reference = self.reference_of(self.create())
        kinds = [str(event.get("kind") or "") for event in task_audit_for(self.board).events()]
        self.assertIn(SPRINT_CREATED, kinds)
        self.assertEqual(active_sprint_projects(self.data_dir), {"ummanu": [reference]})

    def test_a_sprint_on_a_project_another_open_sprint_holds_is_an_owner_conflict(self) -> None:
        self.create()
        with self.assertRaises(OwnerConflict) as refused:
            self.create(request_id="req-2", projects=["ummanu"], issues=["issue:open"])
        self.assertIn("already reserved by an open sprint", str(refused.exception))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_an_unregistered_project_is_a_typed_refusal_that_writes_nothing(self) -> None:
        with self.assertRaises(ValidationRefused) as refused:
            self.create(projects=["not-a-project"])
        self.assertIn("unknown registered project", str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_a_closed_issue_and_a_foreign_issue_are_both_refused(self) -> None:
        for issues, expected in (
            (["issue:done"], "closed"),
            (["issue:foreign"], "belongs to product"),
        ):
            with self.subTest(issues=issues):
                with self.assertRaises(ValidationRefused) as refused:
                    self.create(request_id=f"req-{issues[0]}", issues=issues)
                self.assertIn(expected, str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_an_issue_the_board_does_not_hold_is_a_typed_not_found(self) -> None:
        with self.assertRaises(TaskNotFound):
            self.create(issues=["issue:missing"])
        self.assertEqual(self.sprint_rows(), [])

    def test_an_observer_profile_the_registry_does_not_have_is_refused(self) -> None:
        with self.assertRaises(ValidationRefused) as refused:
            self.create(observer="retired-observer")
        self.assertIn("head registry", str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_an_executor_profile_the_registry_does_not_have_is_refused_for_either_role(self) -> None:
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                with self.assertRaises(ValidationRefused) as refused:
                    self.create(request_id=f"req-{role}", **{role: "retired-observer"})
                self.assertIn(f"sprint {role} names head profile", str(refused.exception))
        self.assertEqual(self.sprint_rows(), [])

    def test_a_request_without_an_id_is_refused_before_anything_is_read(self) -> None:
        with self.assertRaises(ValidationRefused):
            self.create(request_id="")
        self.assertEqual(self.sprint_rows(), [])


class ExecutorPinTests(SprintProtocolFixture):
    """Criterion 2: the difference between "not pinned" and "pinned" survives the whole operation."""

    def test_a_sprint_opens_with_the_pins_it_was_given(self) -> None:
        document = self.create(worker=WORKER_PROFILE, reviewer=REVIEWER_PROFILE)
        executors = document["sprint"]["sprint"]["value"]["executors"]
        self.assertEqual(executors["worker"], {"state": EXECUTOR_PINNED, "profile": WORKER_PROFILE})
        self.assertEqual(
            executors["reviewer"], {"state": EXECUTOR_PINNED, "profile": REVIEWER_PROFILE}
        )
        stored = self.metadata_of(self.reference_of(document))
        self.assertEqual(stored[WORKER_FIELD], WORKER_PROFILE)
        self.assertEqual(stored[REVIEWER_FIELD], REVIEWER_PROFILE)

    def test_a_role_nobody_pinned_reaches_the_entity_as_no_field_at_all(self) -> None:
        """Not an empty string and not a `role_defaults` value: a field that was never written."""
        document = self.create(worker=WORKER_PROFILE)
        stored = self.metadata_of(self.reference_of(document))
        self.assertEqual(stored[WORKER_FIELD], WORKER_PROFILE)
        self.assertNotIn(REVIEWER_FIELD, stored)
        executors = document["sprint"]["sprint"]["value"]["executors"]
        self.assertEqual(executors["reviewer"], {"state": EXECUTOR_UNSET})

    def test_neither_the_empty_string_nor_none_is_a_way_to_pin_nothing(self) -> None:
        for spelling in ("", "none", " "):
            with self.subTest(spelling=spelling), self.assertRaises(ValidationRefused):
                self.create(request_id=f"req-{spelling!r}", worker=spelling)
        self.assertEqual(self.sprint_rows(), [])

    def test_pinning_a_role_is_a_different_request_from_not_pinning_it(self) -> None:
        """The two must not collide in the request fingerprint, or a retry would answer the other."""
        self.create(worker=WORKER_PROFILE)
        with self.assertRaises(ValidationRefused) as refused:
            self.create()
        self.assertIn("different inputs", str(refused.exception))


class IdempotencyTests(SprintProtocolFixture):
    """Criteria 3 and 4: a request id owns the sprint, and a half-done create is resumed."""

    def test_local_run_exceptions_reach_entity_reads_and_request_fingerprint(self) -> None:
        entries = [{"project": "ummanu", "argv": ["docker", "run", "two words"], "rationale": "owner's exact probe"}]
        created = self.create(local_run_exceptions=entries)
        self.assertEqual(validate(created, "web-sprint", created["kind"]), [])
        self.assertEqual(created["sprint"]["sprint"]["value"]["local_run_exceptions"], entries)
        repeated = self.create(local_run_exceptions=entries)
        self.assertFalse(repeated["created"])
        self.assertEqual(self.reference_of(repeated), self.reference_of(created))
        for changed in ([], [{**entries[0], "argv": ["docker", "run", "changed"]}]):
            with self.subTest(changed=changed), self.assertRaises(ValidationRefused):
                self.create(local_run_exceptions=changed)

    def test_old_web_create_fingerprint_keeps_the_empty_default(self) -> None:
        created = self.create()
        with mock.patch("ummanu.sprints.SprintWriter.create") as never:
            repeated = self.create(local_run_exceptions=[])
        self.assertEqual(self.reference_of(repeated), self.reference_of(created))
        never.assert_not_called()

    def test_a_repeat_returns_the_same_sprint_and_creates_no_second_one(self) -> None:
        first = self.create()
        second = self.create()
        self.assertEqual(self.reference_of(second), self.reference_of(first))
        self.assertFalse(second["created"])
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_a_repeat_calls_no_writer_at_all(self) -> None:
        """The shortcut is the point: a repeat cannot create a second observer if it never writes."""
        reference = self.reference_of(self.create())
        with mock.patch("ummanu.sprints.SprintWriter.create") as never:
            repeat = self.create()
        never.assert_not_called()
        self.assertEqual(self.reference_of(repeat), reference)

    def test_the_same_id_over_different_inputs_is_refused_rather_than_answered(self) -> None:
        self.create()
        with self.assertRaises(ValidationRefused) as refused:
            self.create(goal="a different sprint entirely")
        self.assertIn("different inputs", str(refused.exception))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_a_repeat_after_a_partial_failure_uses_the_sprint_that_exists(self) -> None:
        """The entity is created and the step after it fails: the repeat must not open a second.

        The failing step here is this layer's own -- recording which sprint the request produced --
        which is exactly the window criterion 4 names. The repeat therefore arrives with a claimed
        request that names no sprint, hands the same id down to the writer, and gets back the
        sprint the first attempt already created rather than a new one.

        The failure is injected at the *filesystem*, not at `record_reference`. An earlier version
        of this test patched that method to raise `RunStoreError`, which is the exception the
        operation already catches, so by construction it could never see what the real write does:
        `write_text_atomic` raises a bare `RuntimeError`, which the boundary deliberately does not
        translate, and a full disk here escaped as that raw exception instead of the typed answer
        below. The seam this now goes through
        (:func:`ummanu.webproto.store_io.write_document`) is what makes the two agree.
        """
        real = store_io.write_text_atomic

        def refuse_the_reference(path, payload):
            # The claim write carries no reference yet; the write that records which sprint this
            # request produced does. So this fails exactly the second one, with the message the
            # atomic writer really raises when the filesystem refuses it.
            if '"reference": "sprint:' in payload:
                raise RuntimeError(f"could not write export file {path}: [Errno 28] No space left on device")
            return real(path, payload)

        with (
            mock.patch.object(store_io, "write_text_atomic", refuse_the_reference),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        created = self.assert_pending_after_create(pending.exception, cause="No space left on device")

        repeat = self.create()
        self.assertEqual(self.reference_of(repeat), str(created["reference"]))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_a_lock_the_filesystem_refuses_after_the_row_exists_is_the_same_answer(self) -> None:
        """The second primitive of the same region, and the one that made this round happen.

        `_fsutil.file_lock` does `mkdir`, `open("a+")` and `flock`, and every one of them raises a
        bare `OSError`. The boundary turned that into `backend_unavailable` with no request id and
        no action, so a caller learned neither that a sprint already existed nor that repeating the
        same request was the safe move. It is not caught here by adding `OSError` to a list: the
        whole post-create region answers this way, which is why the third primitive to arrive in it
        cannot open a third hole.
        """
        real = sprint_requests.file_lock
        entries = []

        def refuse_the_second_lock(path):
            # Call one is `claim`, before the writer. Call two is `record_reference`, after the row
            # exists -- and it is the one a full disk would refuse while creating the lock file.
            entries.append(path)
            if len(entries) >= 2:
                raise OSError(28, "No space left on device")
            return real(path)

        with (
            mock.patch.object(sprint_requests, "file_lock", refuse_the_second_lock),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        self.assertEqual(len(entries), 2, "the failure must land on the post-create lock")
        created = self.assert_pending_after_create(pending.exception, cause="No space left on device")
        self.assertIsInstance(pending.exception.__cause__, OSError)

        repeat = self.create()
        self.assertEqual(self.reference_of(repeat), str(created["reference"]))
        self.assertEqual(len(self.sprint_rows()), 1)

    def test_any_failure_after_the_row_exists_is_that_answer_including_one_nobody_listed(self) -> None:
        """The region, not the vocabulary: an exception type no list names is answered the same.

        Deliberately a defect-shaped failure (`TypeError`) raised from the step *after* the request
        index -- building the document -- because that is the case a list of durable-source
        vocabularies would miss and the case that must not lose the durable fact. The cause is not
        swallowed: it is chained, so a traceback still names it.
        """
        with (
            mock.patch.object(
                SprintOperationLayer,
                "_document",
                side_effect=TypeError("a defect in the document builder"),
            ),
            self.assertRaises(OperationPending) as pending,
        ):
            self.create()
        self.assert_pending_after_create(pending.exception, cause="a defect in the document builder")
        self.assertIsInstance(pending.exception.__cause__, TypeError)

    def test_the_request_index_names_the_sprint_and_never_a_second_one(self) -> None:
        reference = self.reference_of(self.create())
        record = SprintRequestStore(self.data_dir).by_request("req-1")
        self.assertEqual(record.reference, reference)
        self.assertEqual(record.operation, "sprint_create")
        # A recorded reference is final: a second one could only come from a second sprint.
        SprintRequestStore(self.data_dir).record_reference("req-1", "sprint:999")
        self.assertEqual(SprintRequestStore(self.data_dir).by_request("req-1").reference, reference)


class OptionsTests(SprintProtocolFixture):
    """Criterion 5: what a sprint can be built from, read from the sources that own it."""

    def test_the_catalogue_offers_products_open_issues_projects_and_heads(self) -> None:
        options = self.reads().sprint_options()
        self.assertEqual(options["kind"], "sprint_options")
        self.assertEqual([item["id"] for item in options["products"]["items"]], ["other", "ummanu"])
        refs = [item["ref"] for item in options["issues"]["items"]]
        self.assertEqual(refs, ["issue:foreign", "issue:open"])
        self.assertNotIn("issue:done", refs, "a closed issue is refused, so it is never offered")
        self.assertEqual(
            {item["ref"]: item["product"] for item in options["issues"]["items"]},
            {"issue:open": "ummanu", "issue:foreign": "other"},
        )
        self.assertEqual(
            [item["id"] for item in options["projects"]["items"]],
            ["other", "secretary-instance", "ummanu"],
        )

    def test_a_project_an_open_sprint_holds_is_marked_as_held(self) -> None:
        reference = self.reference_of(self.create())
        held = {
            item["id"]: item["reserved_by"] for item in self.reads().sprint_options()["projects"]["items"]
        }
        self.assertEqual(held["ummanu"], [reference])
        self.assertEqual(held["other"], [])

    def test_the_profile_catalogue_is_the_installed_registry_and_not_a_constant(self) -> None:
        heads = self.reads().sprint_options()["heads"]
        by_id = {item["id"]: item for item in heads["items"]}
        self.assertEqual(sorted(by_id), sorted([WORKER_PROFILE, REVIEWER_PROFILE, OBSERVER_PROFILE]))
        self.assertEqual(by_id[OBSERVER_PROFILE]["model"], "gpt-5.6-terra")
        self.assertEqual(by_id[OBSERVER_PROFILE]["effort"], "high")
        self.assertEqual(by_id[WORKER_PROFILE]["model"], "opus")
        self.assertIsNone(by_id[WORKER_PROFILE]["effort"], "a profile that pins no effort says so")
        # Every entry can be chosen without knowing an identifier, and carries the identifier.
        self.assertEqual(by_id[REVIEWER_PROFILE]["label"], "codex · gpt-5.6-sol · medium effort")
        self.assertEqual(by_id[REVIEWER_PROFILE]["id"], REVIEWER_PROFILE)
        self.assertEqual(heads["observer"]["default"], OBSERVER_PROFILE)
        self.assertEqual(heads["role_defaults"]["reviewer"], REVIEWER_PROFILE)
        self.assertEqual(by_id[OBSERVER_PROFILE]["role_default_for"], ["observer"])

    def test_a_profile_the_registry_does_not_have_is_not_offered_and_is_not_creatable(self) -> None:
        """The catalogue and the refusal read the same file, so they cannot disagree."""
        offered = {item["id"] for item in self.reads().sprint_options()["heads"]["items"]}
        self.assertNotIn("retired-observer", offered)
        with self.assertRaises(ValidationRefused):
            self.create(observer="retired-observer")

    def test_every_offered_profile_is_marked_for_the_observer_role_it_may_take(self) -> None:
        items = self.reads().sprint_options()["heads"]["items"]
        self.assertTrue(all(item["observer"] for item in items))
        self.assertTrue(all(item["observer_reason"] is None for item in items))
        # And the mark is the create's own check, not a restatement: a profile the registry loses
        # is marked ineligible by the same call that refuses it.
        with mock.patch.object(
            sprint_reads_module, "installed_head_profiles", return_value={OBSERVER_PROFILE}
        ):
            marked = {
                item["id"]: item["observer"]
                for item in self.reads().sprint_options()["heads"]["items"]
            }
        self.assertEqual(marked, {OBSERVER_PROFILE: True, WORKER_PROFILE: False, REVIEWER_PROFILE: False})

    def test_an_unreadable_head_registry_blanks_its_own_section_only(self) -> None:
        generated_pair(self.instance).snapshot.write_text("{", encoding="utf-8")
        options = self.reads().sprint_options()
        self.assertEqual(options["heads"]["source"]["state"], "unavailable")
        # `null` and not `[]`: an empty catalogue is the claim that this installation runs off no
        # head at all, which is the opposite of a registry nobody could read.
        self.assertIsNone(options["heads"]["items"])
        self.assertEqual(options["products"]["source"]["state"], "available")
        self.assertTrue(options["products"]["items"])


class SprintStateTests(SprintProtocolFixture):
    """Criterion 6: enough of one sprint to watch it, and three launch states told apart."""

    def _observer_record(self, reference: str, *, alive: bool) -> None:
        run_id = "obs-run-1"
        pid_file = self.data_dir / "dispatcher" / "observer.pid"
        publish_heartbeat(
            str(pid_file),
            {"run_id": run_id, "role": "observer", "task": f"sprint:{reference}"},
        )
        if not alive:
            # A record whose heartbeat cannot be read at all, and which is long past the grace
            # window a just-launched head gets: a head that is not there.
            pid_file.write_text("{}", encoding="utf-8")
        self._production(
            {
                reference: {
                    "sprint": reference,
                    "head": OBSERVER_PROFILE,
                    "state": "working",
                    "pid_file": str(pid_file),
                    "head_run": {"run_id": run_id},
                    "launched_at": 1.0,
                    "launches": 1,
                    "bound": True,
                }
            }
        )

    def test_a_saved_sprint_whose_observer_is_not_up_yet_says_so(self) -> None:
        reference = self.reference_of(self.create())
        document = self.reads().sprint_state(reference)
        self.assertEqual(document["kind"], "sprint")
        self.assertEqual(document["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)
        self.assertIsNone(document["observer"]["launch"]["record"])
        self.assertEqual(document["observer"]["launch"]["source"]["state"], "available")
        self.assertEqual(document["observer"]["declared"]["profile"], OBSERVER_PROFILE)

    def test_an_observer_that_is_really_working_is_read_from_the_dispatcher_state(self) -> None:
        reference = self.reference_of(self.create())
        self._observer_record(reference, alive=True)
        launch = self.reads().sprint_state(reference)["observer"]["launch"]
        self.assertEqual(launch["state"], OBSERVER_RUNNING)
        self.assertEqual(launch["record"]["head"], OBSERVER_PROFILE)
        self.assertTrue(launch["record"]["alive"])

    def test_each_role_names_its_profile_with_the_model_and_effort_it_pins(self) -> None:
        """The page shows a role's model without joining the sprint against the registry itself."""
        reference = self.reference_of(self.create(worker=WORKER_PROFILE))
        layer = self.reads()

        watched = layer.sprint_state(reference)["work"]["head_profiles"]
        items = layer.sprint_list()["sprints"]["items"]
        listed = next(item for item in items if item["ref"] == reference)["head_profiles"]

        self.assertEqual(listed, watched)
        self.assertEqual(watched["source"]["name"], "heads")
        self.assertEqual(
            watched["observer"],
            {
                "profile": OBSERVER_PROFILE,
                "via": "declared",
                "registered": True,
                "label": "codex · gpt-5.6-terra · high effort",
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
        )
        self.assertEqual(
            (watched["worker"]["profile"], watched["worker"]["via"], watched["worker"]["model"]),
            (WORKER_PROFILE, "pinned", "opus"),
        )
        self.assertIsNone(watched["worker"]["effort"])
        # An unpinned reviewer is the dispatcher's choice per card, not a profile of this sprint.
        self.assertEqual(
            (watched["reviewer"]["profile"], watched["reviewer"]["via"], watched["reviewer"]["registered"]),
            (None, "unset", False),
        )

    def test_the_observer_profile_is_the_one_its_record_launched(self) -> None:
        reference = self.reference_of(self.create())
        self._observer_record(reference, alive=True)

        observer = self.reads().sprint_state(reference)["work"]["head_profiles"]["observer"]

        self.assertEqual((observer["profile"], observer["via"]), (OBSERVER_PROFILE, "launched"))

    def test_a_head_registry_nobody_can_read_blanks_only_the_profiles(self) -> None:
        reference = self.reference_of(self.create())
        generated_pair(self.instance).snapshot.write_text("{", encoding="utf-8")

        document = self.reads().sprint_state(reference)

        profiles = document["work"]["head_profiles"]
        self.assertEqual(profiles["source"]["state"], "unavailable")
        self.assertEqual((profiles["observer"], profiles["worker"], profiles["reviewer"]), (None, None, None))
        self.assertEqual(document["sprint"]["source"]["state"], "available")

    def test_a_dispatcher_state_nobody_can_read_is_unavailable_and_never_not_started(self) -> None:
        reference = self.reference_of(self.create())
        (self.data_dir / "dispatcher" / "production-state.json").write_text("{", encoding="utf-8")
        document = self.reads().sprint_state(reference)
        launch = document["observer"]["launch"]
        self.assertEqual(launch["state"], OBSERVER_UNAVAILABLE)
        self.assertEqual(launch["source"]["state"], "unavailable")
        # The sprint's own fields are a different source and survive.
        self.assertEqual(document["sprint"]["source"]["state"], "available")
        self.assertEqual(document["sprint"]["value"]["goal"], "Give webproto a sprint create")

    def test_a_head_the_dispatcher_holds_that_is_not_alive_is_neither_running_nor_missing(self) -> None:
        reference = self.reference_of(self.create())
        self._observer_record(reference, alive=False)
        launch = self.reads().sprint_state(reference)["observer"]["launch"]
        self.assertEqual(launch["state"], sprint_reads_module.OBSERVER_STOPPED)
        self.assertFalse(launch["record"]["alive"])

    def test_a_watched_sprint_carries_its_goal_dod_links_pins_status_card_and_resume(self) -> None:
        reference = self.reference_of(self.create(worker=WORKER_PROFILE))
        value = self.reads().sprint_state(reference)["sprint"]["value"]
        self.assertEqual(value["goal"], "Give webproto a sprint create")
        self.assertEqual(value["definition_of_done"], "the operation exists and is tested")
        self.assertEqual(value["reservations"], ["ummanu"])
        self.assertEqual(value["issues"], ["issue:open"])
        self.assertEqual(value["status"], "open")
        self.assertIsNone(value["current_task"])
        self.assertIsNone(value["resume"], "a sprint nobody has resumed carries no resume entry")
        self.assertEqual(value["executors"]["worker"]["profile"], WORKER_PROFILE)
        self.assertEqual(value["executors"]["reviewer"]["state"], EXECUTOR_UNSET)

    def test_the_last_resume_entry_of_a_sprint_is_on_the_page(self) -> None:
        from tests.observer_identity import bind_observer
        from ummanu.sprints import SprintWriter

        reference = self.reference_of(self.create())
        bind_observer(self, reference)
        SprintWriter(
            self.board, data_dir=self.data_dir, instance=self.instance
        ).resume(
            role="observer",
            actor="observer",
            reference=reference,
            entry={
                "recorded_at": "2026-09-06T10:00:00Z",
                "selected_step": "the create operation",
                "selected_why": "the layer owes the transport a contract",
                "rejected_alternatives": "a second scheduler",
                "current_task": "ummanu-1569",
                "dod_state": "DoD 4 in progress",
                "next_safe_step": "write the transport",
            },
        )
        resume = self.reads().sprint_state(reference)["sprint"]["value"]["resume"]
        self.assertEqual(resume["selected_step"], "the create operation")
        self.assertEqual(resume["next_safe_step"], "write the transport")

    def test_a_reference_no_sprint_holds_is_a_typed_not_found(self) -> None:
        self.create()
        with self.assertRaises(TaskNotFound):
            self.reads().sprint_state("sprint:404")

    def test_a_read_creates_no_sprint_board(self) -> None:
        """A read writes nothing, and the board this installation has never had is one of them."""
        with self.assertRaises(TaskNotFound):
            self.reads().sprint_state("sprint:1")
        self.assertEqual(self.sprint_rows(), [])
        self.assertFalse(any(method == "createProject" for method, _ in self.board.calls))


class LayerPropertyTests(SprintProtocolFixture):
    """Criterion 7: the properties of the layer, checked rather than described."""

    def test_every_document_validates_against_the_published_schema(self) -> None:
        created = self.create(worker=WORKER_PROFILE)
        reference = self.reference_of(created)
        commented = self.ops().sprint_comment(
            request_id="schema-comment", actor="operator", reference=reference, body="a PO note"
        )
        documents = (
            created,
            self.reads().sprint_state(reference),
            self.reads().sprint_list(),
            self.reads().sprint_options(),
            commented,
            self.reads().sprint_comment_delivery(reference, commented["comment_id"]),
        )
        for document in documents:
            with self.subTest(kind=document["kind"]):
                self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
                json.dumps(document)

    def test_the_post_create_region_is_one_place_with_one_exit(self) -> None:
        """The invariant, as structure rather than as a promise every future step must remember.

        Two rounds of this card fixed a post-create failure at the primitive that happened to raise
        it and watched the next primitive open the same hole. So what is checked here is the shape:
        `sprint_create` ends by handing the whole post-create region to `_after_create`, and that
        region is a single `try` with a single `except Exception` that raises the pending answer. A
        step added to it later is covered by being inside it, and there is no list to keep in step.
        """
        import ast

        source = (
            Path(__file__).resolve().parents[1] / "src" / "ummanu" / "webproto" / "sprint_ops.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        layer = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "SprintOperationLayer"
        )
        methods = {node.name: node for node in layer.body if isinstance(node, ast.FunctionDef)}

        # `sprint_create` hands over and does nothing after the hand-over.
        last = methods["sprint_create"].body[-1]
        self.assertIsInstance(last, ast.Return)
        self.assertIsInstance(last.value, ast.Call)
        self.assertEqual(getattr(last.value.func, "attr", ""), "_after_create")

        # And the region has exactly one exit for every failure in it.
        body = [
            node
            for node in methods["_after_create"].body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        guarded = [node for node in body if isinstance(node, ast.Try)]
        self.assertEqual(len(guarded), 1)
        self.assertIs(guarded[0], body[-1], "nothing runs after the region")
        handlers = guarded[0].handlers
        self.assertEqual(len(handlers), 1)
        self.assertEqual(getattr(handlers[0].type, "id", ""), "Exception")
        raised = [node for node in ast.walk(handlers[0]) if isinstance(node, ast.Raise)]
        self.assertEqual(len(raised), 1)
        self.assertEqual(getattr(raised[0].exc.func, "id", ""), "OperationPending")
        # Chained, so the primitive that failed is still named in a traceback.
        self.assertIsNotNone(raised[0].cause)

    def test_the_sprint_modules_import_no_transport(self) -> None:
        """The promise as it is meant: this layer speaks no transport to its caller.

        Direct imports, deliberately. A transitive scan would be a different and false claim: the
        layer's access to the board is a database client, and `ummanu.tasks` has imported a
        driver since long before this card -- as `reads.py`,
        `admission.py`, `ops.py` and `run_events.py` all show. What the promise means, and what is
        checked here and in the refusal tests above, is that nothing of the transport reaches the
        caller: no HTTP, socket, framework or rendering in this layer's own surface, and failures
        that leave it are typed `webproto.errors` codes rather than status numbers.
        """
        import ast

        forbidden = frozenset(
            """
            http httpx requests urllib urllib3 socket socketserver ssl asyncio aiohttp flask
            fastapi starlette uvicorn django jinja2 tornado werkzeug wsgiref html cgi bottle sanic
            quart argparse sys
            """.split()
        )
        root = Path(__file__).resolve().parents[1] / "src" / "ummanu" / "webproto"
        offenders: list[str] = []
        for name in ("sprint_ops.py", "sprint_reads.py", "sprint_requests.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"), filename=name)
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = [node.module]
                offenders.extend(
                    f"{name}: {value}" for value in imported if value.split(".")[0] in forbidden
                )
        self.assertEqual(offenders, [])

    def test_every_operation_of_both_layers_is_guarded_by_being_public(self) -> None:
        for layer in (SprintOperationLayer, SprintReadLayer):
            for name in operations(layer):
                with self.subTest(layer=layer.__name__, operation=name):
                    self.assertTrue(getattr(getattr(layer, name), GUARDED, False))

    def test_an_unreadable_source_refuses_a_write_but_only_its_read_sections(self) -> None:
        """Writes refuse the operation; reads keep the independent board answers."""
        reference = self.reference_of(self.create())
        with mock.patch.object(
            SprintRequestStore, "by_request", side_effect=RunStoreError("unreadable")
        ), self.assertRaises(ReadError) as refused:
            self.create(request_id="req-2")
        self.assertEqual(refused.exception.code, "backend_unavailable")
        with mock.patch(
            "ummanu.webproto.sprint_reads.observer_snapshot", side_effect=RunStoreError("x")
        ):
            document = self.reads().sprint_state(reference)
        self.assertEqual(document["liveness"]["source"]["state"], "unavailable")
        self.assertIn("x", document["liveness"]["source"]["reason"])
        self.assertEqual(document["observer"]["launch"]["state"], OBSERVER_UNAVAILABLE)
        self.assertEqual(document["sprint"]["source"]["state"], "available")
        self.assertEqual(document["sprint"]["value"]["goal"], "Give webproto a sprint create")
        self.assertEqual(document["cards"]["source"]["state"], "available")

    def test_a_refusal_with_nothing_to_add_carries_no_data(self) -> None:
        self.assertEqual(
            RuntimeUnavailable("plain").to_json(), {"code": "backend_unavailable", "message": "plain"}
        )
        self.assertEqual(
            OperationPending("held", data={"reason": PENDING_REASON}).to_json()["data"],
            {"reason": PENDING_REASON},
        )

    def test_the_reads_write_nothing_to_the_board(self) -> None:
        self.create()
        before = len(self.board.calls)
        self.reads().sprint_options()
        self.reads().sprint_state(self.reference_of(self.create()))
        self.reads().sprint_list()
        self.reads().sprint_comment_delivery(self.reference_of(self.create()), "evt_none")
        written = [
            method for method, _params in self.board.calls[before:] if method in _WRITE_METHODS
        ]
        self.assertEqual(written, [])


class SprintWorkFixture(SprintProtocolFixture):
    """The pieces both work-document suites drive: one sprint, one card, and where the card is.

    A base rather than an inheritance between the two suites, so that neither re-runs the other's
    cases to get at a helper. It writes cards, so its board is a real card store.
    """

    def _entry(self, document: dict, reference: str) -> dict:
        return next(item for item in document["sprints"]["items"] if item["ref"] == reference)

    def _card(self, sprint: str) -> str:
        """One Pipeline card of this sprint, created the way the observer creates one."""
        from tests.observer_identity import bind_observer
        from ummanu.tasks import TaskWriter

        bind_observer(self, sprint)
        return str(
            TaskWriter(self.board, data_dir=self.data_dir).create(
                role="observer",
                actor="observer",
                project="ummanu",
                task_type="code",
                title="the current card",
                sprint=sprint,
            )["task"]["ref"]
        )

    def _move(self, card: str, column: str) -> None:
        """Put one card in a Pipeline column, the way the dispatcher's own moves leave it."""
        self.board.move(self.board.key_of(card), _STATE_BY_COLUMN[column])

    def _blocked(self, card: str, reason: str) -> None:
        """The board's own statement that a card is held, with the reason recorded on it."""
        self._move(card, "Blocked")
        self.board.save_metadata(self.board.key_of(card), blocked_by=reason)

    def _current_task(self, sprint: str, card: str) -> None:
        from ummanu.sprints import SprintWriter

        SprintWriter(self.board, data_dir=self.data_dir, instance=self.instance).set_current_task(
            role="observer", actor="observer", reference=sprint, task_reference=card
        )


class CurrentCardStateTests(SprintWorkFixture):
    """Where a sprint's current card stands, and since when, in both documents.

    The moment is the card's **last state transition** on the committed audit and nothing else. The
    journal a real installation keeps is mostly not transitions -- comments, reports, review
    verdicts, observer decisions and every other edit that moves `updated_at` -- so a card whose age
    were taken from the newest event of any kind would read as having just moved every time anybody
    said anything about it. The cases below drive exactly that journal.

    The events are appended to the committed audit directly, because what is under test is a read of
    a journal in the states this installation's history really holds it in: both event shapes, a
    card with no transition at all, and transitions of a card this sprint is not on.
    """

    #: Before the fixture's clock (2026-09-06T00:00:00Z), in the journal's own UTC spelling.
    FIRST = "2026-09-05T09:00:00Z"
    SECOND = "2026-09-05T18:00:00Z"
    LAST = "2026-09-05T21:30:00Z"
    AFTER = "2026-09-05T23:59:00Z"

    def setUp(self) -> None:
        super().setUp()
        self.reference = self.reference_of(self.create())
        self.card = self._card(self.reference)
        self._current_task(self.reference, self.card)
        self._move(self.card, "In progress")

    # -- the journal, in the shapes history holds ----------------------------------------------

    def _append(self, event: dict[str, Any]) -> None:
        """Commit one record to the card audit, under its own event id as its request id."""
        record = {"request_id": event["event_id"], **event}
        task_audit_for(self.board).append(record["request_id"], record)

    def _typed_move(self, at: str, source: str, target: str, *, ref: str | None = None) -> None:
        """A typed protocol event, which carries `transition.source` and `transition.target`."""
        self._append(
            {
                "event_id": f"board-event-{at}-{target}",
                "schema_version": 1,
                "record_type": PROTOCOL_EVENT_RECORD_TYPE,
                "kind": "card.moved",
                "ref": ref or self.card,
                "occurred_at": at,
                "actor": {"role": "dispatcher", "id": "dispatcher"},
                "transition": {"source": source, "target": target},
            }
        )

    def _legacy_move(self, at: str, source: str, target: str, *, ref: str | None = None) -> None:
        """A legacy event, which is `moved` with `payload.from` and `payload.to`."""
        self._append(
            {
                "event_id": f"evt_{at}_{target}",
                "schema_version": 1,
                "kind": "moved",
                "outcome": "success",
                "ref": ref or self.card,
                "occurred_at": at,
                "actor": {"role": "dispatcher", "id": "dispatcher"},
                "payload": {"from": source, "to": target},
            }
        )

    def _noise(self, at: str) -> None:
        """Everything a journal holds that is not a transition, after the last one that is.

        A comment, a worker report, a review verdict and the observer's own decision. Every one of
        them moves the card's `updated_at` and none of them moves the card.
        """
        for kind, role, payload in (
            ("commented", "po", {"body": "is this still going?"}),
            ("reported", "worker", {"kind": "done"}),
            ("reviewed", "reviewer", {"verdict": "red"}),
            ("decided", "observer", {"decision": "rework"}),
        ):
            self._append(
                {
                    "event_id": f"evt_{at}_{kind}",
                    "schema_version": 1,
                    "kind": kind,
                    "outcome": "success",
                    "ref": self.card,
                    "occurred_at": at,
                    "actor": {"role": role, "id": role},
                    "payload": payload,
                }
            )

    # -- what both documents say ---------------------------------------------------------------

    def _standing(self, reference: str | None = None) -> dict[str, Any]:
        """The section, asserted to be the same object in the listing and on the watched page."""
        subject = reference or self.reference
        layer = self.reads()
        watched = layer.sprint_state(subject)["work"]["current_card_state"]
        listed = self._entry(layer.sprint_list(), subject)["current_card_state"]
        self.assertEqual(listed, watched, "the listing and the page answer this differently")
        return watched

    # -- the cases -----------------------------------------------------------------------------

    def test_the_moment_is_the_last_transition_and_not_the_newest_event(self) -> None:
        """Criteria 1, 3 and 7: several transitions, then everything that is not one."""
        self._typed_move(self.FIRST, "ready", "in_progress")
        self._typed_move(self.SECOND, "in_progress", "validate")
        self._typed_move(self.LAST, "validate", "in_progress")
        self._noise(self.AFTER)

        standing = self._standing()

        self.assertEqual(standing["transition"], TRANSITION_RECORDED)
        self.assertEqual(standing["card"], self.card)
        # The title rides on the Pipeline listing entry the state is read from.
        self.assertEqual(standing["title"], "the current card")
        self.assertEqual(standing["state"], "in_progress")
        self.assertEqual(standing["since"], self.LAST)
        self.assertEqual(standing["age_seconds"], 9000.0)
        self.assertEqual(standing["source"]["name"], "journal")

    def test_a_legacy_moved_event_is_read_as_the_transition_it_is(self) -> None:
        """Criterion 3's other shape, and the two mixed: the last one wins whichever shape it is."""
        self._typed_move(self.FIRST, "ready", "in_progress")
        self._legacy_move(self.LAST, "in_progress", "validate")
        self._noise(self.AFTER)

        legacy_last = self._standing()
        self.assertEqual(legacy_last["since"], self.LAST)

        self._typed_move(self.AFTER, "validate", "in_progress")
        self.assertEqual(self._standing()["since"], self.AFTER)

    def test_the_updated_card_is_never_what_dates_it(self) -> None:
        """A journal of nothing but edits leaves the state undated rather than fresh."""
        self._noise(self.AFTER)

        standing = self._standing()

        self.assertEqual(standing["transition"], TRANSITION_ABSENT)
        self.assertIsNone(standing["since"])
        self.assertIsNone(standing["age_seconds"])

    def test_a_card_with_no_transition_says_so_rather_than_showing_a_zero(self) -> None:
        """Criterion 6: a card created and never moved is undated, and says which card it is."""
        standing = self._standing()

        self.assertEqual(standing["transition"], TRANSITION_ABSENT)
        self.assertEqual(standing["card"], self.card)
        self.assertEqual(standing["state"], "in_progress")
        self.assertIsNone(standing["since"])
        self.assertIsNone(standing["age_seconds"])
        self.assertIn("no state transition", standing["reason"])

    def test_another_card_is_never_what_dates_this_one(self) -> None:
        """The journal is one installation's, so a transition is matched to its own card."""
        other = self._card(self.reference)
        self._typed_move(self.LAST, "ready", "in_progress", ref=other)

        self.assertEqual(self._standing()["transition"], TRANSITION_ABSENT)

    def test_a_sprint_that_ended_shows_no_ticking_age(self) -> None:
        """Criterion 5, decided from `current_task.live` and not re-derived here.

        A stopped sprint keeps its card (it may be resumed); a closed one has none at all
        (secretary-1777), so neither can carry an age that ticks.
        """
        self._typed_move(self.LAST, "ready", "in_progress")
        ended = self.add_sprint_row("sprint:1001", status="stopped", current_task=self.card)

        standing = self._standing(ended)

        self.assertEqual(standing["transition"], TRANSITION_NOT_APPLICABLE)
        self.assertEqual(standing["card"], self.card)
        self.assertIsNone(standing["since"])
        self.assertIsNone(standing["age_seconds"])
        self.assertIn("the card it ended on", standing["reason"])
        # And the card's reference is still the sprint's own, which this section may not take away.
        watched = self.reads().sprint_state(ended)["work"]
        self.assertEqual(watched["current_task"]["ref"], self.card)
        self.assertFalse(watched["current_task"]["live"])

        closed = self.add_sprint_row("sprint:1003", status="closed", current_task=self.card)
        standing = self._standing(closed)
        self.assertEqual(standing["transition"], TRANSITION_NOT_APPLICABLE)
        self.assertIsNone(standing["card"])
        self.assertIsNone(standing["age_seconds"])

    def test_a_sprint_with_no_current_card_says_so(self) -> None:
        """Criterion 4, in the same words the rest of the document says it in."""
        empty = self.add_sprint_row("sprint:1002", status="open")

        standing = self._standing(empty)

        self.assertEqual(standing["transition"], TRANSITION_NOT_APPLICABLE)
        self.assertIsNone(standing["card"])
        self.assertIsNone(standing["state"])
        self.assertIsNone(standing["age_seconds"])
        self.assertIn("no current card", standing["reason"])

    def test_a_journal_nobody_can_read_takes_away_this_and_nothing_else(self) -> None:
        """Criterion 2: the new part is unavailable, and the card's own fields still stand."""
        self._typed_move(self.LAST, "ready", "in_progress")
        with mock.patch.object(type(task_audit_for(self.board)), "events", side_effect=PermissionError("audit denied")):
            watched = self.reads().sprint_state(self.reference)
            listed = self._entry(self.reads().sprint_list(), self.reference)

        for work in (watched["work"], listed):
            standing = work["current_card_state"]
            self.assertEqual(standing["source"]["state"], "unavailable")
            self.assertEqual(standing["transition"], TRANSITION_UNKNOWN)
            self.assertIsNone(standing["since"])
            self.assertIsNone(standing["age_seconds"])
            # The narration still names which card the answer would have been about.
            self.assertEqual(standing["card"], self.card)
            # And nothing of the card's own row was taken away with it.
            self.assertEqual(work["current_task"]["ref"], self.card)
            self.assertEqual(work["current_task"]["source"]["state"], "available")
        self.assertEqual(listed["ref"], self.reference)

    def test_both_documents_validate_with_the_section_on_them(self) -> None:
        self._typed_move(self.LAST, "ready", "in_progress")
        layer = self.reads()
        for document in (layer.sprint_list(), layer.sprint_state(self.reference)):
            with self.subTest(kind=document["kind"]):
                self.assertEqual(validate(document, "web-sprint", document["kind"]), [])


class SprintListTests(SprintWorkFixture):
    """The listing: every sprint at once, and no sprint described as something it is not.

    Two defects of the live installation are pinned here as cases rather than as prose. Both were
    reproduced on the owner's board on 2026-09-06, on roughly sixty closed sprints: a finished
    sprint reported a `current_task` with nothing saying it was historical, and reported its
    observer as `not_started` -- a sprint that ended described as one waiting for its head to come
    up.
    """

    def test_the_listing_answers_every_sprint_with_what_it_is_doing(self) -> None:
        open_sprint = self.reference_of(self.create())
        self.add_sprint_row("sprint:1001", status="closed", current_task="ummanu-1435")

        document = self.reads().sprint_list()

        self.assertEqual(document["kind"], "sprint_list")
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
        self.assertEqual(
            sorted(item["ref"] for item in document["sprints"]["items"]),
            [open_sprint, "sprint:1001"],
        )
        entry = self._entry(document, open_sprint)
        self.assertEqual(entry["goal"], "Give webproto a sprint create")
        self.assertEqual(entry["status"], "open")
        for section in ("current_task", "decision", "cards", "degraded_cards", "checks", "waiting"):
            self.assertIn("source", entry[section], f"{section} carries no availability")
        self.assertEqual(entry["observer"]["declared"]["profile"], OBSERVER_PROFILE)
        self.assertEqual(entry["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

    def test_the_status_filter_selects_and_an_unknown_status_is_refused(self) -> None:
        self.reference_of(self.create())
        self.add_sprint_row("sprint:1001", status="closed", current_task="ummanu-1435")

        closed = self.reads().sprint_list(statuses=["closed"])
        self.assertEqual([item["ref"] for item in closed["sprints"]["items"]], ["sprint:1001"])
        self.assertEqual(closed["filter"]["statuses"], ["closed"])

        with self.assertRaises(ValidationRefused):
            self.reads().sprint_list(statuses=["retired"])

    def test_a_closed_sprint_is_never_presented_as_working(self) -> None:
        """Criterion 3, in the two shapes the live board has it in."""
        self.add_sprint_row("sprint:1001", status="closed", current_task="ummanu-1435")
        self.add_sprint_row("sprint:1030", status="closed")

        document = self.reads().sprint_list()

        ended = self._entry(document, "sprint:1001")
        # A closed sprint has no current card, whatever its row stores (secretary-1777).
        self.assertIsNone(ended["current_task"]["ref"])
        self.assertFalse(ended["current_task"]["live"])
        self.assertEqual(ended["current_task"]["reason"], "sprint:1001 is closed; it has no current card")
        self.assertEqual(ended["waiting"]["state"], sprint_reads_module.WAITING_ENDED)
        self.assertEqual(ended["checks"]["state"], sprint_reads_module.CHECKS_NOT_APPLICABLE)
        # The second defect: a sprint that ended is not one whose observer has not come up.
        self.assertEqual(ended["observer"]["launch"]["state"], sprint_reads_module.OBSERVER_ENDED)
        self.assertNotEqual(ended["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

        without = self._entry(document, "sprint:1030")
        self.assertIsNone(without["current_task"]["ref"])
        self.assertFalse(without["current_task"]["live"])
        self.assertEqual(without["observer"]["launch"]["state"], sprint_reads_module.OBSERVER_ENDED)

    def test_a_watched_closed_sprint_answers_the_way_the_listing_does(self) -> None:
        """Criterion 2: one question, one answer, whichever operation is asked."""
        self.add_sprint_row("sprint:1001", status="closed", current_task="ummanu-1435")

        watched = self.reads().sprint_state("sprint:1001")
        listed = self._entry(self.reads().sprint_list(), "sprint:1001")

        self.assertEqual(validate(watched, "web-sprint", watched["kind"]), [])
        for section in ("current_task", "decision", "cards", "degraded_cards", "checks", "waiting"):
            self.assertEqual(watched["work"][section], listed[section], section)
        self.assertEqual(watched["observer"], listed["observer"])
        # And the sprint's own fields are still there beside the work -- with no current card, as
        # `sprint show` has it (secretary-1777).
        self.assertEqual(watched["sprint"]["value"]["ref"], "sprint:1001")
        self.assertIsNone(watched["sprint"]["value"]["current_task"])

    def test_the_checks_of_a_current_card_are_the_dispatchers_own_gate_record(self) -> None:
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)
        self._production(
            {},
            {
                card: {
                    "state": "validate",
                    "gate_state": "green",
                    # The receipt's own field names: the candidate it is bound to, and the base it
                    # was validated against. Reporting the base as the attested candidate would name
                    # the wrong commit, which is why the test states both.
                    "gate_attestation": {"validated_sha": "abc123", "base_sha": "def456"},
                }
            },
        )

        checks = self._entry(self.reads().sprint_list(), reference)["checks"]

        self.assertEqual(checks["state"], sprint_reads_module.CHECKS_GREEN)
        self.assertEqual(checks["card"], card)
        self.assertEqual(checks["gate"]["attested_sha"], "abc123")
        self.assertEqual(checks["gate"]["base_sha"], "def456")
        self.assertIn("abc123", checks["reason"])

    def test_a_card_whose_gate_has_not_passed_is_not_green_and_says_why(self) -> None:
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)
        self._production({}, {card: {"state": "rework", "gate_state": "", "gate_transport_error": ""}})

        entry = self._entry(self.reads().sprint_list(), reference)

        self.assertEqual(entry["checks"]["state"], sprint_reads_module.CHECKS_NOT_GREEN)
        self.assertIn("has not passed", entry["checks"]["reason"])
        self.assertEqual(entry["waiting"]["state"], sprint_reads_module.WAITING_WORKING)
        self.assertIn("rework", entry["waiting"]["reason"])

    def test_a_current_card_no_record_names_is_unknown_and_never_not_green(self) -> None:
        """The distinction criterion 2 is about: nobody said, which is not "it has not passed"."""
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)

        entry = self._entry(self.reads().sprint_list(), reference)

        self.assertEqual(entry["checks"]["state"], sprint_reads_module.CHECKS_UNKNOWN)
        self.assertIsNone(entry["checks"]["gate"])
        self.assertIn("no record", entry["checks"]["reason"])
        self.assertEqual(entry["waiting"]["state"], sprint_reads_module.WAITING_WAITING)

    def test_an_unreadable_pipeline_board_marks_its_own_sections_and_no_others(self) -> None:
        reference = self.reference_of(self.create())

        with _board_missing(self.board, "Pipeline"):
            document = self.reads().sprint_list()
        entry = self._entry(document, reference)

        self.assertEqual(document["cards"]["source"]["state"], "unavailable")
        self.assertIsNone(entry["cards"]["states"], "an empty grouping would claim the sprint has none")
        self.assertEqual(entry["decision"]["freshness"]["source"]["state"], "unavailable")
        self.assertIsNone(entry["decision"]["freshness"]["value"])
        # The sprint's own fields, and the dispatcher's, are other sources and stand.
        self.assertEqual(document["sprints"]["source"]["state"], "available")
        self.assertEqual(entry["goal"], "Give webproto a sprint create")
        self.assertEqual(entry["current_task"]["source"]["state"], "available")
        self.assertEqual(entry["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

    def test_an_unreadable_production_state_marks_only_what_it_feeds(self) -> None:
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)
        # In progress on purpose: this is the one column the board cannot settle by itself, so the
        # `unknown` below really is the dispatcher's answer missing rather than a board answer being
        # hidden. The Blocked and Ready cases are `WaitingSourceIsolationTests`.
        self._move(card, "In progress")
        (self.data_dir / "dispatcher" / "production-state.json").write_text("{", encoding="utf-8")

        document = self.reads().sprint_list()
        entry = self._entry(document, reference)

        self.assertEqual(document["liveness"]["source"]["state"], "unavailable")
        self.assertEqual(entry["checks"]["state"], sprint_reads_module.CHECKS_UNKNOWN)
        self.assertEqual(entry["waiting"]["state"], sprint_reads_module.WAITING_UNKNOWN)
        # And it says what the board did establish, so the `unknown` is not read as "nothing at all
        # is known about this card".
        self.assertIn("in progress", entry["waiting"]["reason"])
        self.assertIsNone(entry["degraded_cards"]["items"])
        self.assertEqual(entry["observer"]["launch"]["state"], OBSERVER_UNAVAILABLE)
        # And the board's answers are untouched.
        self.assertEqual(entry["current_task"]["ref"], card)
        self.assertEqual(entry["cards"]["source"]["state"], "available")

    def test_an_unreadable_sprint_board_marks_the_listing_and_still_says_the_rest(self) -> None:
        self.create()
        original = self.board.call
        sprint_board = _board_id(self.board, SPRINT_BOARD_NAME)

        def refuse(method: str, **params: Any) -> Any:
            if method == "getAllTasks" and params.get("project_id") == sprint_board:
                raise TaskError("backend_error", "the sprint board is unavailable", 1)
            return original(method, **params)

        self.board.call = refuse  # type: ignore[method-assign]
        document = self.reads().sprint_list()

        self.assertEqual(document["sprints"]["source"]["state"], "unavailable")
        # `null` items, and never `[]`: an empty listing is the affirmative claim that this
        # installation holds no sprints, which is exactly what a board that refused cannot say.
        self.assertIsNone(document["sprints"]["items"])
        self.assertEqual(document["cards"]["source"]["state"], "available")
        self.assertEqual(document["liveness"]["source"]["state"], "available")
        self.assertEqual(document["journal"]["source"]["state"], "available")

    def test_the_listing_creates_no_board_and_starts_nothing(self) -> None:
        document = self.reads().sprint_list()

        self.assertEqual(document["sprints"]["items"], [])
        self.assertFalse(
            [method for method, _ in self.board.calls if method in _WRITE_METHODS],
            "a read of the listing wrote to the board",
        )


class WaitingSourceIsolationTests(SprintWorkFixture):
    """One source that refused must not take away an answer another source already gave.

    Round 1 of this card had `_waiting` ask whether the dispatcher's production state was readable
    *before* it looked at the Pipeline listing it had already read, so an unreadable
    `production-state.json` turned a card the board held in Blocked -- with its reason on it -- into
    `unknown`. That is the collapse `webproto/sources.py` exists to prevent, stated there as: "there
    are no running agents" and "the file that would say so could not be read" are different answers.

    The cases below fix the order in place. They inherit `SprintListTests` for its fixture helpers
    and run over both operations, because the two share `_work` and a repair that reached only one
    of them would be no repair at all.
    """

    def _work(self, reference: str) -> list[tuple[str, dict]]:
        """The same sprint's work sections, from the listing and from the watched page."""
        return [
            ("sprint_list", self._entry(self.reads().sprint_list(), reference)),
            ("sprint_state", self.reads().sprint_state(reference)["work"]),
        ]

    def _sprint_on_a_card(self) -> tuple[str, str]:
        reference = self.reference_of(self.create())
        card = self._card(reference)
        self._current_task(reference, card)
        return reference, card

    def _break_production(self) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text("{", encoding="utf-8")

    def test_a_blocked_current_card_is_reported_even_with_no_production_state(self) -> None:
        """The reviewer's reproduction, as a case: board available, dispatcher unreadable."""
        reference, card = self._sprint_on_a_card()
        self._blocked(card, "waiting for an operator")
        self._break_production()

        for operation, work in self._work(reference):
            with self.subTest(operation=operation):
                waiting = work["waiting"]
                self.assertEqual(waiting["state"], sprint_reads_module.WAITING_BLOCKED)
                self.assertIn("waiting for an operator", waiting["reason"])
                # Sourced from the listing that established it, not from the state that refused.
                self.assertEqual(waiting["source"]["state"], "available")
                # And the sections that really do need the dispatcher still say it is missing.
                self.assertEqual(work["checks"]["state"], sprint_reads_module.CHECKS_UNKNOWN)
                self.assertIsNone(work["degraded_cards"]["items"])
                self.assertEqual(work["cards"]["states"], {"blocked": [card]})

    def test_a_blocked_current_card_reads_the_same_when_the_dispatcher_does_answer(self) -> None:
        """The control: the board's answer is not a fallback used only when something failed."""
        reference, card = self._sprint_on_a_card()
        self._blocked(card, "waiting for an operator")
        self._production({}, {card: {"state": "claimed"}})

        for operation, work in self._work(reference):
            with self.subTest(operation=operation):
                self.assertEqual(work["waiting"]["state"], sprint_reads_module.WAITING_BLOCKED)
                self.assertIn("waiting for an operator", work["waiting"]["reason"])

    def test_a_blocked_card_with_no_recorded_reason_says_that_rather_than_nothing(self) -> None:
        reference, card = self._sprint_on_a_card()
        self._move(card, "Blocked")
        self._break_production()

        waiting = self._entry(self.reads().sprint_list(), reference)["waiting"]

        self.assertEqual(waiting["state"], sprint_reads_module.WAITING_BLOCKED)
        self.assertIn("no reason recorded on the card", waiting["reason"])

    def test_a_card_nobody_has_claimed_is_waiting_even_with_no_production_state(self) -> None:
        """The same rule at the sibling branch: Ready is the board saying nothing is running."""
        reference, card = self._sprint_on_a_card()
        self._move(card, "Ready")
        self._break_production()

        for operation, work in self._work(reference):
            with self.subTest(operation=operation):
                waiting = work["waiting"]
                self.assertEqual(waiting["state"], sprint_reads_module.WAITING_WAITING)
                self.assertEqual(waiting["source"]["state"], "available")
                self.assertIn("nothing has claimed it", waiting["reason"])

    def test_a_finished_current_card_is_the_sprint_waiting_for_its_next_cut(self) -> None:
        reference, card = self._sprint_on_a_card()
        self._move(card, "Done")
        self._break_production()

        waiting = self._entry(self.reads().sprint_list(), reference)["waiting"]

        self.assertEqual(waiting["state"], sprint_reads_module.WAITING_WAITING)
        self.assertIn("cut the next card", waiting["reason"])

    def test_an_active_column_with_no_record_still_prefers_what_the_board_settled(self) -> None:
        """The dispatcher answered, and holds no record: the column is the better answer where it
        has one, and the bare "no record" is what is left where it does not."""
        reference, card = self._sprint_on_a_card()
        self._move(card, "Ready")
        self._production({}, {})

        ready = self._entry(self.reads().sprint_list(), reference)["waiting"]
        self.assertEqual(ready["state"], sprint_reads_module.WAITING_WAITING)
        self.assertIn("nothing has claimed it", ready["reason"])
        self.assertEqual(ready["source"]["state"], "available")

        self._move(card, "In progress")
        active = self._entry(self.reads().sprint_list(), reference)["waiting"]
        self.assertEqual(active["state"], sprint_reads_module.WAITING_WAITING)
        self.assertIn("holds no record", active["reason"])

    def test_an_open_decision_card_waits_on_the_po_and_then_on_the_owner(self) -> None:
        """secretary-1761: no head runs it, so its record is not `working`; the reason says who has it."""
        from ummanu.board.owner_handover import waiting_owner

        reference = self.reference_of(self.create())
        bind_observer(self, reference)
        writer = TaskWriter(self.board, data_dir=self.data_dir)
        card = str(
            writer.create(
                role="observer", actor="observer", project="ummanu", task_type="decision",
                title="the question", sprint=reference,
            )["task"]["ref"]
        )
        self._current_task(reference, card)
        self._move(card, "In progress")
        self._production({}, {card: {"state": "po_submitted"}})

        for operation, work in self._work(reference):
            with self.subTest(operation=operation, holder="po"):
                waiting = work["waiting"]
                self.assertEqual(waiting["state"], sprint_reads_module.WAITING_WAITING)
                self.assertEqual(waiting["reason"], f"{card} (decision) is with the PO")
                self.assertEqual(waiting["card"], card)

        writer.handover(
            role="po", actor="po", reference=card, to="owner", reason="The owner holds the payment card."
        )
        self.assertIsNotNone(waiting_owner(writer.reader.show(card)))

        for operation, work in self._work(reference):
            with self.subTest(operation=operation, holder="owner"):
                waiting = work["waiting"]
                self.assertEqual(waiting["state"], sprint_reads_module.WAITING_WAITING)
                self.assertEqual(
                    waiting["reason"], f"{card} (decision) is handed to the owner: The owner holds the payment card."
                )
                self.assertEqual(waiting["card"], card)

    def test_a_dispatcher_record_still_decides_an_active_column(self) -> None:
        """The board deliberately settles nothing here: a column is not evidence of a head."""
        reference, card = self._sprint_on_a_card()
        self._move(card, "In progress")
        self._production({}, {card: {"state": "reviewing"}})

        waiting = self._entry(self.reads().sprint_list(), reference)["waiting"]

        self.assertEqual(waiting["state"], sprint_reads_module.WAITING_WORKING)
        self.assertIn("reviewing", waiting["reason"])

    def test_a_sprint_the_board_could_not_read_says_so_and_claims_nothing_else(self) -> None:
        self.create()
        original = self.board.call
        sprint_board = _board_id(self.board, SPRINT_BOARD_NAME)

        def refuse(method: str, **params: Any) -> Any:
            if method == "getAllTasks" and params.get("project_id") == sprint_board:
                raise TaskError("backend_error", "the sprint board is unavailable", 1)
            return original(method, **params)

        self.board.call = refuse  # type: ignore[method-assign]
        work = self.reads().sprint_state("sprint:1")["work"]

        for section in ("current_task", "decision", "cards", "degraded_cards", "checks", "waiting"):
            with self.subTest(section=section):
                self.assertEqual(work[section]["source"]["state"], "unavailable", section)
        self.assertEqual(work["waiting"]["state"], sprint_reads_module.WAITING_UNKNOWN)
        self.assertEqual(work["checks"]["state"], sprint_reads_module.CHECKS_UNKNOWN)

    def test_an_ended_sprint_answers_from_its_own_row_whatever_the_dispatcher_does(self) -> None:
        """A closed sprint is closed: the section may not borrow an unrelated unavailability."""
        self.add_sprint_row("sprint:1001", status="closed", current_task="ummanu-1435")
        self._break_production()

        work = self.reads().sprint_state("sprint:1001")["work"]

        for section in ("waiting", "checks", "current_task"):
            with self.subTest(section=section):
                self.assertEqual(work[section]["source"]["state"], "available", section)
        self.assertEqual(work["waiting"]["state"], sprint_reads_module.WAITING_ENDED)
        self.assertEqual(work["checks"]["state"], sprint_reads_module.CHECKS_NOT_APPLICABLE)


class SprintReadCommandTests(SprintProtocolFixture):
    """`ummanu sprint list` and `ummanu sprint status` as clients of the two operations.

    Criterion 6: the commands keep being what an operator types, and stop being a second reader.
    What is checked here is exactly that -- the document the command prints is the document the
    operation returned, and a typed refusal reaches the exit status `ummanu web-read` maps it to.
    """

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main([*argv, "--instance", str(self.instance), "--data-dir", str(self.data_dir)])
        return code, output.getvalue(), errors.getvalue()

    def test_sprint_list_prints_the_document_the_operation_answered(self) -> None:
        reference = self.reference_of(self.create())

        code, output, errors = self._run(["sprint", "list"])

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint_list")
        self.assertEqual([item["ref"] for item in document["sprints"]["items"]], [reference])

    def test_sprint_status_renders_the_e2e_runs_used_of_the_budget(self) -> None:
        """secretary-1796: the watched sprint's value carries `e2e: <used> of <budget>` and who spent it."""
        reference = self.reference_of(self.create())
        with self.board.transaction():
            self.board.call(
                "chargeSprintE2e",
                sprint_ref=reference,
                task_ref="ummanu-90",
                dispatch_id="ummanu-90-e2e-1-00000001",
                at="2026-09-27T10:00:00Z",
            )

        code, output, errors = self._run(["sprint", "status", "--ref", reference])

        self.assertEqual(code, 0, errors)
        e2e = json.loads(output)["sprint"]["value"]["e2e"]
        self.assertEqual((e2e["summary"], e2e["cards"]), ("e2e: 1 of 3", ["ummanu-90"]))

    def test_sprint_list_passes_its_filter_through_and_decides_nothing(self) -> None:
        self.create()
        self.add_sprint_row("sprint:1001", status="closed", current_task="ummanu-1435")

        code, output, _errors = self._run(["sprint", "list", "--status", "closed"])

        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual([item["ref"] for item in document["sprints"]["items"]], ["sprint:1001"])
        self.assertFalse(document["sprints"]["items"][0]["current_task"]["live"])

    def test_sprint_status_prints_the_watched_sprint_document(self) -> None:
        reference = self.reference_of(self.create())

        code, output, errors = self._run(["sprint", "status", "--ref", reference])

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint")
        self.assertEqual(document["ref"], reference)
        self.assertIn("waiting", document["work"])
        self.assertEqual(document["observer"]["launch"]["state"], OBSERVER_NOT_STARTED)

    def test_the_command_prints_whatever_the_operation_returns(self) -> None:
        """Clientship, checked rather than described: no shaping of its own on the way out."""
        answered = {"kind": "sprint_list", "sprints": {"items": []}}
        with mock.patch.object(SprintReadLayer, "sprint_list", return_value=answered):
            code, output, _errors = self._run(["sprint", "list"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), answered)

    def test_a_sprint_nobody_holds_is_the_exit_status_web_read_uses(self) -> None:
        self.create()

        code, output, errors = self._run(["sprint", "status", "--ref", "sprint:404"])

        self.assertEqual(code, _EXIT_BY_CODE["not_found"])
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "not_found")

    def test_an_unvalidated_config_with_an_explicit_data_dir_still_answers_from_the_board(self) -> None:
        """Criterion 4: the config is one more source that refused, not a refusal of the operation.

        The previous round documented this as a stricter precondition and that was wrong: an
        operator with a usable board transport and an explicit `--data-dir` had these answers before
        the commands became clients of the layer, and losing them is losing an answer. The rule is
        the same one every section obeys -- the unvalidated config appears as an unavailable source,
        and takes away only what it owns.
        """
        reference = self.reference_of(self.create())
        (self.instance / "instance.yaml").write_text(
            f"version: 1\nname: test\ndata_dir: {self.data_dir}\n", encoding="utf-8"
        )

        code, output, errors = self._run(["sprint", "list"])
        self.assertEqual(code, 0, errors)
        listing = json.loads(output)
        self.assertEqual([item["ref"] for item in listing["sprints"]["items"]], [reference])
        self.assertEqual(listing["installation"]["source"]["state"], "unavailable")
        self.assertIn("does not validate", listing["installation"]["source"]["reason"])
        self.assertEqual(listing["sprints"]["source"]["state"], "available")

        code, output, errors = self._run(["sprint", "status", "--ref", reference])
        self.assertEqual(code, 0, errors)
        watched = json.loads(output)
        self.assertEqual(watched["sprint"]["value"]["ref"], reference)
        self.assertEqual(watched["installation"]["source"]["state"], "unavailable")

    def test_an_installation_that_does_not_validate_is_the_backend_status(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["sprint", "list", "--instance", str(self.tmp / "not-an-instance")])

        self.assertEqual(code, _EXIT_BY_CODE["backend_unavailable"])
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")


class ClosedSprintTruthTests(SprintProtocolFixture):
    """`sprint show`, `sprint status` and `sprint list` read truthfully about a finished sprint.

    secretary-1777, the "CLI truthfulness" items of sprint:1467. A closed sprint has no current card
    in any read output (PO decision of 2026-09-26 on issue:002bce88: null plus a status, no renamed
    field), while its stored row keeps what it held. A stopped sprint may be resumed and keeps its
    card, qualified. `sprint status` names the status first, and `list` and `show` agree on budget.
    """

    CLOSED_CARD = "ummanu-1435"
    STOPPED_CARD = "ummanu-1436"
    OPEN_CARD = "ummanu-1437"

    def setUp(self) -> None:
        super().setUp()
        self.add_sprint_row("sprint:1001", status="closed", current_task=self.CLOSED_CARD)
        self.add_sprint_row("sprint:1002", status="stopped", current_task=self.STOPPED_CARD)
        self.add_sprint_row("sprint:1003", status="open", current_task=self.OPEN_CARD)

    def _run(self, argv: list[str], *, explicit: bool = True) -> tuple[int, str, str]:
        """One command, told its installation by `--instance` or by the default `UMMANU_INSTANCE`."""
        output, errors = io.StringIO(), io.StringIO()
        environment = {"UMMANU_INSTANCE": str(self.instance), "UMMANU_DATA_DIR": str(self.data_dir)}
        flags = ["--instance", str(self.instance), "--data-dir", str(self.data_dir)] if explicit else []
        with (
            self.board_injected(),
            mock.patch("ummanu.sprint_commands.board_client", return_value=self.board),
            mock.patch.dict("os.environ", environment),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main([*argv, *flags])
        return code, output.getvalue(), errors.getvalue()

    def _json(self, argv: list[str], **kwargs: Any) -> tuple[str, dict[str, Any]]:
        code, output, errors = self._run(argv, **kwargs)
        self.assertEqual(code, 0, errors)
        return output, json.loads(output)

    def test_show_of_a_closed_sprint_names_no_current_card_and_the_row_keeps_it(self) -> None:
        for reference, expected in (
            ("sprint:1001", None),
            ("sprint:1002", self.STOPPED_CARD),
            ("sprint:1003", self.OPEN_CARD),
        ):
            with self.subTest(reference=reference):
                _output, shown = self._json(["sprint", "show", "--ref", reference])
                self.assertEqual(shown["current_task"], expected)
        # Only the read output changed: the stored row and every internal reader keep the card.
        self.assertEqual(self.metadata_of("sprint:1001")["sprint_current_task"], self.CLOSED_CARD)
        self.assertEqual(
            sprints_module.SprintReader(self.board, data_dir=self.data_dir).show("sprint:1001")["current_task"],
            self.CLOSED_CARD,
        )

    def test_status_of_a_closed_sprint_names_its_old_card_nowhere_as_current(self) -> None:
        output, document = self._json(["sprint", "status", "--ref", "sprint:1001"])

        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
        work = document["work"]
        self.assertEqual(
            {key: value for key, value in work["current_task"].items() if key != "source"},
            {"ref": None, "live": False, "reason": "sprint:1001 is closed; it has no current card"},
        )
        self.assertIsNone(document["sprint"]["value"]["current_task"])
        self.assertIsNone(work["current_card_state"]["card"])
        self.assertIsNone(work["checks"]["card"])
        self.assertIsNone(work["waiting"]["card"])
        self.assertEqual(work["waiting"]["state"], sprint_reads_module.WAITING_ENDED)
        self.assertIn(self.CLOSED_CARD, output)
        # The only place the card may appear is where the sprint's cards are listed by column.
        rest = {**document, "work": {key: value for key, value in work.items() if key != "cards"}}
        self.assertIn(self.CLOSED_CARD, json.dumps(work["cards"]))
        self.assertNotIn(self.CLOSED_CARD, json.dumps(rest))

        listed = self._json(["sprint", "list", "--status", "closed"])[1]
        entry = next(item for item in listed["sprints"]["items"] if item["ref"] == "sprint:1001")

        # Two reads, two wall-clock stamps: they may straddle a second (CI red on secretary-1778),
        # so the answer is compared whole except for when each read was observed.
        def unstamped(current: dict) -> dict:
            return {**current, "source": {k: v for k, v in current["source"].items() if k != "observed_at"}}

        self.assertEqual(unstamped(entry["current_task"]), unstamped(work["current_task"]))

    def test_status_prints_the_sprints_status_first(self) -> None:
        for reference, status in (("sprint:1001", "closed"), ("sprint:1002", "stopped"), ("sprint:1003", "open")):
            with self.subTest(reference=reference):
                output, document = self._json(["sprint", "status", "--ref", reference])
                self.assertTrue(output.startswith(f'{{"status":"{status}",'), output[:40])
                self.assertEqual(next(iter(document)), "status")
                self.assertEqual(document["status"], document["sprint"]["value"]["status"])
                self.assertEqual(len(output.strip().splitlines()), 1)

    def test_open_and_stopped_sprints_keep_their_card(self) -> None:
        _output, stopped = self._json(["sprint", "status", "--ref", "sprint:1002"])
        self.assertEqual(stopped["work"]["current_task"]["ref"], self.STOPPED_CARD)
        self.assertFalse(stopped["work"]["current_task"]["live"])
        self.assertIn("not work in progress", stopped["work"]["current_task"]["reason"])
        self.assertEqual(stopped["sprint"]["value"]["current_task"], self.STOPPED_CARD)

        _output, opened = self._json(["sprint", "status", "--ref", "sprint:1003"])
        self.assertEqual(opened["work"]["current_task"]["ref"], self.OPEN_CARD)
        self.assertTrue(opened["work"]["current_task"]["live"])
        self.assertEqual(opened["sprint"]["value"]["current_task"], self.OPEN_CARD)

    def test_list_and_show_answer_the_same_budget_by_the_installations_thresholds(self) -> None:
        """issue:16277741: `list` and `show` agree, with `--instance` and with `UMMANU_INSTANCE`."""
        instance_file = self.instance / "instance.yaml"
        instance_file.write_text(
            instance_file.read_text(encoding="utf-8") + "sprint_budget:\n  signal: 12\n  hard: 30\n",
            encoding="utf-8",
        )
        # A budget is the charges that make it up, recorded through the product's own write.
        writer = sprints_module.SprintWriter(self.board, data_dir=self.data_dir)
        for event_type, count in (("red_review", 4), ("blocked", 2)):
            for occurrence in range(count):
                writer.record_budget(
                    role="steward",
                    actor="fixture",
                    reference="sprint:1003",
                    event_type=event_type,
                    request_id=f"fixture-budget-{event_type}-{occurrence}",
                )
        fields = ("total", "thresholds", "signal_reached", "hard_reached")
        expected = {"total": 6, "thresholds": {"signal": 12, "hard": 30}, "signal_reached": False, "hard_reached": False}

        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                _output, shown = self._json(["sprint", "show", "--ref", "sprint:1003"], explicit=explicit)
                _output, listed = self._json(["sprint", "list"], explicit=explicit)
                entry = next(item for item in listed["sprints"]["items"] if item["ref"] == "sprint:1003")
                self.assertEqual({field: shown["budget"][field] for field in fields}, expected)
                self.assertEqual({field: entry["budget"][field] for field in fields}, expected)


class CommentFixture(SprintProtocolFixture):
    """One open sprint and the pieces both comment suites drive.

    Delivery is state the dispatcher keeps, so a test that wants a comment in a particular delivery
    state writes that state into the production file the dispatcher writes -- exactly as the launch
    tests above write an observer record. Nothing here runs a tick, wakes a head or opens a terminal.
    """

    #: The body every case comments with unless it is deliberately commenting something else.
    BODY = "PO: slow down on the second card and finish the first"

    def setUp(self) -> None:
        super().setUp()
        self.reference = self.reference_of(self.create())

    # -- the operation, and what it left behind ------------------------------------------------

    def comment(self, **kwargs: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "request_id": "po-comment-1",
            "actor": "operator",
            "reference": self.reference,
            "body": self.BODY,
            "role": "po",
        }
        request.update(kwargs)
        return self.ops().sprint_comment(**request)

    def board_comments(self) -> list[str]:
        """Every comment on this sprint's row, as the board actually holds them."""
        row = next(task for task in self.sprint_rows() if task["reference"] == self.reference)
        return [str(entry.get("comment") or "") for entry in self.board.comments(int(row["id"]))]

    def audit_events(self) -> list[dict[str, Any]]:

        return task_audit_for(self.board).events()

    def significant_events(self) -> list[str]:
        """The events that are a semantic wake for this sprint's observer, by the product's own rule.

        `is_significant_observer_event` is what the dispatcher's delivery decision is made from, so
        this is the input a second wake would have to come from -- not a restatement of it.
        """
        from ummanu.tasks import is_significant_observer_event

        return [
            str(event.get("event_id") or "")
            for event in self.audit_events()
            if is_significant_observer_event(event, linked_refs=set(), sprint_ref=self.reference)
        ]

    def pending_wake(self) -> dict[str, Any]:
        """What the dispatcher's own delivery decision would find owed to this sprint's observer.

        The production function, driven over this fixture's board and audit: it is what decides
        whether a head is woken at all, so asking it is the difference between covering the wake and
        counting comments and hoping.
        """
        from types import SimpleNamespace

        from ummanu.dispatch.observer import ObserverRecord, _observer_event_state
        from ummanu.sprints import SprintReader

        runtime = SimpleNamespace(
            sprints=SprintReader(self.board, data_dir=self.data_dir, thresholds=None),
            audit=task_audit_for(self.board),
        )
        record = ObserverRecord.from_json(
            (self.production_payload().get("observers") or {}).get(self.reference)
            or {"sprint": self.reference}
        )
        owed = _observer_event_state(runtime, self.reference, record)
        # The age of the latest event moves with the wall clock and is not part of the decision.
        return {key: value for key, value in owed.items() if key != "age_seconds"}

    def production_payload(self) -> dict[str, Any]:
        return json.loads(self.production_path().read_text(encoding="utf-8"))

    def production_path(self) -> Path:
        return self.data_dir / "dispatcher" / "production-state.json"

    def data_plane(self) -> dict[str, bytes]:
        """Every file of this installation's data plane, so a write anywhere in it is visible."""
        return {
            str(path.relative_to(self.data_dir)): path.read_bytes()
            for path in sorted(self.data_dir.rglob("*"))
            if path.is_file()
        }

    # -- the delivery state a case wants ------------------------------------------------------

    def delivery_record(self, **delivery: Any) -> None:
        """The dispatcher's observer record for this sprint, with the delivery cursors a case needs."""
        self._production(
            {
                self.reference: {
                    "sprint": self.reference,
                    "head": OBSERVER_PROFILE,
                    "state": "working",
                    "launches": 1,
                    "bound": True,
                    "delivery": {"stage": "idle", **delivery},
                }
            }
        )

    def delivery_of(self, comment_id: str, *, through: str = "") -> dict[str, Any]:
        document = self.reads().sprint_comment_delivery(self.reference, comment_id)
        self.assertEqual(document["kind"], "sprint_comment_delivery")
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [], through)
        return document["delivery"]


class CommentTests(CommentFixture):
    """Criteria 1-3: one named operation, a durable identifier, and a repeat that does nothing."""

    def test_a_comment_is_saved_and_answers_with_a_durable_identifier(self) -> None:
        answered = self.comment()

        self.assertEqual(answered["kind"], "sprint_comment")
        self.assertEqual(answered["ref"], self.reference)
        self.assertTrue(answered["saved"])
        self.assertEqual(validate(answered, "web-sprint", answered["kind"]), [])
        # The identifier is the committed audit event of the comment, and the read takes it back.
        committed = [event for event in self.audit_events() if event["kind"] == "commented"]
        self.assertEqual([event["event_id"] for event in committed], [answered["comment_id"]])
        self.assertEqual(self.board_comments(), [f"[po]\n{self.BODY}"])
        said = self.delivery_of(answered["comment_id"])["source"]
        self.assertTrue(said["name"])

    def test_the_identifier_is_the_one_a_later_read_uses_and_not_a_board_row(self) -> None:
        answered = self.comment()
        comment = self.reads().sprint_comment_delivery(self.reference, answered["comment_id"])["comment"]
        self.assertEqual(comment["state"], COMMENT_SAVED)
        self.assertEqual(comment["id"], answered["comment_id"])
        self.assertEqual(comment["role"], "po")
        # Durable, and the audit's own: not the board row a caller would have to interpret, and not
        # the position of the comment in whatever order the board happens to return its rows in.
        row = next(task for task in self.sprint_rows() if task["reference"] == self.reference)
        self.assertNotEqual(answered["comment_id"], str(row["id"]))
        self.assertEqual(
            answered["comment_id"],
            next(event["event_id"] for event in self.audit_events() if event["kind"] == "commented"),
        )

    def test_an_identifier_this_sprint_does_not_hold_is_absent_and_never_delivered(self) -> None:
        self.comment()
        document = self.reads().sprint_comment_delivery(self.reference, "evt_nobody")
        self.assertEqual(document["comment"]["state"], COMMENT_ABSENT)
        self.assertEqual(document["comment"]["source"]["name"], "journal")
        self.assertEqual(document["delivery"]["state"], DELIVERY_UNKNOWN)
        self.assertEqual(document["delivery"]["source"]["name"], "journal")

    def test_a_repeat_makes_no_second_comment_no_second_event_and_no_second_wake(self) -> None:
        """Criterion 2, as three separate assertions because they are three separate failures.

        Counting comments would pass with a duplicated audit event, and counting audit events would
        pass with a second wake of the head. So the wake is asked of the dispatcher's own delivery
        decision, and the production state is compared byte for byte -- a launch, a deferral or a
        moved cursor all write to it.
        """
        first = self.comment()
        comments, events = self.board_comments(), [event["event_id"] for event in self.audit_events()]
        significant, owed = self.significant_events(), self.pending_wake()
        production = self.production_path().read_bytes()

        repeated = self.comment()

        self.assertFalse(repeated["saved"], "a repeat found the comment already saved")
        self.assertEqual(repeated["comment_id"], first["comment_id"])
        self.assertEqual(self.board_comments(), comments, "a repeat wrote a second comment")
        self.assertEqual(
            [event["event_id"] for event in self.audit_events()], events, "a repeat wrote a second event"
        )
        self.assertEqual(self.significant_events(), significant, "a repeat left a second semantic wake")
        self.assertEqual(self.pending_wake(), owed, "the dispatcher would wake the head a second time")
        self.assertEqual(self.production_path().read_bytes(), production)

    def test_the_one_comment_is_what_the_dispatcher_would_wake_the_observer_for(self) -> None:
        """The control for the case above: without it, "unchanged" could mean "never owed at all"."""
        self.assertFalse(self.pending_wake()["pending"])
        answered = self.comment()
        owed = self.pending_wake()
        self.assertTrue(owed["pending"])
        self.assertEqual(owed["event_id"], answered["comment_id"])

    def test_a_repeat_over_different_content_is_refused_rather_than_answered(self) -> None:
        self.comment()
        with self.assertRaises(ValidationRefused) as refused:
            self.comment(body="PO: actually, stop the sprint")
        self.assertIn("different inputs", refused.exception.message)
        self.assertEqual(self.board_comments(), [f"[po]\n{self.BODY}"])

    def test_a_repeat_over_a_different_sprint_role_or_actor_is_refused_too(self) -> None:
        self.comment()
        other = self.add_sprint_row("sprint:9001")
        for label, request in (
            ("sprint", {"reference": other}),
            ("role", {"role": "steward"}),
            ("actor", {"actor": "somebody-else"}),
        ):
            with self.subTest(differs=label), self.assertRaises(ValidationRefused):
                self.comment(**request)

    def test_a_request_id_that_already_owns_another_sprint_write_is_refused(self) -> None:
        """The same refusal, over the id of a write that is not a comment at all."""
        with self.assertRaises(ValidationRefused):
            self.comment(request_id="req-1")

    def test_a_comment_without_a_request_id_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(ValidationRefused):
            self.comment(request_id="  ")
        self.assertEqual(self.board_comments(), [])

    def test_the_writer_keeps_every_rule_it_has(self) -> None:
        """The operation restates no rule of `SprintWriter`; it says which code carries its answer.

        The closed sprint used to be one of these, and it is not any more: secretary-1578 admits a
        PO comment on a sprint that has ended (issue:9eee1d8ee505bc4ecdc2), which
        `PostCloseCommentTests` pins. What is left here is a rule the writer still holds.
        """
        # The observer was the refused role here until secretary-1765 admitted its comment under the
        # identity guard; `owner` is a role the sprint writer still refuses.
        with self.assertRaises(ValidationRefused):
            self.comment(request_id="po-role", role="owner")

    def test_a_body_the_writer_refuses_is_a_validation_refusal(self) -> None:
        with self.assertRaises(ValidationRefused):
            self.comment(body="   ")

    def test_a_half_written_comment_is_repeated_under_the_same_request_id(self) -> None:
        """`audit_pending` names this operation and this id, never the create's."""
        from ummanu.sprints import SprintWriter

        with mock.patch.object(
            SprintWriter, "comment", side_effect=TaskError("audit_pending", "audit repair required", 4)
        ), self.assertRaises(OperationPending) as refused:
            self.comment()
        self.assertEqual(refused.exception.data["reason"], COMMENT_PENDING_REASON)
        action = refused.exception.data["action"]
        self.assertEqual(action["operation"], SPRINT_COMMENT_OPERATION)
        self.assertEqual(action["request_id"], "po-comment-1")
        self.assertTrue(action["repeat_request"])


class CommentDeliveryTests(CommentFixture):
    """Criterion 4: the five answers, each from the delivery machinery that already exists."""

    def setUp(self) -> None:
        super().setUp()
        self.comment_id = self.comment()["comment_id"]

    def test_no_observer_record_is_unknown_and_never_not_delivered(self) -> None:
        delivery = self.delivery_of(self.comment_id)
        self.assertEqual(delivery["state"], DELIVERY_UNKNOWN)
        self.assertIsNone(delivery["batch"])
        self.assertIn("no observer record", delivery["reason"])

    def test_a_saved_comment_no_batch_carries_yet_says_exactly_that(self) -> None:
        self.delivery_record()
        delivery = self.delivery_of(self.comment_id)
        self.assertEqual(delivery["state"], DELIVERY_SAVED)
        self.assertEqual(delivery["batch"]["stage"], "idle")

    def test_a_batch_held_for_a_busy_head_is_waiting(self) -> None:
        self.delivery_record(stage="waiting_for_idle", reason="the head is mid-turn")
        delivery = self.delivery_of(self.comment_id)
        self.assertEqual(delivery["state"], DELIVERY_WAITING)
        self.assertIn("the head is mid-turn", delivery["reason"])

    def test_a_batch_in_flight_that_carries_it_is_waiting(self) -> None:
        for stage in ("delivery_intent", "awaiting_ack"):
            with self.subTest(stage=stage):
                self.delivery_record(
                    stage=stage, through_event=self.comment_id, delivery_id="delivery-1"
                )
                delivery = self.delivery_of(self.comment_id)
                self.assertEqual(delivery["state"], DELIVERY_WAITING)
                self.assertEqual(delivery["batch"]["through_event"], self.comment_id)

    def test_an_acknowledged_batch_is_handed_over_and_says_it_is_not_acceptance(self) -> None:
        self.delivery_record(
            stage="idle",
            acknowledged_through=self.comment_id,
            acknowledged_delivery_id="delivery-1",
        )
        document = self.reads().sprint_comment_delivery(self.reference, self.comment_id)
        self.assertEqual(document["delivery"]["state"], DELIVERY_HANDED_OVER)
        self.assertIn("not a statement that the comment was read", document["delivery"]["reason"])
        self.assertFalse(document["acceptance"]["established"])
        self.assertEqual(document["acceptance"]["issue"], ACCEPTANCE_ISSUE)

    def test_a_failed_batch_is_an_error_carrying_the_recorded_reason(self) -> None:
        self.delivery_record(
            stage="retry_deferred",
            through_event=self.comment_id,
            delivery_id="delivery-1",
            wake_attempts=3,
            wake_failures=2,
            launch_delivery_failures=1,
            last_failure_reason="the observer pane refused the prompt",
        )
        delivery = self.delivery_of(self.comment_id)
        self.assertEqual(delivery["state"], DELIVERY_ERROR)
        self.assertIn("the observer pane refused the prompt", delivery["reason"])
        self.assertEqual(delivery["batch"]["wake_failures"], 2)
        self.assertIn("(2 wake, 1 launch)", delivery["batch"]["evidence"])

    def test_a_batch_fixed_before_this_comment_arrived_leaves_it_saved(self) -> None:
        """An event appended after a delivery intent is left for the next batch, by contract."""
        earlier = self.audit_events()[0]["event_id"]
        self.delivery_record(stage="awaiting_ack", through_event=earlier, delivery_id="delivery-1")
        self.assertEqual(self.delivery_of(self.comment_id)["state"], DELIVERY_SAVED)

    def test_a_cursor_the_audit_cannot_place_is_unknown_and_never_an_answer(self) -> None:
        for label, record in (
            ("acknowledged", {"acknowledged_through": "evt_gone"}),
            ("active", {"stage": "awaiting_ack", "through_event": "evt_gone", "delivery_id": "d"}),
        ):
            with self.subTest(cursor=label):
                self.delivery_record(**record)
                delivery = self.delivery_of(self.comment_id)
                self.assertEqual(delivery["state"], DELIVERY_UNKNOWN)
                self.assertIn("evt_gone", delivery["reason"])

    def test_the_states_are_the_six_and_none_of_them_is_acceptance(self) -> None:
        """Criterion 5, as a property of the vocabulary rather than of one document.

        Six since secretary-1578: a comment on a sprint that has ended is `not_deliverable`, which
        is neither "no batch carries it yet" nor "nobody could say". None of the six is acceptance.
        """
        self.assertEqual(
            set(DELIVERY_STATES),
            {
                DELIVERY_SAVED,
                DELIVERY_WAITING,
                DELIVERY_HANDED_OVER,
                DELIVERY_ERROR,
                DELIVERY_NOT_DELIVERABLE,
                DELIVERY_UNKNOWN,
            },
        )
        self.delivery_record(acknowledged_through=self.comment_id)
        for document in (
            self.comment(request_id="po-2", body="a second note")["delivery"],
            self.reads().sprint_comment_delivery(self.reference, self.comment_id),
        ):
            with self.subTest(kind=document["kind"]):
                self.assertFalse(document["acceptance"]["established"])
                self.assertIn(ACCEPTANCE_ISSUE, document["acceptance"]["reason"])
                self.assertIn("not acceptance", document["acceptance"]["reason"])

    def test_the_read_delivers_nothing_at_all(self) -> None:
        """Criterion 6: a read of the delivery state is a read, and this is what that means.

        Nothing of the data plane changes -- so no wake, no nudge, no retry, no launch and no write
        to the dispatcher's own state -- and the board sees no write either.
        """
        self.delivery_record(stage="awaiting_ack", through_event=self.comment_id, delivery_id="d")
        before, calls = self.data_plane(), len(self.board.calls)

        self.reads().sprint_comment_delivery(self.reference, self.comment_id)

        self.assertEqual(self.data_plane(), before)
        written = [method for method, _params in self.board.calls[calls:] if method in _WRITE_METHODS]
        self.assertEqual(written, [])

    def test_a_sprint_nobody_holds_and_a_missing_identifier_are_typed_refusals(self) -> None:
        with self.assertRaises(TaskNotFound):
            self.reads().sprint_comment_delivery("sprint:404", self.comment_id)
        with self.assertRaises(ValidationRefused):
            self.reads().sprint_comment_delivery(self.reference, "")


class CommentDeliveryFaultTests(CommentFixture):
    """Criterion 7: the delivery answer with each source it stands on refusing, on both surfaces.

    Every fault is this fixture's own installation. The live installation's sources are never made
    unreadable, so this behaviour is proven here and nowhere else.
    """

    def setUp(self) -> None:
        super().setUp()
        self.comment_id = self.comment()["comment_id"]
        self.delivery_record(acknowledged_through=self.comment_id)

    @contextlib.contextmanager
    def _production_refuses(self) -> Any:
        path = self.production_path()
        kept = path.read_text(encoding="utf-8")
        path.write_text("{", encoding="utf-8")
        try:
            yield
        finally:
            path.write_text(kept, encoding="utf-8")

    @contextlib.contextmanager
    def _journal_refuses(self) -> Any:
        from ummanu.board.sql_audit import SqlTaskAudit

        with mock.patch.object(
            SqlTaskAudit, "events", side_effect=PermissionError("audit journal denied")
        ):
            yield

    def _documents(self, request_id: str) -> list[dict[str, Any]]:
        """The same delivery document as the read answers it and as the operation embeds it."""
        return [
            self.reads().sprint_comment_delivery(self.reference, self.comment_id),
            self.ops().sprint_comment(
                request_id=request_id, actor="operator", reference=self.reference, body=self.BODY
            )["delivery"],
        ]

    def test_an_unreadable_production_state_is_unknown_sourced_from_it(self) -> None:
        with self._production_refuses():
            documents = self._documents("po-comment-1")
        for document in documents:
            with self.subTest(surface=document["kind"]):
                self.assertEqual(document["delivery"]["state"], DELIVERY_UNKNOWN)
                self.assertEqual(document["delivery"]["source"]["name"], "liveness")
                self.assertEqual(document["delivery"]["source"]["state"], "unavailable")
                # The journal is a different source and its answer stands.
                self.assertEqual(document["comment"]["state"], COMMENT_SAVED)
                self.assertEqual(document["comment"]["source"]["state"], "available")

    def test_an_unreadable_audit_is_unknown_sourced_from_the_journal(self) -> None:
        with self._journal_refuses():
            documents = self._documents("po-comment-1")
        for document in documents:
            with self.subTest(surface=document["kind"]):
                self.assertEqual(document["comment"]["state"], COMMENT_UNKNOWN)
                self.assertEqual(document["comment"]["id"], self.comment_id)
                self.assertEqual(document["comment"]["source"]["name"], "journal")
                self.assertEqual(document["delivery"]["state"], DELIVERY_UNKNOWN)
                self.assertEqual(document["delivery"]["source"]["name"], "journal")
                self.assertEqual(document["delivery"]["source"]["state"], "unavailable")

    def test_both_refusing_names_the_journal_and_claims_nothing(self) -> None:
        with self._journal_refuses(), self._production_refuses():
            document = self.reads().sprint_comment_delivery(self.reference, self.comment_id)
        self.assertEqual(document["delivery"]["state"], DELIVERY_UNKNOWN)
        self.assertIsNone(document["delivery"]["batch"])
        self.assertEqual(document["delivery"]["source"]["name"], "journal")
        self.assertFalse(document["acceptance"]["established"])

    def test_a_repeat_still_answers_when_the_audit_read_of_the_document_refuses(self) -> None:
        """The write's own idempotency does not depend on the read that reports it."""
        with self._journal_refuses():
            repeated = self.ops().sprint_comment(
                request_id="po-comment-1", actor="operator", reference=self.reference, body=self.BODY
            )
        self.assertFalse(repeated["saved"])
        self.assertEqual(repeated["comment_id"], self.comment_id)


class CommentCommandTests(CommentFixture):
    """Criterion 8: `ummanu sprint comment` as a client, and the read beside it."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main([*argv, "--instance", str(self.instance), "--data-dir", str(self.data_dir)])
        return code, output.getvalue(), errors.getvalue()

    def _body_file(self, text: str) -> str:
        path = self.tmp / "comment.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_sprint_comment_prints_the_document_the_operation_answered(self) -> None:
        code, output, errors = self._run(
            [
                "sprint", "comment", "--ref", self.reference, "--role", "po", "--actor", "operator",
                "--request-id", "cli-comment", "--body-file", self._body_file(self.BODY),
            ]
        )

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint_comment")
        self.assertTrue(document["saved"])
        self.assertEqual(self.board_comments(), [f"[po]\n{self.BODY}"])
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])

    def test_the_command_holds_no_rule_of_its_own(self) -> None:
        answered = {"kind": "sprint_comment", "comment_id": "evt_1"}
        with mock.patch.object(SprintOperationLayer, "sprint_comment", return_value=answered):
            code, output, _errors = self._run(
                [
                    "sprint", "comment", "--ref", self.reference, "--role", "po",
                    "--body-file", self._body_file("anything"),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), answered)

    def test_a_repeat_over_different_content_is_the_validation_exit_status(self) -> None:
        self._run(
            [
                "sprint", "comment", "--ref", self.reference, "--role", "po", "--actor", "operator",
                "--request-id", "cli-comment", "--body-file", self._body_file(self.BODY),
            ]
        )
        code, output, errors = self._run(
            [
                "sprint", "comment", "--ref", self.reference, "--role", "po", "--actor", "operator",
                "--request-id", "cli-comment", "--body-file", self._body_file("something else"),
            ]
        )
        self.assertEqual(code, _EXIT_BY_CODE["validation"])
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "validation")

    def test_a_closed_sprint_takes_a_comment_through_the_command_too(self) -> None:
        """The outcome added after the fact, from the command a PO actually has.

        This case asserted the opposite until secretary-1578: `sprint comment` answered a closed
        sprint with a conflict, which is what sent a PO past this protocol into the board's own
        `createComment` (issue:9eee1d8ee505bc4ecdc2). The sprint's status is unchanged by it.
        """
        closed = self.add_sprint_row("sprint:9003", status="closed")
        code, output, errors = self._run(
            [
                "sprint", "comment", "--ref", closed, "--role", "po", "--actor", "operator",
                "--request-id", "cli-closed", "--body-file", self._body_file(self.BODY),
            ]
        )
        self.assertEqual(code, 0, errors)
        self.assertTrue(json.loads(output)["saved"])
        self.assertEqual(self.reads().sprint_state(closed)["sprint"]["value"]["status"], "closed")

    def test_sprint_comment_delivery_reads_what_happened_to_it(self) -> None:
        self.delivery_record(acknowledged_through=self.comment()["comment_id"])
        comment_id = self.audit_events()[-1]["event_id"]

        code, output, errors = self._run(
            ["sprint", "comment-delivery", "--ref", self.reference, "--comment-id", comment_id]
        )

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint_comment_delivery")
        self.assertEqual(document["delivery"]["state"], DELIVERY_HANDED_OVER)
        self.assertFalse(document["acceptance"]["established"])

    def test_an_identifier_nobody_holds_still_answers_rather_than_failing(self) -> None:
        code, output, errors = self._run(
            ["sprint", "comment-delivery", "--ref", self.reference, "--comment-id", "evt_nobody"]
        )
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["comment"]["state"], COMMENT_ABSENT)

    def test_a_sprint_nobody_holds_is_the_exit_status_web_read_uses(self) -> None:
        code, _output, errors = self._run(
            ["sprint", "comment-delivery", "--ref", "sprint:404", "--comment-id", "evt_x"]
        )
        self.assertEqual(code, _EXIT_BY_CODE["not_found"])
        self.assertEqual(json.loads(errors)["error"]["code"], "not_found")


class SectionSeamTests(SprintProtocolFixture):
    """The enforcement point itself: what makes the invariant hold for a section written tomorrow.

    Four sites of one document broke the same rule in four different ways across two review rounds,
    each repaired where it was found. What is checked here is the place that makes the fifth one
    hard to write: every section of both documents is a guarded builder of `SprintSections`, a rule
    is not run when a source it needs refused, a refusal cannot answer, and a section assembled
    outside the seam cannot reach a document at all.
    """

    def _paths(self, document: dict, prefix: str = "") -> set[str]:
        """Every place in a document that carries a source, by path."""
        found: set[str] = set()
        for key, value in document.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                if isinstance(value.get("source"), dict):
                    found.add(path)
                found |= self._paths(value, f"{path}.")
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        found |= {
                            entry.replace(f"{path}.0.", f"{path}[].") for entry in
                            self._paths(item, f"{path}.0.")
                        }
        return found

    def test_every_section_of_both_documents_is_a_guarded_builder(self) -> None:
        for name in section_module.sections(sprint_reads_module.SprintSections):
            with self.subTest(section=name):
                built = getattr(sprint_reads_module.SprintSections, name)
                self.assertTrue(getattr(built, section_module.GUARDED, False))

    def test_a_builder_that_answers_with_anything_but_a_section_is_caught(self) -> None:
        """The half of the seam that covers a section somebody writes by hand next month."""

        class Rogue(section_module.SectionSet):
            def loose(self, read: Any) -> Any:
                return {"source": sources.available(0.0).to_json(), "state": "green"}

        with self.assertRaises(section_module.SectionContractError):
            Rogue().loose(None)

    def test_a_hand_built_section_cannot_reach_a_document(self) -> None:
        """And the other half: `render` is the only way a source gets into a document."""
        hand_built = {"work": {"checks": {"source": sources.available(0.0).to_json(), "state": "green"}}}
        with self.assertRaises(section_module.SectionContractError):
            section_module.render(hand_built)
        # A mapping that merely has a field called `source` is data, and is left alone.
        self.assertEqual(
            section_module.render({"event": {"source": "the observer"}}), {"event": {"source": "the observer"}}
        )

    def test_a_builder_that_assembles_its_own_section_is_caught(self) -> None:
        """The reviewer's reproduction of the round that shipped this seam, as a case.

        Being a `Section` used to be the credential, so a builder that constructed one directly --
        the extension shape this module documents, a new public method of the set -- was accepted by
        the wrapper and by `render` alike, and published `state: working` under an *unavailable*
        liveness source with no `SectionContractError`. Both places now ask where the section came
        from, and this case fails if either check is removed.
        """

        class Rogue(section_module.SectionSet):
            def lie(self, read: Any) -> Any:
                return section_module.Section(
                    read.source("liveness"), {"state": "working"}, "liveness"
                )

        read = section_module.SourceSet(
            [
                section_module.Reading(
                    "liveness", sources.unavailable("production state denied", now=0.0)
                )
            ]
        )
        with self.assertRaises(section_module.SectionContractError):
            Rogue().lie(read)

        # And the same object cannot reach a document by any other route either.
        forged = section_module.Section(read.source("liveness"), {"state": "working"}, "liveness")
        self.assertFalse(forged.trusted)
        with self.assertRaises(section_module.SectionContractError) as refused:
            section_module.render({"work": {"waiting": forged}})
        self.assertIn("liveness", str(refused.exception))

        # What is refused is the provenance and not the source or the shape: a decided section over
        # the same refused source publishes, and says `unknown` rather than `working`.
        decided = read.decide(
            section_module.rule("liveness", lambda _value: {"state": "working"}),
            blank={"state": "unknown", "reason": None},
        )
        self.assertTrue(decided.trusted)
        published = section_module.render({"work": {"waiting": decided}})
        self.assertEqual(published["work"]["waiting"]["state"], "unknown")
        self.assertEqual(published["work"]["waiting"]["source"]["name"], "liveness")

    def test_the_no_claim_mark_is_decided_here_too(self) -> None:
        """The document-level source marks go through the same factory, so they publish."""
        read = section_module.SourceSet(
            [section_module.Reading("journal", sources.available(0.0), [])]
        )
        self.assertTrue(read.mark("journal").trusted)

    def test_a_rule_never_runs_when_a_source_it_needs_refused(self) -> None:
        """Not a check a branch remembers: the code that would have claimed is not executed."""
        ran: list[str] = []
        read = section_module.SourceSet(
            [
                section_module.Reading("first", sources.unavailable("gone", now=0.0)),
                section_module.Reading("second", sources.available(0.0), {"card": "x"}),
            ]
        )
        decided = read.decide(
            section_module.rule("first", lambda _value: ran.append("first") or {"state": "working"}),
            section_module.Rule(
                "second", ("first", "second"), lambda *_v: ran.append("both") or {"state": "working"}
            ),
            blank={"state": "unknown"},
            narrates=(),
        )
        self.assertEqual(ran, [], "a rule needing a refused source was executed")
        self.assertEqual(decided.name, "first")
        self.assertEqual(decided.fields["state"], "unknown")
        # And its payload is not reachable at all, so it cannot be read by accident either.
        with self.assertRaises(section_module.SectionContractError):
            read.value("first")

    def test_a_section_that_claims_under_a_refusal_is_caught(self) -> None:
        """Criterion 1's pin: a section that tries to violate the invariant fails here."""
        read = section_module.SourceSet(
            [section_module.Reading("liveness", sources.unavailable("unreadable", now=0.0))]
        )
        with self.assertRaises(section_module.SectionContractError):
            read.decide(
                section_module.rule("liveness", lambda _value: {"state": "working"}),
                blank={"state": "unknown", "reason": None},
                unresolved=lambda _reading: {"state": "working", "reason": "the head is up"},
            )

    def test_a_section_that_cannot_answer_when_everything_answered_is_a_hole(self) -> None:
        read = section_module.SourceSet([section_module.Reading("sprints", sources.available(0.0), {})])
        with self.assertRaises(section_module.SectionContractError):
            read.decide(
                section_module.rule("sprints", lambda _value: None),
                blank={"state": "unknown"},
                narrates=(),
            )

    def test_every_section_a_document_carries_is_one_the_seam_decided(self) -> None:
        """The exhaustive walk of both documents, as a set rather than as a survey.

        A section added to either document without going through `SprintSections` fails here, and so
        does one silently removed: the paths are the contract `docs/PROTOCOLS.md` publishes.
        """
        reference = self.reference_of(self.create())
        listing = self.reads().sprint_list()
        watched = self.reads().sprint_state(reference)
        work = {
            "current_task",
            "current_card_state",
            "decision",
            "decision.freshness",
            "cards",
            "degraded_cards",
            "checks",
            "waiting",
            "head_profiles",
        }
        marks = {"cards", "journal", "liveness", "installation"}
        observer = {"observer.declared", "observer.launch"}
        self.assertEqual(
            self._paths(listing),
            marks
            | {"sprints"}
            | {f"sprints.items[].{name}" for name in work | observer},
        )
        self.assertEqual(self._paths(watched), marks | {"sprint"} | observer | {f"work.{name}" for name in work})
        # The delivery document is walked the same way, and for the same reason: its two sections are
        # a section each, and `acceptance` is deliberately not one -- it is read from no source.
        commented = self.ops().sprint_comment(
            request_id="seam-1", actor="operator", reference=reference, body="a note"
        )
        delivery = commented["delivery"]
        self.assertEqual(self._paths(delivery), marks | {"comment", "delivery"})
        self.assertNotIn("acceptance", self._paths(delivery))
        # And the close result, whose `definition_of_done` is deliberately not a section either: it
        # is this product saying what a close is not, read from no source at all.
        closed = self.reads().sprint_close_result(reference, "evt_nobody")
        self.assertEqual(self._paths(closed), marks | {"close", "reservations", "sprint"})
        self.assertNotIn("definition_of_done", self._paths(closed))
        # And every one of them names the source that answered it.
        for document in (listing, watched, delivery):
            for path in self._paths(document):
                with self.subTest(kind=document["kind"], section=path):
                    self.assertTrue(_source_at(document, path)["name"])


def _source_at(document: dict, path: str) -> dict:
    """The `source` of one section of a document, by the path `SectionSeamTests` walks."""
    node: Any = document
    for step in path.split("."):
        if step.endswith("[]"):
            node = node[step[:-2]][0]
        else:
            node = node[step]
    return node["source"]


class SourceIsolationMatrixTests(SprintWorkFixture):
    """Every source refusing alone and in combination, per section, over both operations.

    Criterion 6 of secretary-1574, as a table rather than as prose: for each fault the whole work
    document is asserted at once -- what each section says *and* which source it names -- so a
    repair that fixes one section by taking an answer away from another fails here. Both operations
    are asserted every time, because they share the assembly and a repair that reached only one of
    them would be no repair at all.

    Every fault is injected into this fixture's own installation. The live installation's sources
    are never made unreadable, which is why source-isolation fault behaviour is proven here and not
    against the real board.
    """

    #: Which section of the work document is asserted, and the field that carries its claim.
    CLAIMS: ClassVar[dict[str, str]] = {
        "current_task": "ref",
        "current_card_state": "transition",
        "decision": "entry",
        "cards": "states",
        "degraded_cards": "items",
        "checks": "state",
        "waiting": "state",
    }

    def setUp(self) -> None:
        super().setUp()
        self.reference = self.reference_of(self.create())
        self.card = self._card(self.reference)
        self._current_task(self.reference, self.card)
        # In progress: the one column the board deliberately does not settle, so every source in the
        # chain has something only it can say about this sprint.
        self._move(self.card, "In progress")
        self._production({}, {self.card: {"state": "claimed"}})

    # -- the faults, each hermetic and each only this fixture's ------------------------------

    @contextlib.contextmanager
    def _sprint_board_refuses(self) -> Any:
        original = self.board.call
        board = _board_id(self.board, SPRINT_BOARD_NAME)

        def refuse(method: str, **params: Any) -> Any:
            if method == "getAllTasks" and params.get("project_id") == board:
                raise TaskError("backend_error", "the sprint board is unavailable", 1)
            return original(method, **params)

        self.board.call = refuse  # type: ignore[method-assign]
        try:
            yield
        finally:
            self.board.call = original  # type: ignore[method-assign]

    @contextlib.contextmanager
    def _pipeline_refuses(self) -> Any:
        with _board_missing(self.board, "Pipeline"):
            yield

    @contextlib.contextmanager
    def _journal_refuses(self) -> Any:

        with mock.patch.object(type(task_audit_for(self.board)), "events", side_effect=PermissionError("audit journal denied")):
            yield

    @contextlib.contextmanager
    def _production_refuses(self) -> Any:
        path = self.data_dir / "dispatcher" / "production-state.json"
        kept = path.read_text(encoding="utf-8")
        path.write_text("{", encoding="utf-8")
        try:
            yield
        finally:
            path.write_text(kept, encoding="utf-8")

    @contextlib.contextmanager
    def _config_refuses(self) -> Any:
        """A schema-invalid `instance.yaml`, with the explicit data directory this layer was given."""
        path = self.instance / "instance.yaml"
        kept = path.read_text(encoding="utf-8")
        path.write_text(f"version: 1\nname: test\ndata_dir: {self.data_dir}\n", encoding="utf-8")
        try:
            yield
        finally:
            path.write_text(kept, encoding="utf-8")

    def _faults(self, *names: str) -> Any:
        stack = contextlib.ExitStack()
        for name in names:
            stack.enter_context(getattr(self, f"_{name}_refuses")())
        return stack

    # -- what the document says, in one shape -------------------------------------------------

    def _entry_of(self, document: dict[str, Any], kind: str) -> dict[str, Any] | None:
        """This sprint's sections, or `None` when the board that would list it did not answer."""
        if kind == "sprint_state":
            return {**document["work"], "observer": document["observer"]}
        items = document["sprints"]["items"]
        return None if items is None else next(
            (item for item in items if item["ref"] == self.reference), None
        )

    def _seen(self, kind: str) -> dict[str, Any]:
        """Every asserted section of one operation's document: its source, and what it claims.

        The document-level marks are keyed `source:<name>` so that they cannot collide with the work
        section of the same name -- `cards` is both a source of this document and a section of it,
        and the whole point of the table is that the two are asserted apart.
        """
        layer = self.reads()
        document = layer.sprint_state(self.reference) if kind == "sprint_state" else layer.sprint_list()
        seen: dict[str, Any] = {
            f"source:{key}": (document[key]["source"]["name"], document[key]["source"]["state"])
            for key in ("cards", "journal", "liveness", "installation")
        }
        entry = self._entry_of(document, kind)
        if entry is None:
            listed = document["sprints"]
            seen["sprints"] = (listed["source"]["name"], listed["source"]["state"], listed["items"])
            return seen
        for section, claim in self.CLAIMS.items():
            source = entry[section]["source"]
            seen[section] = (source["name"], source["state"], entry[section][claim])
        freshness = entry["decision"]["freshness"]
        seen["decision.freshness"] = (
            freshness["source"]["name"],
            freshness["source"]["state"],
            freshness["value"] is not None,
        )
        for half in ("declared", "launch"):
            said = entry["observer"][half]
            seen[f"observer.{half}"] = (said["source"]["name"], said["source"]["state"], said["state"])
        return seen

    def _assert_sections(self, faults: tuple[str, ...], expected: dict[str, Any]) -> None:
        """The same table over both operations, since they share the assembly."""
        for kind in ("sprint_list", "sprint_state"):
            with self.subTest(faults=faults or ("none",), operation=kind):
                with self._faults(*faults):
                    seen = self._seen(kind)
                self.assertEqual(seen, expected)

    def _sections(self, **overrides: Any) -> dict[str, Any]:
        """The healthy answer, with the sections a fault changes named explicitly by each case."""
        healthy: dict[str, Any] = {
            "source:cards": ("cards", "available"),
            "source:journal": ("journal", "available"),
            "source:liveness": ("liveness", "available"),
            "source:installation": ("installation", "available"),
            "current_task": ("sprints", "available", self.card),
            # The fixture's card was put in its column on the board directly, which is a state this
            # installation really has: the journal holds no transition for it, and that is the
            # journal's own answer rather than a zero age.
            "current_card_state": ("journal", "available", sprint_reads_module.TRANSITION_ABSENT),
            "decision": ("sprints", "available", None),
            "decision.freshness": ("journal", "available", True),
            "cards": ("cards", "available", {"in_progress": [self.card]}),
            "degraded_cards": ("liveness", "available", {}),
            "checks": ("liveness", "available", sprint_reads_module.CHECKS_NOT_GREEN),
            "waiting": ("liveness", "available", sprint_reads_module.WAITING_WORKING),
            "observer.declared": ("sprints", "available", sprint_reads_module.OBSERVER_DECLARED),
            "observer.launch": ("liveness", "available", OBSERVER_NOT_STARTED),
        }
        return {**healthy, **overrides}

    def test_nothing_refuses(self) -> None:
        """The control: with every source in hand, each section names the one that answered it."""
        self._assert_sections((), self._sections())

    def test_the_audit_journal_alone_refuses(self) -> None:
        """Site 3: the sprint row, the current card and the observer all stand."""
        self._assert_sections(
            ("journal",),
            self._sections(
                **{
                    "source:journal": ("journal", "unavailable"),
                    "current_card_state": (
                        "journal",
                        "unavailable",
                        sprint_reads_module.TRANSITION_UNKNOWN,
                    ),
                    "decision.freshness": ("journal", "unavailable", False),
                },
            ),
        )

    def test_the_pipeline_alone_refuses(self) -> None:
        self._assert_sections(
            ("pipeline",),
            self._sections(
                cards=("cards", "unavailable", None),
                **{
                    "source:cards": ("cards", "unavailable"),
                    "current_card_state": (
                        "cards",
                        "unavailable",
                        sprint_reads_module.TRANSITION_UNKNOWN,
                    ),
                    "decision.freshness": ("cards", "unavailable", False),
                },
            ),
        )

    def test_the_production_state_alone_refuses(self) -> None:
        self._assert_sections(
            ("production",),
            self._sections(
                degraded_cards=("liveness", "unavailable", None),
                checks=("liveness", "unavailable", sprint_reads_module.CHECKS_UNKNOWN),
                waiting=("liveness", "unavailable", sprint_reads_module.WAITING_UNKNOWN),
                **{
                    "source:liveness": ("liveness", "unavailable"),
                    "observer.launch": ("liveness", "unavailable", OBSERVER_UNAVAILABLE),
                },
            ),
        )

    def test_the_installation_config_alone_refuses(self) -> None:
        """Criterion 4: an unvalidated config takes away nothing the board can still answer."""
        self._assert_sections(
            ("config",), self._sections(**{"source:installation": ("installation", "unavailable")})
        )

    def test_the_journal_and_the_production_state_refuse_together(self) -> None:
        self._assert_sections(
            ("journal", "production"),
            self._sections(
                degraded_cards=("liveness", "unavailable", None),
                checks=("liveness", "unavailable", sprint_reads_module.CHECKS_UNKNOWN),
                waiting=("liveness", "unavailable", sprint_reads_module.WAITING_UNKNOWN),
                **{
                    "source:journal": ("journal", "unavailable"),
                    "source:liveness": ("liveness", "unavailable"),
                    "current_card_state": (
                        "journal",
                        "unavailable",
                        sprint_reads_module.TRANSITION_UNKNOWN,
                    ),
                    "decision.freshness": ("journal", "unavailable", False),
                    "observer.launch": ("liveness", "unavailable", OBSERVER_UNAVAILABLE),
                },
            ),
        )

    def test_the_pipeline_and_the_production_state_refuse_together(self) -> None:
        """With both of a section's sources gone it names the first one the chain needed."""
        self._assert_sections(
            ("pipeline", "production"),
            self._sections(
                cards=("cards", "unavailable", None),
                degraded_cards=("liveness", "unavailable", None),
                checks=("liveness", "unavailable", sprint_reads_module.CHECKS_UNKNOWN),
                waiting=("cards", "unavailable", sprint_reads_module.WAITING_UNKNOWN),
                **{
                    "source:cards": ("cards", "unavailable"),
                    "source:liveness": ("liveness", "unavailable"),
                    "current_card_state": (
                        "cards",
                        "unavailable",
                        sprint_reads_module.TRANSITION_UNKNOWN,
                    ),
                    "decision.freshness": ("cards", "unavailable", False),
                    "observer.launch": ("liveness", "unavailable", OBSERVER_UNAVAILABLE),
                },
            ),
        )

    def test_the_sprint_board_alone_refuses(self) -> None:
        """Site 4: with no sprint row, the observer claims nothing -- and neither does anything else.

        The production state is readable here and holds no observer for this reference. That proves
        only that it holds no row: `absent` and `not_started` would both be affirmative claims about
        a sprint nobody has seen.
        """
        refused = ("sprints", "unavailable")
        marks = {
            "source:cards": ("cards", "available"),
            "source:journal": ("journal", "available"),
            "source:liveness": ("liveness", "available"),
            "source:installation": ("installation", "available"),
        }
        with self._faults("sprint_board"):
            listed = self._seen("sprint_list")
            watched = self._seen("sprint_state")
        # The listing has no item to carry sections: `null` items, never an empty listing.
        self.assertEqual(listed, {**marks, "sprints": (*refused, None)})
        self.assertEqual(
            watched,
            {
                **marks,
                "current_task": (*refused, None),
                "current_card_state": (*refused, sprint_reads_module.TRANSITION_UNKNOWN),
                "decision": (*refused, None),
                "decision.freshness": (*refused, False),
                "cards": (*refused, None),
                "degraded_cards": (*refused, None),
                "checks": (*refused, sprint_reads_module.CHECKS_UNKNOWN),
                "waiting": (*refused, sprint_reads_module.WAITING_UNKNOWN),
                "observer.declared": (*refused, sprint_reads_module.OBSERVER_UNKNOWN),
                "observer.launch": (*refused, OBSERVER_UNAVAILABLE),
            },
        )

    def test_every_source_refuses_at_once(self) -> None:
        with self._faults("sprint_board", "pipeline", "journal", "production", "config"):
            listing = self.reads().sprint_list()
            watched = self.reads().sprint_state(self.reference)
        self.assertIsNone(listing["sprints"]["items"])
        for name in ("cards", "journal", "liveness", "installation"):
            with self.subTest(source=name):
                for document in (listing, watched):
                    self.assertEqual(document[name]["source"]["state"], "unavailable")
                    self.assertEqual(document[name]["source"]["name"], name)
        self.assertIsNone(watched["sprint"]["value"])
        self.assertEqual(watched["sprint"]["source"]["name"], "sprints")
        self.assertEqual(watched["observer"]["declared"]["state"], sprint_reads_module.OBSERVER_UNKNOWN)


class CloseFixture(SprintProtocolFixture):
    """One sprint that does not close tidily, and the pieces the close suites drive.

    Deliberately not a tidy sprint, because sprint:1431 is not one: two declared issues decided
    differently, a card that reached Done and a card that never will, and a closeout that says the
    findings are deferred rather than fixed. A fixture with nothing left over would pass with an
    operation that assumed a sprint closes with no remainder.
    """

    REASON = "the goal is reached far enough to cut the next sprint, and the rest is deferred"
    #: It writes cards, so its board is a real card store.

    def setUp(self) -> None:
        super().setUp()
        init_state_repo(self.instance)
        self.board.add_record(
            "issue:second",
            "Second issue",
            {
                "record_type": "issue",
                "issue_product": "ummanu",
                "issue_kind": "bug",
                "issue_priority": "P1",
            },
        )
        self.tasks = TaskWriter(self.board, data_dir=self.data_dir)
        self.reference = self.reference_of(self.create(issues=["issue:open", "issue:second"]))
        # The cards below are written as this sprint's observer head, bound to it exactly as the
        # dispatcher binds a head it launches.
        bind_observer(self, self.reference)
        self.done = self._card("landed in this sprint", "card-done")
        self._take_to_done(self.done)
        self.left = self._card("superseded by the next cut", "card-left")

    # -- the sprint's shape --------------------------------------------------------------------

    def _card(self, title: str, request_id: str) -> str:
        return self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title=title,
            target="ready",
            sprint=self.reference,
            request_id=request_id,
        )["task"]["ref"]

    def _take_to_done(self, reference: str) -> None:
        self.tasks.claim(
            role="dispatcher",
            actor="dispatcher",
            reference=reference,
            worker="worker",
            request_id=f"claim-{reference}",
        )
        for target in ("validate", "done"):
            self.tasks.move(
                role="dispatcher",
                actor="dispatcher",
                reference=reference,
                target=target,
                reason="",
                request_id=f"move-{reference}-{target}",
            )

    # -- the operation -------------------------------------------------------------------------

    def decisions(self, **overrides: Any) -> dict[str, list[dict[str, str]]]:
        decided: dict[str, list[dict[str, str]]] = {
            "issues": [
                {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed on this card"},
                {
                    "ref": "issue:second",
                    "verdict": "open",
                    "reason": "the sprint ran out before this one was reached",
                },
            ],
            "cards": [
                {
                    "ref": self.left,
                    "verdict": "drop",
                    "reason": "superseded by the next sprint's cut",
                }
            ],
        }
        decided.update(overrides)
        return decided

    def close(self, **kwargs: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "request_id": "close-1",
            "actor": "operator",
            "reference": self.reference,
            "reason": self.REASON,
            "closeout": CLOSEOUT_BODY,
            "decisions": self.decisions(),
            "role": "po",
        }
        request.update(kwargs)
        return self.ops().sprint_close(**request)

    # -- what it left behind -------------------------------------------------------------------

    def knowledge(self) -> tuple[str, ...]:
        return list_knowledge_documents(self.instance)

    def closeout_text(self, document: str) -> str:
        return (self.instance / "state" / "knowledge" / document).read_text(encoding="utf-8")

    def knowledge_writes(self) -> dict[str, tuple[bytes, int, int]]:
        """Every knowledge file with its bytes, inode and mtime: a write replaces the file, a no-op keeps it.

        The knowledge writer makes no Git commit (docs/RECOVERY.md, "Writers"), so "wrote nothing
        new" is read from the files themselves.
        """
        root = self.instance / "state" / "knowledge"
        found: dict[str, tuple[bytes, int, int]] = {}
        for path in sorted(root.rglob("*")) if root.is_dir() else ():
            if path.is_file():
                info = path.stat()
                found[path.relative_to(root).as_posix()] = (path.read_bytes(), info.st_ino, info.st_mtime_ns)
        return found

    def status_of(self, reference: str = "") -> str:
        value = self.reads().sprint_state(reference or self.reference)["sprint"]["value"]
        return str(value["status"])

    def production_bytes(self) -> bytes:
        return (self.data_dir / "dispatcher" / "production-state.json").read_bytes()

    def observer_record(self) -> None:
        """The observer record the tick would have written for this sprint, so a stop is visible."""
        self._production(
            {
                self.reference: {
                    "sprint": self.reference,
                    "head": OBSERVER_PROFILE,
                    "state": "working",
                    "launches": 1,
                    "bound": True,
                }
            }
        )


class CloseOperationTests(CloseFixture):
    """Criterion 1: a named operation closes a sprint and answers with what became of the work."""

    def test_the_operation_answers_with_every_decision_the_close_made(self) -> None:
        answered = self.close()

        self.assertEqual(answered["kind"], "sprint_closed")
        self.assertEqual(answered["ref"], self.reference)
        self.assertEqual(validate(answered, "web-sprint", answered["kind"]), [])
        closed = answered["result"]["close"]
        self.assertEqual(closed["state"], sprint_reads_module.CLOSE_RECORDED)
        self.assertEqual(closed["source"]["name"], "journal")
        self.assertEqual(closed["closed_by"], "operator")
        self.assertEqual(closed["closing_reason"], self.REASON)
        self.assertEqual(
            [(entry["ref"], entry["verdict"]) for entry in closed["issue_decisions"]],
            [("issue:open", "resolved"), ("issue:second", "open")],
        )
        self.assertEqual(closed["closed_issues"], ["issue:open"])
        self.assertEqual(
            [(entry["ref"], entry["verdict"]) for entry in closed["card_dispositions"]],
            [(self.left, "drop")],
        )
        self.assertEqual(closed["archived_tasks"], [self.done])
        self.assertEqual(closed["disposed_tasks"], [self.left])
        self.assertEqual(answered["result"]["sprint"]["value"]["status"], "closed")

    def test_the_result_names_the_reservations_the_close_released(self) -> None:
        answered = self.close()

        released = answered["result"]["reservations"]
        self.assertEqual(released["source"]["name"], "reservations")
        self.assertEqual(released["declared"], ["ummanu"])
        self.assertEqual(released["released"], ["ummanu"])
        self.assertEqual(released["held"], [])

    def test_the_close_is_not_a_completed_definition_of_done(self) -> None:
        """Criterion 3, on the answer: no field, name or sentence lets this read as a satisfied contract."""
        answered = self.close()

        for document in (answered, answered["result"]):
            with self.subTest(kind=document["kind"]):
                self.assertFalse(document["definition_of_done"]["satisfied"])
                self.assertEqual(document["definition_of_done"]["reason"], CLOSE_NOT_DONE)
        self.assertIn("not a statement", CLOSE_NOT_DONE)

    def test_the_result_is_read_back_afterwards_through_the_protocol(self) -> None:
        answered = self.close()

        later = self.reads().sprint_close_result(self.reference, answered["event_id"])

        self.assertEqual(later["kind"], "sprint_close_result")
        self.assertEqual(validate(later, "web-sprint", later["kind"]), [])
        self.assertEqual(later["close"], answered["result"]["close"])

    def test_the_operation_re_decides_nothing_and_the_writer_keeps_every_rule(self) -> None:
        """A close short of a decision is refused by the writer, before anything at all is written."""
        with self.assertRaises(ValidationRefused) as refused:
            self.close(decisions={"issues": [], "cards": []})

        self.assertIn("issue:open", refused.exception.message)
        self.assertEqual(self.status_of(), "open")
        self.assertEqual(self.knowledge(), ())

    def test_a_close_states_its_reason_and_its_closeout_or_it_is_refused(self) -> None:
        for label, request in (("reason", {"reason": "  "}), ("closeout", {"closeout": ""})):
            with self.subTest(missing=label), self.assertRaises(ValidationRefused):
                self.close(**request)
        self.assertEqual(self.status_of(), "open")
        self.assertEqual(self.knowledge(), ())

    def test_a_close_of_a_sprint_nobody_holds_is_refused(self) -> None:
        with self.assertRaises(ReadError) as refused:
            self.close(reference="sprint:9999", request_id="close-missing")
        self.assertEqual(refused.exception.code, "not_found")

    def test_a_repeat_of_the_same_request_closes_nothing_a_second_time(self) -> None:
        first = self.close()
        documents, writes = self.knowledge(), self.knowledge_writes()
        events = [event["event_id"] for event in self.audit_events()]

        repeated = self.close()

        self.assertEqual(repeated["event_id"], first["event_id"])
        self.assertEqual(self.knowledge(), documents)
        self.assertEqual(self.knowledge_writes(), writes)
        self.assertEqual([event["event_id"] for event in self.audit_events()], events)

    def test_a_repeat_that_states_another_closeout_is_refused(self) -> None:
        """Compared exactly, and never by containment.

        The shortened body is the case that matters: it is a *substring* of the prose this close was
        staged with, so a containment test accepted it and answered the caller with the completed
        close -- telling them a close succeeded with an account no document ever carried. Ordinary
        editing during a retry is enough to produce it, which is why all three shapes are pinned.
        """
        self.close()
        writes = self.knowledge_writes()
        first_sentence = CLOSEOUT_BODY.split(".")[0] + "."
        self.assertIn(first_sentence, CLOSEOUT_BODY)
        for label, body in (
            ("another account entirely", "actually the sprint achieved everything"),
            ("a shortened body", first_sentence),
            ("an extended body", CLOSEOUT_BODY + "\nAnd one more paragraph nobody staged.\n"),
        ):
            with self.subTest(closeout=label):
                with self.assertRaises(ValidationRefused) as refused:
                    self.close(closeout=body)
                self.assertIn("staged with another closeout", refused.exception.message)
        # The body it was staged with still answers from the record and writes nothing new.
        self.assertTrue(self.close()["result"]["close"]["closeout"]["written"])
        self.assertEqual(len(self.knowledge()), 1)
        self.assertEqual(self.knowledge_writes(), writes)

    def audit_events(self) -> list[dict[str, Any]]:

        return task_audit_for(self.board).events()


class CloseoutTests(CloseFixture):
    """Criterion 2: the closeout is a step of the close, written through the one writer, exactly once."""

    def test_the_closeout_is_written_by_the_close_and_names_the_sprint(self) -> None:
        answered = self.close()

        written = answered["result"]["close"]["closeout"]
        self.assertEqual(self.knowledge(), (written["document"],))
        self.assertTrue(written["written"])
        # The knowledge writer's content revision of the document, where a Git commit id used to be.
        text = self.closeout_text(written["document"])
        self.assertEqual(
            written["commit"],
            content_revision({written["document"]: hashlib.sha256(text.encode("utf-8")).hexdigest()}),
        )
        self.assertIn(self.reference, text)
        # The outcome the caller stated, verbatim: the operation writes and links what it is given.
        self.assertIn(CLOSEOUT_BODY.strip(), text)
        # What is left unfinished, and the owner's decision about it.
        self.assertIn(self.left, text)
        self.assertIn("superseded by the next sprint's cut", text)
        self.assertIn("issue:second", text)
        self.assertIn(self.REASON, text)
        # Criterion 3, in the document itself.
        self.assertIn(CLOSE_NOT_DONE, text)

    def test_the_closeout_step_carries_an_id_derived_from_the_close_request(self) -> None:
        answered = self.close()

        step = _close_step_request_id("close-1", "closeout", self.reference)
        committed = task_audit_for(self.board).committed_event(step)
        self.assertIsNotNone(committed)
        self.assertEqual(committed["kind"], SPRINT_CLOSEOUT)
        self.assertEqual(committed["ref"], self.reference)
        self.assertEqual(
            committed["payload"]["document"], answered["result"]["close"]["closeout"]["document"]
        )
        self.assertEqual(committed["payload"]["close_request_id"], "close-1")

    def test_a_closeout_this_installation_cannot_write_is_refused_before_anything_is(self) -> None:
        """The preflight: a knowledge path the writer cannot write refuses, and nothing is written.

        No Git is needed any more, so the unwritable case is `state/knowledge` being a file.
        """
        (self.instance / "state").mkdir(exist_ok=True)
        (self.instance / "state" / "knowledge").write_text("not a directory\n", encoding="utf-8")
        before = self.board.card_count()

        with self.assertRaises(ValidationRefused) as refused:
            self.close()

        self.assertIn("closeout", refused.exception.message)
        self.assertEqual(self.status_of(), "open")
        self.assertEqual(self.board.card_count(), before)


class ClosedSprintObserverTests(CloseFixture):
    """Criterion 5: the close releases the reservations and stops no head itself."""

    def test_the_close_touches_no_dispatcher_state_and_stops_no_head(self) -> None:
        self.observer_record()
        production = self.production_bytes()

        self.close()

        # The observer of a closed sprint is ended by the production tick reconciling against the
        # sprint board -- `ummanu.dispatch.observer`, "closed or gone sprint -> stop the head
        # and drop the record". The close adds no second teardown, so the dispatcher's own state is
        # byte for byte what it was: no stop, no launch, no cursor moved.
        self.assertEqual(self.production_bytes(), production)

    def test_the_reservations_are_released_by_the_close_itself(self) -> None:
        from ummanu.sprints import active_sprint_projects

        self.assertEqual(active_sprint_projects(self.data_dir), {"ummanu": [self.reference]})

        self.close()

        self.assertEqual(active_sprint_projects(self.data_dir), {})


class PostCloseCommentTests(CloseFixture):
    """Criterion 4: a PO adds the outcome after the fact, and nothing else moves."""

    BODY = "PO: the deferred findings are on the next sprint's cut, not lost"

    def setUp(self) -> None:
        super().setUp()
        self.closed = self.close()

    def comment(self, **kwargs: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "request_id": "po-after-close",
            "actor": "operator",
            "reference": self.reference,
            "body": self.BODY,
            "role": "po",
        }
        request.update(kwargs)
        return self.ops().sprint_comment(**request)

    def test_a_comment_on_a_closed_sprint_is_accepted_and_audited(self) -> None:
        answered = self.comment()

        self.assertTrue(answered["saved"])
        self.assertEqual(validate(answered, "web-sprint", answered["kind"]), [])
        committed = task_audit_for(self.board).committed_event("po-after-close")
        self.assertEqual(committed["event_id"], answered["comment_id"])
        self.assertEqual(committed["kind"], "commented")

    def test_it_changes_nothing_about_the_sprint(self) -> None:
        from ummanu.sprints import active_sprint_projects

        self.comment()

        self.assertEqual(self.status_of(), "closed")
        self.assertEqual(active_sprint_projects(self.data_dir), {})

    def test_it_wakes_no_head_and_launches_none(self) -> None:
        """Asserted separately, as secretary-1575 asserted its no-second-wake.

        A wake, a launch, a deferral or a moved cursor all write to the dispatcher's production
        state, so the file is compared byte for byte. The observer record is the one the tick would
        have written for this sprint, so "unchanged" is not "there was nothing to change".
        """
        self.observer_record()
        production = self.production_bytes()

        self.comment()

        self.assertEqual(self.production_bytes(), production)

    def test_the_delivery_read_says_no_batch_will_ever_carry_it(self) -> None:
        answered = self.comment()

        delivery = answered["delivery"]["delivery"]
        self.assertEqual(delivery["state"], DELIVERY_NOT_DELIVERABLE)
        self.assertEqual(delivery["source"]["name"], "sprints")
        self.assertIn("closed", delivery["reason"])
        self.assertIsNone(delivery["batch"])
        # And the comment itself is saved: the two facts are separate and both are answered.
        self.assertEqual(answered["delivery"]["comment"]["state"], COMMENT_SAVED)

    def test_a_repeat_is_idempotent_on_the_request_id(self) -> None:
        first = self.comment()
        events = [event["event_id"] for event in task_audit_for(self.board).events()]

        repeated = self.comment()

        self.assertFalse(repeated["saved"])
        self.assertEqual(repeated["comment_id"], first["comment_id"])
        self.assertEqual([event["event_id"] for event in task_audit_for(self.board).events()], events)

    def test_a_repeat_over_different_content_is_refused(self) -> None:
        self.comment()
        with self.assertRaises(ValidationRefused):
            self.comment(body="PO: something else entirely")

    def test_a_stopped_sprint_takes_one_too(self) -> None:
        stopped = self.add_sprint_row("sprint:9100", status="stopped")
        answered = self.comment(reference=stopped, request_id="po-after-stop")
        self.assertTrue(answered["saved"])
        self.assertEqual(answered["delivery"]["delivery"]["state"], DELIVERY_NOT_DELIVERABLE)


class TerminalSprintWriteTests(SprintProtocolFixture):
    """The documented terminal-sprint table, held to what `SprintWriter._write` actually does.

    The first version of this pin carried a hand-written list of three kinds and a document that
    claimed to describe the writer "in full". It was wrong -- `budget_recorded` and `restored` are
    accepted on a sprint that has ended -- and, being its own authority on what to check, it could
    not notice. So the set is not written here. It is read out of `SprintWriter`'s own calls to
    `_write`, which is where the kinds actually are, and a write added to the writer tomorrow either
    appears in the published table or fails `test_the_document_names_every_write_the_writer_makes`.

    Each kind is driven through `_write` itself rather than through its public caller, which could
    refuse first for a reason of its own -- a role, a payload, an observer identity -- and hide what
    the terminal guard would have done.
    """

    #: The kinds `SprintWriter` passes to `_write`, taken from the writer's own source. A call whose
    #: kind is not a literal would leave a kind untested without saying so, so it is collected too
    #: and asserted away rather than skipped.
    KINDS: ClassVar[tuple[str, ...]]
    UNDERIVABLE: ClassVar[tuple[str, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.KINDS, cls.UNDERIVABLE = _write_kinds()

    def setUp(self) -> None:
        super().setUp()
        self.terminal = {
            "closed": self.add_sprint_row("sprint:9200", status="closed"),
            "stopped": self.add_sprint_row("sprint:9201", status="stopped"),
        }

    def _answer(self, reference: str, kind: str) -> tuple[str, ...]:
        """What `_write` does with this kind on this sprint, as the table's cells say it."""
        from ummanu.sprints import SprintWriter

        writer = SprintWriter(self.board, data_dir=self.data_dir, instance=self.instance)
        try:
            writer._write(
                kind,
                "po",
                "operator",
                reference,
                f"{kind}-on-{reference}",
                {},
                lambda sprint: None,
            )
        except TaskError as refused:
            return ("refused", refused.code, str(refused.exit_code))
        return ("accepted",)

    def _documented(self) -> dict[str, tuple[str, ...]]:
        """The published table, read as rows rather than looked up by the kinds this test knows.

        Looking rows up would let a row for a kind the writer no longer has survive unnoticed, which
        is the same shape of hole from the other side.
        """
        protocols = (Path(__file__).resolve().parents[1] / "docs" / "PROTOCOLS.md").read_text(
            encoding="utf-8"
        )
        lines = protocols.splitlines()
        header = lines.index("| sprint write | a `closed` or `stopped` sprint |")
        rows: dict[str, tuple[str, ...]] = {}
        for line in lines[header + 2 :]:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.split("|")]
            # A kind may carry a digit (`e2e_budget_raised`, secretary-1796).
            kind = re.fullmatch(r"`([a-z0-9_]+)`", cells[1])
            self.assertIsNotNone(kind, f"unreadable row in the terminal-write table: {line}")
            assert kind is not None
            rows[kind.group(1)] = tuple(re.findall(r"`([a-z_0-9]+)`", cells[2]))
        return rows

    def test_every_kind_the_writer_passes_to_write_was_derivable(self) -> None:
        """The derivation is only a pin while it can see every call. It says so when it cannot."""
        self.assertEqual(self.UNDERIVABLE, ())
        self.assertIn("commented", self.KINDS)

    def test_the_document_names_every_write_the_writer_makes(self) -> None:
        self.assertEqual(sorted(self._documented()), sorted(self.KINDS))

    def test_each_documented_row_is_what_the_writer_actually_answers(self) -> None:
        documented = self._documented()
        for status, reference in self.terminal.items():
            for kind in self.KINDS:
                with self.subTest(status=status, kind=kind):
                    self.assertIn(kind, documented, f"{kind} is a sprint write the table omits")
                    self.assertEqual(self._answer(reference, kind), documented[kind])

    def test_the_two_semantic_writes_are_the_refused_ones(self) -> None:
        """The direction, stated outright, so a table of five acceptances could not pass by symmetry.

        Only these two are named by hand, because only these two are this card's own contract: a
        comment is accepted where it used to be refused, and the writes that state work in progress
        are still refused. What the rest answer is settled by the derivation above, not here.
        """
        for status, reference in self.terminal.items():
            with self.subTest(status=status):
                self.assertEqual(self._answer(reference, "commented"), ("accepted",))
                self.assertEqual(
                    self._answer(reference, "resume_recorded"), ("refused", "closed", "3")
                )
                self.assertEqual(
                    self._answer(reference, "current_task_set"), ("refused", "closed", "3")
                )


class CloseResultFaultTests(CloseFixture):
    """Criterion 7: every source of the result refuses on its own, and nothing claims for it."""

    def setUp(self) -> None:
        super().setUp()
        self.event_id = self.close()["event_id"]

    @contextlib.contextmanager
    def _journal_refuses(self) -> Any:
        with mock.patch.object(type(task_audit_for(self.board)), "events", side_effect=PermissionError("audit journal denied")):
            yield

    @contextlib.contextmanager
    def _index_refuses(self) -> Any:
        path = self.data_dir / "sprints" / "active-repositories.json"
        kept = path.read_bytes()
        path.write_bytes(b"{")
        try:
            yield
        finally:
            path.write_bytes(kept)

    def result(self) -> dict[str, Any]:
        document = self.reads().sprint_close_result(self.reference, self.event_id)
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
        return document

    def test_a_journal_nobody_can_read_leaves_the_close_unknown(self) -> None:
        with self._journal_refuses():
            document = self.result()

        close = document["close"]
        self.assertEqual(close["state"], sprint_reads_module.CLOSE_UNKNOWN)
        self.assertEqual(close["source"]["name"], "journal")
        self.assertEqual(close["source"]["state"], "unavailable")
        self.assertIsNone(close["issue_decisions"])
        self.assertIsNone(close["closeout"])
        # And it takes nothing away from the sources that answered.
        self.assertEqual(document["reservations"]["source"]["state"], "available")
        self.assertEqual(document["sprint"]["value"]["status"], "closed")

    def test_an_index_nobody_can_read_never_reports_a_reservation_as_released(self) -> None:
        with self._index_refuses():
            document = self.result()

        released = document["reservations"]
        self.assertEqual(released["source"]["name"], "reservations")
        self.assertEqual(released["source"]["state"], "unavailable")
        self.assertIsNone(released["released"])
        self.assertIsNone(released["held"])
        self.assertEqual(document["close"]["state"], sprint_reads_module.CLOSE_RECORDED)

    def test_a_board_nobody_can_read_leaves_the_close_and_the_reservations_standing(self) -> None:
        original = self.board.call
        board = _board_id(self.board, SPRINT_BOARD_NAME)

        def refuse(method: str, **params: Any) -> Any:
            if method == "getAllTasks" and params.get("project_id") == board:
                raise TaskError("backend_error", "the sprint board is unavailable", 1)
            return original(method, **params)

        self.board.call = refuse  # type: ignore[method-assign]
        try:
            document = self.result()
        finally:
            self.board.call = original  # type: ignore[method-assign]

        self.assertIsNone(document["sprint"]["value"])
        self.assertIsNone(document["reservations"]["declared"])
        self.assertEqual(document["reservations"]["source"]["name"], "sprints")
        self.assertEqual(document["close"]["state"], sprint_reads_module.CLOSE_RECORDED)

    def test_an_identifier_this_sprint_does_not_hold_is_absent_rather_than_unknown(self) -> None:
        self.event_id = "evt_nobody"
        document = self.result()
        self.assertEqual(document["close"]["state"], sprint_reads_module.CLOSE_ABSENT)
        self.assertEqual(document["close"]["source"]["name"], "journal")

    def test_a_close_result_needs_the_identifier_the_close_answered_with(self) -> None:
        with self.assertRaises(ValidationRefused):
            self.reads().sprint_close_result(self.reference, "")


class CloseCommandTests(CloseFixture):
    """Criterion 8: `ummanu sprint close` is a client, and its exit statuses are unchanged."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main([*argv, "--instance", str(self.instance), "--data-dir", str(self.data_dir)])
        return code, output.getvalue(), errors.getvalue()

    def _file(self, name: str, text: str) -> str:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _decisions_file(self) -> str:
        return self._file(
            "decisions.yaml",
            "issues:\n"
            "  - ref: issue:open\n"
            "    verdict: resolved\n"
            "    reason: the fix landed on this card\n"
            "  - ref: issue:second\n"
            "    verdict: open\n"
            "    reason: the sprint ran out before this one was reached\n"
            "cards:\n"
            f"  - ref: {self.left}\n"
            "    verdict: drop\n"
            "    reason: superseded by the next sprint's cut\n",
        )

    def _argv(self, **overrides: str) -> list[str]:
        argv = {
            "--ref": self.reference,
            "--role": "po",
            "--actor": "operator",
            "--request-id": "cli-close",
            "--reason": self.REASON,
            "--decisions-file": self._decisions_file(),
            "--closeout-file": self._file("closeout.md", CLOSEOUT_BODY),
        }
        argv.update(overrides)
        return ["sprint", "close", *[part for pair in argv.items() for part in pair]]

    def test_the_command_prints_the_document_the_operation_answered(self) -> None:
        code, output, errors = self._run(self._argv())

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint_closed")
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
        self.assertEqual(document["result"]["close"]["closed_issues"], ["issue:open"])
        self.assertFalse(document["definition_of_done"]["satisfied"])

    def test_a_refused_close_keeps_the_exit_status_this_command_has_always_given_it(self) -> None:
        code, output, errors = self._run(
            self._argv(**{"--decisions-file": self._file("empty.yaml", "issues: []\n")})
        )

        self.assertEqual(code, _EXIT_BY_CODE["validation"])
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "validation")

    def test_a_half_written_close_keeps_the_status_that_says_repeat_it(self) -> None:
        """`audit_pending` was exit 4 before this command became a client, and it still is."""
        from ummanu.sprints import SprintWriter

        with mock.patch.object(
            SprintWriter, "close", side_effect=TaskError("audit_pending", "repair required", 4)
        ):
            code, output, errors = self._run(self._argv())

        self.assertEqual(code, EXIT_PENDING)
        self.assertEqual(output, "")
        failure = json.loads(errors)["error"]
        self.assertEqual(failure["data"]["action"]["operation"], SPRINT_CLOSE_OPERATION)

    def test_a_card_whose_work_is_live_keeps_its_conflict_status(self) -> None:
        from ummanu.sprints import SprintWriter

        with mock.patch.object(
            SprintWriter, "close", side_effect=TaskError("live_work", "settle the head first", 3)
        ):
            code, _output, errors = self._run(self._argv())

        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(json.loads(errors)["error"]["code"], "owner_conflict")

    def test_close_result_reads_what_the_close_decided(self) -> None:
        code, output, errors = self._run(self._argv())
        self.assertEqual(code, 0, errors)
        event_id = json.loads(output)["event_id"]

        code, output, errors = self._run(
            ["sprint", "close-result", "--ref", self.reference, "--event-id", event_id]
        )

        self.assertEqual(code, 0, errors)
        document = json.loads(output)
        self.assertEqual(document["kind"], "sprint_close_result")
        self.assertEqual(document["close"]["disposed_tasks"], [self.left])
        self.assertFalse(document["definition_of_done"]["satisfied"])


class ClosePublishedPromiseTests(unittest.TestCase):
    """Criterion 9: the published prose is held to the contract rather than trusted to keep up.

    Each promise below is one this card's code makes. A change that moves the behaviour without the
    sentence, or the sentence without the behaviour, fails here instead of leaving a public claim
    behind.
    """

    def _document(self, name: str) -> str:
        return (Path(__file__).resolve().parents[1] / "docs" / name).read_text(encoding="utf-8")

    def test_the_protocol_records_the_operation_the_closeout_step_and_the_post_close_comment(self) -> None:
        protocols = self._document("PROTOCOLS.md")
        for promise in (
            "**`sprint_close(request_id, actor, reference, reason, closeout, decisions, role=\"po\")`**",
            "**`sprint_close_result(ref, event_id)`**",
            # The terminal phase order, with the step this card added in its place.
            "the dispositions, the knowledge closeout, then the\nstatus",
            "closeouts/<day>-<sprint-ref>.md",
            "#### A comment on a sprint that has ended",
            "`not_deliverable`",
        ):
            with self.subTest(promise=promise):
                self.assertIn(promise, protocols)

    def test_the_passages_a_terminal_sprint_is_read_through_agree_with_the_accepted_comment(
        self,
    ) -> None:
        """The three passages the finding named, beside `TerminalSprintWriteTests` which pins them.

        The table there holds the behaviour; these hold the sentences an operator reads *around* it,
        which is where the contradiction actually lived: the CLI's exit status, the errors table's
        two rows, and the resume-freshness paragraph whose reasoning had to survive the narrowing.
        """
        protocols = " ".join(self._document("PROTOCOLS.md").split())
        for promise in (
            "`sprint comment` on a `closed` or `stopped` sprint succeeds with exit status `0`",
            (
                "the sprint a resume or a current task names has ended "
                "(a comment on it is accepted, not refused)"
            ),
            "the create, the comment or the close is part-done and repairable with the same request id",
            "a terminal sprint's freshness never reads the audit the comment is recorded in",
        ):
            with self.subTest(promise=promise):
                self.assertIn(promise, protocols)

    def test_the_operator_scenario_is_written_down_end_to_end(self) -> None:
        operations = self._document("OPERATIONS.md")
        for promise in (
            "## Closing a sprint",
            "--closeout-file CLOSEOUT.md",
            "sprint close-result --ref sprint:1431 --event-id",
            "### Commenting after the close",
            "A close is not a completed Definition of Done.",
        ):
            with self.subTest(promise=promise):
                self.assertIn(promise, operations)

    def test_the_sentence_the_documents_carry_is_the_one_the_code_carries(self) -> None:
        """The one claim that may not drift: a close is not a satisfied contract."""
        clause = "it is not a statement that the sprint's definition of done was reached"
        self.assertIn(clause, CLOSE_NOT_DONE.lower())
        for name in ("PROTOCOLS.md", "OPERATIONS.md"):
            with self.subTest(document=name):
                self.assertIn("definition of done", self._document(name).lower())
        self.assertIn(clause, self._document("PROTOCOLS.md").lower())


if __name__ == "__main__":
    unittest.main()
