from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.fakes.sprints import SprintBackendFixture, SprintFixture
from tests.observer_identity import as_observer, bind_observer, unbound_observer
from tests.sprint_close_fixtures import (
    CLOSEOUT_BODY,
    DROP_REASON,
    KEEP_OPEN_REASON,
    close_decisions,
    init_state_repo,
)
from ummanu import sprints
from ummanu.board.models import EntityKind
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.steward_reports import StewardReportBoard
from ummanu.cli import main
from ummanu.config import load_config
from ummanu.dispatch.cleanup import CleanupJournal
from ummanu.knowledge_write import list_knowledge_documents
from ummanu.product_issues import ProductIssueStore
from ummanu.sprint_close import parse_close_decisions
from ummanu.sprint_observer import (
    encode_observer,
    head_choice,
    none_choice,
)
from ummanu.sprints import (
    BUDGET_EVENT_TYPES,
    BUDGET_UNCHARGED_EVENT_TYPES,
    BUDGET_UNCHARGED_INFRASTRUCTURE,
    SprintReader,
    SprintWriter,
    active_sprint_projects,
    budget_thresholds,
    open_sprint_admission_error,
    open_sprint_limit,
    open_sprint_limit_invalid,
    refresh_active_sprint_projects,
    sprint_admission_lock,
)
from ummanu.tasks import TaskError, TaskReader, TaskWriter

# A close states a verdict on every issue its sprint declared, and every sprint this fixture
# opens declares `issue:open`. The tests below are about the rest of the close, so they give
# the verdict that writes nothing: the issue stays open, with the basis on the close record.
KEEP_THE_ISSUE_OPEN = {
    "issues": [{"ref": "issue:open", "verdict": "open", "reason": KEEP_OPEN_REASON}],
    "cards": [],
}


def drop_cards(*refs: str) -> dict:
    """Keep the fixture's issue open and take the named cards off the closing contract."""
    return {
        "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
        "cards": [{"ref": ref, "verdict": "drop", "reason": DROP_REASON} for ref in refs],
    }


class SprintOwnershipTests(SprintFixture):
    """A sprint belongs to a Product, serves its open Issues and reserves projects."""

    def _assert_nothing_was_written(self) -> None:
        self.assertEqual(self._events(), [])
        self.assertEqual(self.sprint_record_count(), 0)

    def test_local_run_declarations_persist_read_audit_and_own_request_identity(self) -> None:
        entries = [{"project": "ummanu", "argv": ["python3", "-m", "tests.probe", "two words", ""], "rationale": "owner's exact probe"}]
        first = self._create(goal="declared", request_id="local-runs", local_run_exceptions=entries)
        reference = first["sprint"]["ref"]
        self.assertEqual(SprintReader(self.client).show(reference)["local_run_exceptions"], entries)
        self.assertEqual(first["sprint"]["local_run_exceptions"], entries)
        self.assertEqual(self._events()[0]["payload"]["intent"]["local_run_exceptions"], entries)
        self.assertEqual(self._create(goal="declared", request_id="local-runs", local_run_exceptions=entries)["event_id"], first["event_id"])
        for changed in ([], [{**entries[0], "argv": ["docker", "run"]}], [{**entries[0], "rationale": "changed"}]):
            with self.subTest(changed=changed), self.assertRaises(TaskError):
                self._create(goal="declared", request_id="local-runs", local_run_exceptions=changed)
        self.assertEqual(len(self._events()), 1)

    def test_default_local_runs_replay_old_identity_and_reject_unknown_project(self) -> None:
        with self.assertRaises(TaskError):
            self._create(goal="unknown", projects=["unregistered"], local_run_exceptions=[
                {"project": "unregistered", "argv": ["docker", "run"], "rationale": "probe"}
            ])
        self._assert_nothing_was_written()
        first = self._create(goal="old-default", request_id="old-default")
        self.assertNotIn("local_run_exceptions", self._events()[0]["payload"]["intent"])
        replay = self._create(goal="old-default", request_id="old-default", local_run_exceptions=[])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(replay["sprint"]["local_run_exceptions"], [])

    def test_create_requires_product_issue_and_reservation_before_any_write(self) -> None:
        for kwargs, message in (
            ({"product": ""}, "owning product"),
            ({"issues": []}, "at least one open issue"),
            ({"projects": []}, "at least one reserved project"),
            ({"product": "ghost"}, "was not found"),
            ({"issues": ["issue:missing"]}, "was not found"),
            ({"projects": ["unregistered"]}, "unknown registered project"),
        ):
            with self.assertRaisesRegex(TaskError, message):
                self._create(goal="rejected", **kwargs)
            self._assert_nothing_was_written()

    def test_foreign_and_closed_issues_are_refused_separately(self) -> None:
        with self.assertRaisesRegex(TaskError, "belongs to product 'other'") as foreign:
            self._create(goal="foreign issue", issues=["issue:foreign"])
        self.assertEqual(foreign.exception.code, "validation")

        with self.assertRaisesRegex(TaskError, "is closed") as closed:
            self._create(goal="closed issue", issues=["issue:done"])
        self.assertEqual(closed.exception.code, "validation")
        self._assert_nothing_was_written()

    def test_second_open_sprint_is_refused_and_names_the_open_one(self) -> None:
        first = self._create(goal="first", reference="sprint:first")["sprint"]["ref"]

        with self.assertRaisesRegex(TaskError, first) as raised:
            self._create(
                goal="second",
                reference="sprint:second",
                projects=["secretary-instance"],
            )

        self.assertEqual(raised.exception.code, "sprint_conflict")
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], [first])  # type: ignore[arg-type]

    def test_a_reserved_project_is_a_resource_conflict_of_its_own(self) -> None:
        first = self._create(goal="first", reference="sprint:first")["sprint"]["ref"]

        with self.assertRaisesRegex(TaskError, "already reserved") as raised:
            self._create(goal="second", reference="sprint:second", projects=["ummanu"])

        self.assertEqual(raised.exception.code, "resource_conflict")
        self.assertIn("ummanu held by " + first, raised.exception.message)

    def test_a_closed_sprint_releases_its_reservation(self) -> None:
        first = self._create(goal="first", reference="sprint:first")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)

        second = self._create(goal="second", reference="sprint:second")["sprint"]

        self.assertEqual(second["reservations"], ["ummanu"])

    def test_create_replay_returns_the_same_event_instead_of_conflicting_with_itself(self) -> None:
        first = self._create(goal="replayed", request_id="create-once")
        second = self._create(goal="replayed", request_id="create-once")

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["sprint"]["ref"], second["sprint"]["ref"])
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_a_concurrent_repeat_of_one_request_is_replayed_not_refused(self) -> None:
        """At-least-once delivery may overlap with the request it repeats.

        Both callers are held at the admission gate before either can look at live
        state, so neither could have seen the other's sprint. The repeat has to come
        back with the first event instead of colliding with the sprint it opened.
        """
        self.ensure_backend_ready()
        started = threading.Barrier(3)
        outcomes: dict[str, Any] = {}
        waiting_at_gate = threading.Event()
        arrivals_lock = threading.Lock()
        arrivals = 0
        real_admission_lock = sprint_admission_lock

        @contextlib.contextmanager
        def observed_admission_lock(data_dir: str | Path):
            nonlocal arrivals
            with arrivals_lock:
                arrivals += 1
                if arrivals == 2:
                    waiting_at_gate.set()
            with real_admission_lock(data_dir):
                yield

        def deliver(name: str) -> None:
            writer = SprintWriter(  # type: ignore[arg-type]
                self.client,
                data_dir=self.tmp.name,
                instance=self.instance,
            )
            started.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po",
                    actor="operator",
                    goal="one delivery",
                    reference="sprint:once",
                    product="ummanu",
                    issues=["issue:open"],
                    projects=["ummanu"],
                    observer=head_choice("codex-observer"),
                    request_id="same-delivery",
                )
            except TaskError as exc:
                outcomes[name] = exc

        threads = [threading.Thread(target=deliver, args=(name,)) for name in ("first", "second")]
        with (
            sprint_admission_lock(self.tmp.name),
            mock.patch.object(sprints, "sprint_admission_lock", observed_admission_lock),
        ):
            for thread in threads:
                thread.start()
            # Both are inside `create` and waiting for the gate before it is released.
            started.wait(timeout=5)
            self.assertTrue(waiting_at_gate.wait(timeout=5), "both creates reached the admission gate")
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual([type(value) for value in outcomes.values()], [dict, dict], outcomes)
        self.assertEqual(len({result["event_id"] for result in outcomes.values()}), 1, outcomes)
        self.assertEqual([event["kind"] for event in self._events()], ["created"])
        self.assertEqual([sprint["ref"] for sprint in SprintReader(self.client).list()], ["sprint:once"])  # type: ignore[arg-type]

    def _refuse_once(self, refused_method: str, field: str = ""):
        """Answer the first call of that method carrying `field` with a refusal (`False`)."""
        original = self.client.call
        refused: list[str] = []

        def refuse(method: str, **params: object) -> object:
            values = dict(params.get("values") or {}) if method == "saveTaskMetadata" else {}  # type: ignore[arg-type]
            if method == refused_method and (not field or field in values) and not refused:
                refused.append(method)
                return False
            return original(method, **params)

        return mock.patch.object(self.client, "call", side_effect=refuse)

    def _refuse_metadata(self, field: str):
        return self._refuse_once("saveTaskMetadata", field)

    def _reject_removal(self):
        """Answer every `removeTask` the way a backend that keeps the row does."""
        original = self.client.call

        def refuse(method: str, **params: object) -> object:
            if method == "removeTask":
                return False
            return original(method, **params)

        return mock.patch.object(self.client, "call", side_effect=refuse)

    def _stall_create(self, request_id: str, **kwargs) -> None:
        """Leave one admitted create staged, repairable by its own request id."""
        with self._refuse_metadata("sprint_goal"):
            with self.assertRaisesRegex(TaskError, "pending repair") as pending:
                self._create(goal="rejected metadata", request_id=request_id, **kwargs)
        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(self._events(), [])

    def test_an_automatic_reference_clears_every_number_the_board_handed_out(self) -> None:
        """The counter used to be the row's own id, which forgets what it already gave away.

        Live on 2026-08-06 that handed a new sprint `sprint:804`, taken by a sprint closed in July,
        and `show` then resolved the new reference to the old row.
        """
        self.arrange_historical_sprint("sprint:804")
        self.arrange_historical_sprint("sprint:1153")

        created = self._create(goal="numbered above every reference")["sprint"]

        self.assertEqual(created["ref"], "sprint:1154")
        self.assertEqual(
            SprintReader(self.client).show("sprint:1154")["goal"],  # type: ignore[arg-type]
            "numbered above every reference",
        )

    def test_an_automatic_reference_that_is_claimed_is_refused_not_adopted(self) -> None:
        """Allocation is only as good as the enumeration it counted, so the claim decides.

        An enumeration that missed a row is simulated here by allocating a reference the board
        already holds: the create must refuse it loudly instead of publishing a second sprint under
        someone else's reference.
        """
        taken = self._create(goal="the sprint that holds it", reference="sprint:900")["sprint"]
        # Closing it frees the installation to open another sprint; the reference stays taken.
        self.writer.close(
            role="po",
            actor="operator",
            reference=taken["ref"],
            decisions=KEEP_THE_ISSUE_OPEN,
        )

        with mock.patch.object(sprints, "next_reference", return_value="sprint:900"):
            with self.assertRaisesRegex(TaskError, "sprint:900") as raised:
                self._create(goal="collides", request_id="collides")

        self.assertEqual(raised.exception.code, "sprint_conflict")
        self.assertEqual(SprintReader(self.client).show("sprint:900")["goal"], taken["goal"])  # type: ignore[arg-type]
        self.assertEqual(
            [event["ref"] for event in self._events() if event["kind"] == "created"],
            ["sprint:900"],
        )

    def test_a_restored_sprint_without_a_reference_is_refused_rather_than_given_a_row(self) -> None:
        """A restore adopts the row it finds under its reference, so it has to name one.

        It is the one create that may take over a row it did not write, because that row is the one
        it exported and is putting back. An invented reference there would let it adopt a row it has
        never seen, which is the silent adoption every rule in this area exists to prevent.
        """
        held = self._create(
            goal="already on the board",
            reference="sprint:900",
            request_id="held",
        )["sprint"]
        sprint_before = self.sprint("sprint:900")
        records_before = self.sprint_record_count()

        with self.assertRaisesRegex(TaskError, "must name its own reference") as raised:
            self.writer.restore_create(
                reference="",
                goal="restored without a reference",
                request_id="restore-no-reference",
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.sprint("sprint:900"), sprint_before)
        self.assertEqual(self.sprint_record_count(), records_before)
        self.assertEqual(SprintReader(self.client).show("sprint:900")["goal"], held["goal"])  # type: ignore[arg-type]
        self.assertEqual([event["request_id"] for event in self._events()], ["held"])
        self.assertEqual(self.transaction_state(), {"ok": True, "pending": 0})

    def test_a_repeated_create_records_exactly_one_audit_event(self) -> None:
        first = self._create(goal="repeated", request_id="repeat-once")
        results = [self._create(goal="repeated", request_id="repeat-once") for _ in range(3)]

        self.assertEqual({result["event_id"] for result in results}, {first["event_id"]})
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_a_repeat_with_another_payload_is_refused_before_any_side_effect(self) -> None:
        self._create(goal="original", reference="sprint:original", request_id="claimed")
        before = (self.sprint_record_count(), list(self._events()))

        with self.assertRaisesRegex(TaskError, "request id belongs to another operation") as raised:
            self._create(goal="different", reference="sprint:other", request_id="claimed")

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual((self.sprint_record_count(), self._events()), before)
        self.assertEqual([event["kind"] for event in self._events()], ["created"])

    def test_concurrent_creates_admit_exactly_one_open_sprint(self) -> None:
        """Two writers that check at the same time still open one sprint between them.

        The rules are reads of live state, so without a shared gate both would see an
        installation with no open sprint and both would create a row.
        """
        self.ensure_backend_ready()
        start = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def open_sprint(name: str, project: str) -> None:
            writer = SprintWriter(  # type: ignore[arg-type]
                self.client,
                data_dir=self.tmp.name,
                instance=self.instance,
            )
            start.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po",
                    actor="operator",
                    goal=name,
                    reference=f"sprint:{name}",
                    product="ummanu",
                    issues=["issue:open"],
                    projects=[project],
                    observer=head_choice("codex-observer"),
                )
            except TaskError as exc:
                outcomes[name] = exc

        threads = [
            threading.Thread(target=open_sprint, args=("left", "ummanu")),
            threading.Thread(target=open_sprint, args=("right", "secretary-instance")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        refused = [value for value in outcomes.values() if isinstance(value, TaskError)]
        self.assertEqual(len(refused), 1, outcomes)
        self.assertEqual(refused[0].code, "sprint_conflict")
        open_sprints = SprintReader(self.client).list(statuses={"open"})  # type: ignore[arg-type]
        self.assertEqual(len(open_sprints), 1, [sprint["ref"] for sprint in open_sprints])

    def test_both_transitions_into_open_wait_for_the_admission_gate(self) -> None:
        """Both ways into `open` take the gate, so neither can slip past a holder."""
        ref = self._create(goal="gated", reference="sprint:gated")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

        for name, call in (
            (
                "reopen",
                lambda: self.writer.reopen(
                    observer=head_choice("codex-observer"), role="po", actor="operator", reference=ref
                ),
            ),
            ("create", lambda: self._create(goal="second", reference="sprint:second")),
        ):
            done = threading.Event()
            worker = threading.Thread(target=lambda call=call, done=done: (call(), done.set()))
            with sprint_admission_lock(self.tmp.name):
                worker.start()
                self.assertFalse(done.wait(timeout=0.3), name)
            worker.join(timeout=10)
            self.assertTrue(done.is_set(), name)
            self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

    def test_recovery_reproduces_the_roots_a_closed_row_already_carries(self) -> None:
        """Canonicalization is a rule for declaring a sprint, not for reproducing one.

        The rows closed before it carry the spellings their creates wrote, and recovery
        has to bring them back unchanged; rewriting them here would make the restored
        entity differ from its own export.
        """
        legacy = self.writer.restore_create(
            reference="sprint:legacy",
            goal="legacy",
            repositories=["ummanu", "."],
            request_id="legacy-create",
        )["sprint"]
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        self.assertEqual(legacy["repositories"], ["ummanu", "."])
        for view in (reader.show(legacy["ref"]), reader.list()[0], reader.export()[0]):
            self.assertEqual(view["repositories"], ["ummanu", "."])
        self.assertEqual(
            self.writer.close(
                role="po",
                actor="operator",
                reference=legacy["ref"],
            )["sprint"]["repositories"],
            ["ummanu", "."],
        )

    def test_show_status_and_export_carry_the_new_links(self) -> None:
        ref = self._create(goal="linked", projects=["ummanu", "secretary-instance"])["sprint"]["ref"]
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        shown = reader.show(ref)
        status = reader.status(ref)
        exported = reader.export()[0]

        for view in (shown, status, exported):
            self.assertEqual(view["product"], "ummanu")
            self.assertEqual(view["issues"], ["issue:open"])
            self.assertEqual(view["reservations"], ["ummanu", "secretary-instance"])

    def test_reopen_rechecks_ownership_and_stays_idempotent(self) -> None:
        ref = self._create(goal="reopened", reference="sprint:reopened")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)

        first = self.writer.reopen(
            observer=head_choice("codex-observer"),
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-once",
        )
        second = self.writer.reopen(
            observer=head_choice("codex-observer"),
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-once",
        )

        self.assertEqual(first["sprint"]["status"], "open")
        self.assertEqual(first["event_id"], second["event_id"])

    def test_reopen_refuses_a_request_id_that_belongs_to_another_operation(self) -> None:
        """The transition into `open` owns its request id like every other one.

        A caller that passes the id of its own `close` back to `reopen` is delivering a
        second operation under one id. It has to be refused before a side effect instead
        of being replayed into a `reopened` answer the board never made.
        """
        ref = self._create(goal="reused id", reference="sprint:reused")["sprint"]["ref"]
        self.writer.close(
            role="po", actor="operator", reference=ref, request_id="reused-id", decisions=KEEP_THE_ISSUE_OPEN
        )
        before = (self.sprint(ref), list(self._events()))

        with self.assertRaisesRegex(TaskError, "request id belongs to another operation") as raised:
            self.writer.reopen(
                observer=head_choice("codex-observer"),
                role="po",
                actor="operator",
                reference=ref,
                request_id="reused-id",
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual((self.sprint(ref), self._events()), before)
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        self.assertEqual([event["kind"] for event in self._events()], ["created", "sprint.closed", "closed"])

    def test_reopen_is_refused_when_its_only_issue_has_been_closed(self) -> None:
        ref = self._create(goal="issue closed later", reference="sprint:stale")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        self.arrange_issue_closed()

        with self.assertRaisesRegex(TaskError, "is closed"):
            self.writer.reopen(
                observer=head_choice("codex-observer"), role="po", actor="operator", reference=ref
            )


class TwoOpenSprintFixture(SprintFixture):
    """Three pairwise disjoint sprint candidates, of which the setting admits two.

    The fixture gains a third product, issue and project, because the count refusal is
    only reachable once three sprints can be pairwise disjoint on everything else.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.instance / "projects" / "third.yaml").write_text("id: third\n", encoding="utf-8")
        self.arrange_product("third", projects=["third"])
        self.third_issue = self.arrange_issue("third", product="third")["ref"]
        self.roots = Path(self.tmp.name) / "repos"

    def _limit(self, value: object) -> None:
        # Set the one line and keep the rest: the installation's `data_dir` is where its generated
        # head registry lives (ummanu-26), so replacing the whole file would unplace it.
        instance_file = self.instance / "instance.yaml"
        kept = [
            line
            for line in instance_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith("open_sprint_limit:")
        ]
        instance_file.write_text("\n".join([*kept, f"open_sprint_limit: {value}"]) + "\n", encoding="utf-8")

    def _first(self, **kwargs) -> str:
        kwargs.setdefault("repositories", [str(self.roots / "ummanu")])
        return self._create(goal="first", reference="sprint:first", **kwargs)["sprint"]["ref"]

    def _second(self, **kwargs) -> dict:
        """A sprint disjoint from `_first` on product, reservation and repository."""
        for field, value in (
            ("goal", "second"),
            ("reference", "sprint:second"),
            ("product", "other"),
            ("issues", ["issue:foreign"]),
            ("projects", ["other"]),
            ("repositories", [str(self.roots / "other")]),
            ("observer", none_choice()),
        ):
            kwargs.setdefault(field, value)
        return self._create(**kwargs)

    def _third(self, **kwargs) -> dict:
        for field, value in (
            ("goal", "third"),
            ("reference", "sprint:third"),
            ("product", "third"),
            ("issues", [self.third_issue]),
            ("projects", ["third"]),
            ("repositories", [str(self.roots / "third")]),
            ("observer", none_choice()),
        ):
            kwargs.setdefault(field, value)
        return self._create(**kwargs)

    def _open_refs(self) -> list[str]:
        return [
            sprint["ref"]
            for sprint in SprintReader(self.client).list(statuses={"open"}, create=False)  # type: ignore[arg-type]
        ]

    def _assert_refusal_left_nothing(self, call, code: str, message: str) -> None:
        """Prove a refusal is only an answer: no row, no staged intent, no audit event."""
        rows = self.sprint_record_count()
        transactions = self.transaction_state()
        events = [event["event_id"] for event in self._events()]
        audit = SqlTaskAudit(self.client)
        pending = [event["event_id"] for event in audit.pending_events()]

        with self.assertRaisesRegex(TaskError, message) as raised:
            call()

        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.sprint_record_count(), rows)
        self.assertEqual(self.transaction_state(), transactions)
        self.assertEqual([event["event_id"] for event in self._events()], events)
        self.assertEqual([event["event_id"] for event in audit.pending_events()], pending)


class TwoOpenSprintAdmissionTests(TwoOpenSprintFixture):
    """The opt-in limit of two open sprints, and the disjointness that makes it safe."""

    def test_the_setting_reader_never_widens_the_limit(self) -> None:
        self.assertEqual(open_sprint_limit(None), 1)
        self.assertEqual(open_sprint_limit({}), 1)
        self.assertEqual(open_sprint_limit({"open_sprint_limit": 2}), 2)
        for value in (0, 3, -1, 1.5, "", "2", True, False, None, [2], {"limit": 2}):
            with self.subTest(value=value):
                config = {"open_sprint_limit": value}
                self.assertEqual(open_sprint_limit(config), 1)
                self.assertTrue(open_sprint_limit_invalid(config))
        self.assertFalse(open_sprint_limit_invalid({}))
        self.assertFalse(open_sprint_limit_invalid({"open_sprint_limit": 1}))
        self.assertFalse(open_sprint_limit_invalid({"open_sprint_limit": 2}))

    def test_an_absent_or_singleton_setting_keeps_the_installation_a_singleton(self) -> None:
        """The default and an explicit 1 are the behaviour every installation has today."""
        for setting in (None, 1):
            with self.subTest(setting=setting):
                self.setUp()
                if setting is not None:
                    self._limit(setting)
                first = self._first()

                self._assert_refusal_left_nothing(
                    self._second,
                    "sprint_conflict",
                    f"installation already has an open sprint: {first}; close it before opening another",
                )
                self.assertEqual(self._open_refs(), [first])

    def test_an_invalid_setting_fails_closed_and_is_reported(self) -> None:
        """No unreadable value may widen the limit, and doctor has to name it."""
        for setting in ("0", "3", "-1", "1.5", '""', "true", "two"):
            with self.subTest(setting=setting):
                self.setUp()
                self._limit(setting)
                first = self._first()

                self._assert_refusal_left_nothing(
                    self._second,
                    "sprint_conflict",
                    "installation already has an open sprint",
                )
                self.assertEqual(self._open_refs(), [first])
                self.assertTrue(open_sprint_limit_invalid(load_config(self.instance / "instance.yaml")))

    def test_a_disjoint_second_sprint_is_admitted_under_the_pilot_limit(self) -> None:
        self._limit(2)
        first = self._first()

        second = self._second()["sprint"]

        self.assertEqual(second["product"], "other")
        self.assertEqual(sorted(self._open_refs()), sorted([first, second["ref"]]))

    def test_a_second_sprint_of_the_same_product_is_refused(self) -> None:
        self._limit(2)
        first = self._first()

        self._assert_refusal_left_nothing(
            lambda: self._second(product="ummanu", issues=["issue:open"]),
            "resource_conflict",
            f"product ummanu is already the product of open sprint {first}",
        )
        self.assertEqual(self._open_refs(), [first])

    def test_a_shared_reservation_is_refused_before_the_count(self) -> None:
        """The reservation clash reads the same at either limit, and names the holder."""
        self._limit(2)
        first = self._first()

        self._assert_refusal_left_nothing(
            lambda: self._second(projects=["ummanu"]),
            "resource_conflict",
            f"ummanu held by {first}",
        )
        self.assertEqual(self._open_refs(), [first])

    def test_an_overlapping_repository_root_names_the_tree_not_the_count(self) -> None:
        """Nesting is overlap, a sibling prefix is not, and symlinks are resolved first."""
        self._limit(2)
        (self.roots / "ummanu").mkdir(parents=True)
        link = Path(self.tmp.name) / "linked-ummanu"
        link.symlink_to(self.roots / "ummanu")
        first = self._first()

        for repositories in (
            [str(self.roots / "ummanu")],
            [str(self.roots / "ummanu" / "nested")],
            [str(self.roots / "ummanu") + "/./nested/.."],
            [str(link)],
        ):
            with self.subTest(repositories=repositories):
                self._assert_refusal_left_nothing(
                    lambda repositories=repositories: self._second(repositories=repositories),
                    "resource_conflict",
                    f"held by open sprint {first}",
                )

        sibling = self._second(repositories=[str(self.roots / "secretary-instance")])["sprint"]

        self.assertEqual(sorted(self._open_refs()), sorted([first, sibling["ref"]]))

    def _stored_repositories(self, reference: str, values: list[str]) -> None:
        """Put values on an open row that no create would write, as a legacy row carries."""
        self.arrange_metadata(reference, sprint_repositories=json.dumps(values))

    def test_a_declared_root_is_canonicalized_where_it_is_declared(self) -> None:
        """The reviewer's sequence, which used to admit an overlapping pair.

        A sprint that declared `.` persisted the literal `.`, and the next admission
        resolved it against its own working directory.  Run from a second tree, the
        first sprint's stored root pointed at that second tree, the two read as
        disjoint, and both were admitted although they shared one working tree.
        """
        self._limit(2)
        work_a, work_b = self.roots / "work-a", self.roots / "work-b"
        for path in (work_a, work_b):
            path.mkdir(parents=True)

        with contextlib.chdir(work_a):
            first = self._first(repositories=["."])

        self.assertEqual(
            SprintReader(self.client).show(first)["repositories"],
            [str(work_a)],  # type: ignore[arg-type]
        )

        with contextlib.chdir(work_b):
            self._assert_refusal_left_nothing(
                lambda: self._second(repositories=[str(work_a)]),
                "resource_conflict",
                f"overlaps {work_a}, held by open sprint {first}",
            )
        self.assertEqual(self._open_refs(), [first])

    def test_a_root_this_host_cannot_resolve_is_refused_before_anything_is_written(self) -> None:
        """A declaration nobody can canonicalize is an answer, not a sprint."""
        with mock.patch.object(Path, "resolve", side_effect=OSError("too many levels")):
            self._assert_refusal_left_nothing(
                lambda: self._first(repositories=["/loop"]),
                "validation",
                "repository root '/loop' cannot be resolved on this host",
            )
        self.assertEqual(self._open_refs(), [])

    def test_a_stored_root_that_is_not_absolute_is_refused_rather_than_resolved(self) -> None:
        """Admission never resolves a stored root: it would answer against its own cwd.

        Both sides are judged, because a relative root proves nothing about the tree it
        names whichever of the two sprints happens to carry it.
        """
        self._limit(2)
        first = self._first()
        self._stored_repositories(first, ["."])

        self._assert_refusal_left_nothing(
            self._second,
            "resource_conflict",
            f"open sprint {first} declares repository root '.', which is not an absolute path",
        )

        self._stored_repositories(first, [str(self.roots / "ummanu")])
        second = self._second()["sprint"]["ref"]
        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )
        self._stored_repositories(second, ["../elsewhere"])

        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=second,
                observer=none_choice(),
            ),
            "resource_conflict",
            "this sprint declares repository root '../elsewhere', which is not an absolute path",
        )
        self.assertEqual(self._open_refs(), [first])

    def test_a_sole_sprint_may_not_be_reopened_under_a_root_nobody_can_place(self) -> None:
        """The candidate's own roots are judged whether or not another sprint is open.

        Nothing else being open is what makes this reachable: with the pairwise scan as
        the only check, a row is excluded from its own comparison, the loop over the
        other open sprints has nothing to run, and a relative root reaches `open`.
        """
        self._limit(2)
        first = self._first()
        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)
        self._stored_repositories(first, ["."])
        self.assertEqual(self._open_refs(), [])

        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=first,
                observer=none_choice(),
            ),
            "resource_conflict",
            "this sprint declares repository root '.', which is not an absolute path",
        )
        self.assertEqual(self._open_refs(), [])
        self.assertEqual(
            SprintReader(self.client).show(first, include_cards=False)["status"],
            "closed",  # type: ignore[arg-type]
        )

    def test_a_disjoint_second_sprint_may_declare_its_own_observer_head(self) -> None:
        """An observer call is bound to its sprint, so both open sprints may run a head.

        The ceiling this replaces refused the second head because nothing scoped an
        observer call to the sprint it was about.  That binding exists now, and the
        declaration is judged on the disjointness rules alone.
        """
        self._limit(2)
        first = self._first()

        second = self._second(observer=head_choice("claude-observer"))["sprint"]

        self.assertEqual(second["observer"], head_choice("claude-observer"))
        self.assertEqual(sorted(self._open_refs()), sorted([first, second["ref"]]))

    def test_a_third_sprint_is_refused_however_disjoint_it_is(self) -> None:
        self._limit(2)
        first = self._first()
        second = self._second()["sprint"]["ref"]

        self._assert_refusal_left_nothing(
            self._third,
            "sprint_conflict",
            "installation already holds its limit of 2 open sprints: " + ", ".join(sorted([first, second])),
        )
        self.assertEqual(sorted(self._open_refs()), sorted([first, second]))

    def test_a_third_sprint_that_collides_is_told_the_resource_not_the_count(self) -> None:
        """At capacity too, the refusal names the holder the caller has to close.

        The count names every open sprint and distinguishes none of them, so a caller
        acting on it can close the wrong one and be refused again.
        """
        self._limit(2)
        first = self._first()
        second = self._second()["sprint"]["ref"]

        for name, candidate, code, message in (
            (
                "reservation",
                lambda: self._third(projects=["other"]),
                "resource_conflict",
                f"other held by {second}",
            ),
            (
                "product",
                lambda: self._third(product="other", issues=["issue:foreign"]),
                "resource_conflict",
                f"product other is already the product of open sprint {second}",
            ),
            (
                "repository",
                lambda: self._third(repositories=[str(self.roots / "other")]),
                "resource_conflict",
                f"overlaps {self.roots / 'other'}, held by open sprint {second}",
            ),
        ):
            with self.subTest(collision=name):
                self._assert_refusal_left_nothing(candidate, code, message)

        # A declared head is not a collision of its own: a disjoint third sprint is told the
        # count, which is the only thing left standing in its way.
        self._assert_refusal_left_nothing(
            lambda: self._third(observer=head_choice("claude-observer")),
            "sprint_conflict",
            "installation already holds its limit of 2 open sprints",
        )
        self.assertEqual(sorted(self._open_refs()), sorted([first, second]))

    def test_a_pair_that_cannot_be_proven_disjoint_is_refused_in_either_order(self) -> None:
        """Both orderings of the pair refuse, whichever row carries the opaque value.

        A one-way comparison would admit the pair whenever the opaque row happened to be
        the one already open, which the repository check got wrong once.
        """
        for attribute, opaque in (
            ("product", {"product": "", "repositories": [str(self.roots / "opaque")]}),
            ("repository", {"product": "opaque", "repositories": ["."]}),
        ):
            for refs in (("sprint:a", "sprint:b"), ("sprint:b", "sprint:a")):
                with self.subTest(attribute=attribute, opaque_ref=refs[0]):
                    rows = [
                        {"ref": refs[0], "reservations": [], **opaque},
                        {
                            "ref": refs[1],
                            "reservations": [],
                            "product": "plain",
                            "repositories": [str(self.roots / "plain")],
                        },
                    ]
                    self.assertIsNotNone(open_sprint_admission_error(rows, limit=2))

    def test_reopen_obeys_the_same_rules_excluding_only_its_own_row(self) -> None:
        self._limit(2)
        first = self._first()
        second = self._second()["sprint"]["ref"]
        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )

        # Its own reservation, product and repository are not collisions of its own row.
        reopened = self.writer.reopen(
            role="po",
            actor="operator",
            reference=second,
            observer=none_choice(),
        )
        self.assertEqual(reopened["sprint"]["status"], "open")

        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )
        # A reopen may declare its own head beside the one the other sprint already runs.
        with_head = self.writer.reopen(
            role="po",
            actor="operator",
            reference=second,
            observer=head_choice("claude-observer"),
        )
        self.assertEqual(with_head["sprint"]["observer"], head_choice("claude-observer"))

        self.writer.close(
            role="po",
            actor="operator",
            reference=second,
            decisions=close_decisions(self.writer, second),
        )
        third = self._third(projects=["other"])["sprint"]["ref"]
        # At its limit too, the reservation it collides on is named ahead of the count.
        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=second,
                observer=none_choice(),
            ),
            "resource_conflict",
            f"other held by {third}",
        )
        self.assertEqual(sorted(self._open_refs()), sorted([first, third]))

        # With room again, the reservation the third sprint took is what refuses it.
        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)
        self._assert_refusal_left_nothing(
            lambda: self.writer.reopen(
                role="po",
                actor="operator",
                reference=second,
                observer=none_choice(),
            ),
            "resource_conflict",
            f"other held by {third}",
        )
        self.assertEqual(self._open_refs(), [third])

    def test_concurrent_creates_admit_at_most_the_limit(self) -> None:
        """Three disjoint creates at once still leave exactly two open sprints."""
        self._limit(2)
        self.ensure_backend_ready()
        start = threading.Barrier(3)
        outcomes: dict[str, Any] = {}
        candidates = {
            "first": ("ummanu", "issue:open", "ummanu"),
            "second": ("other", "issue:foreign", "other"),
            "third": ("third", self.third_issue, "third"),
        }

        def open_sprint(name: str) -> None:
            product, issue, project = candidates[name]
            writer = SprintWriter(  # type: ignore[arg-type]
                self.client,
                data_dir=self.tmp.name,
                instance=self.instance,
            )
            start.wait(timeout=5)
            try:
                outcomes[name] = writer.create(
                    role="po",
                    actor="operator",
                    goal=name,
                    reference=f"sprint:{name}",
                    product=product,
                    issues=[issue],
                    projects=[project],
                    repositories=[str(self.roots / project)],
                    observer=none_choice(),
                )
            except TaskError as exc:
                outcomes[name] = exc

        threads = [threading.Thread(target=open_sprint, args=(name,)) for name in candidates]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        refused = [value for value in outcomes.values() if isinstance(value, TaskError)]
        self.assertEqual(len(refused), 1, outcomes)
        self.assertEqual(refused[0].code, "sprint_conflict")
        self.assertIn("its limit of 2 open sprints", refused[0].message)
        self.assertEqual(len(self._open_refs()), 2, self._open_refs())

    def test_an_open_sprint_without_a_product_cannot_be_proven_disjoint(self) -> None:
        """A restored legacy row is opaque, so it holds the installation on its own."""
        self._limit(2)
        self.writer.restore_create(
            reference="sprint:legacy",
            goal="legacy",
            request_id="legacy",
            status="open",
        )

        self._assert_refusal_left_nothing(
            self._second,
            "resource_conflict",
            "open sprint sprint:legacy declares no product",
        )
        self.assertEqual(self._open_refs(), ["sprint:legacy"])


class TwoOpenSprintIsolationTests(TwoOpenSprintFixture):
    """What the entity itself keeps apart once two sprints are open at the same time.

    The pair is opened through admission under the pilot setting, not written onto the
    board, so every fact below is one a real installation could reach.  The budget, the
    hard stop, the close and the reserved-project index are each read for both sprints
    after a write that names one of them.
    """

    def setUp(self) -> None:
        super().setUp()
        self._limit(2)

    def _pair(self) -> tuple[str, str]:
        first = self._first()
        second = self._second()["sprint"]["ref"]
        self.assertEqual(sorted(self._open_refs()), sorted([first, second]))
        return first, second

    def _writer(self, **thresholds: int) -> SprintWriter:
        return SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds=thresholds or None,
        )

    def _budget_of(self, reference: str, writer: SprintWriter | None = None) -> dict:
        reader = (writer or self.writer).reader
        return reader.show(reference, include_cards=False)["budget"]

    def _status_of(self, reference: str) -> str:
        return self.writer.reader.show(reference, include_cards=False)["status"]

    def _charge(self, writer: SprintWriter, reference: str, event_type: str, request_id: str) -> None:
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=reference,
            event_type=event_type,
            request_id=request_id,
            source_event_id="evt-" + request_id,
        )

    def test_a_charge_moves_the_counters_of_the_sprint_it_names_only(self) -> None:
        first, second = self._pair()

        self._charge(self.writer, first, "red_ci", "charge-first")

        self.assertEqual(self._budget_of(first)["total"], 1)
        self.assertEqual(self._budget_of(first)["by_type"]["red_ci"], 1)
        self.assertEqual(self._budget_of(second)["total"], 0)
        self.assertEqual(
            self._budget_of(second)["by_type"],
            {event: 0 for event in BUDGET_EVENT_TYPES},
        )
        # The charge is an event of its own sprint, and the other sprint has none.
        self.assertEqual(
            [event["kind"] for event in SqlTaskAudit(self.client).events(reference=first)],
            ["created", "budget_recorded"],
        )
        self.assertEqual(
            [event["kind"] for event in SqlTaskAudit(self.client).events(reference=second)],
            ["created"],
        )

    def test_each_sprint_reaches_its_signal_threshold_on_its_own_counters(self) -> None:
        """Two charges to one sprint are not two charges to the installation."""
        writer = self._writer(signal=2, hard=4)
        first, second = self._pair()

        self._charge(writer, first, "red_ci", "signal-first-1")
        self.assertFalse(self._budget_of(first, writer)["signal_reached"])

        self._charge(writer, first, "blocked", "signal-first-2")

        self.assertTrue(self._budget_of(first, writer)["signal_reached"])
        self.assertFalse(self._budget_of(second, writer)["signal_reached"])
        self.assertEqual(self._budget_of(second, writer)["total"], 0)

        # And the second sprint's own signal is reached by its own two charges, no sooner.
        self._charge(writer, second, "red_ci", "signal-second-1")
        self.assertFalse(self._budget_of(second, writer)["signal_reached"])
        self._charge(writer, second, "red_ci", "signal-second-2")
        self.assertTrue(self._budget_of(second, writer)["signal_reached"])
        self.assertEqual(self._status_of(first), "open")
        self.assertEqual(self._status_of(second), "open")

    def test_a_hard_stop_stops_the_sprint_that_reached_it_and_not_the_other(self) -> None:
        writer = self._writer(signal=1, hard=2)
        first, second = self._pair()

        self._charge(writer, first, "blocked", "hard-first-1")
        self._charge(writer, first, "blocked", "hard-first-2")

        self.assertEqual(self._status_of(first), "stopped")
        self.assertEqual(self._status_of(second), "open")
        self.assertEqual(self._budget_of(second, writer)["total"], 0)
        self.assertFalse(self._budget_of(second, writer)["hard_reached"])
        self.assertEqual(
            [
                event["ref"]
                for event in SqlTaskAudit(self.client).events()
                if event["kind"] == "budget_hard_stopped"
            ],
            [first],
        )

        # The other sprint still charges, and stops on its own second event, not on the first.
        self._charge(writer, second, "red_ci", "hard-second-1")
        self.assertEqual(self._status_of(second), "open")
        self._charge(writer, second, "red_ci", "hard-second-2")

        self.assertEqual(self._status_of(second), "stopped")
        self.assertEqual(
            sorted(
                event["ref"]
                for event in SqlTaskAudit(self.client).events()
                if event["kind"] == "budget_hard_stopped"
            ),
            sorted([first, second]),
        )

    def test_closing_either_sprint_leaves_the_other_open(self) -> None:
        """Both orders, because closing the older one is not the only close that happens."""
        for closed_first in (True, False):
            with self.subTest(closes="first" if closed_first else "second"):
                self.setUp()
                first, second = self._pair()
                closing, remaining = (first, second) if closed_first else (second, first)

                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=closing,
                    decisions=close_decisions(self.writer, closing),
                )

                self.assertEqual(self._status_of(closing), "closed")
                self.assertEqual(self._status_of(remaining), "open")
                self.assertEqual(self._open_refs(), [remaining])
                # The sprint left open is still a sprint that writes: its budget still moves.
                self._charge(self.writer, remaining, "red_ci", "after-close")
                self.assertEqual(self._budget_of(remaining)["total"], 1)

    def test_closing_one_sprint_releases_its_reservations_and_holds_the_others(self) -> None:
        for closed_first in (True, False):
            with self.subTest(closes="first" if closed_first else "second"):
                self.setUp()
                first, second = self._pair()
                self.assertEqual(
                    active_sprint_projects(self.tmp.name),
                    {"ummanu": [first], "other": [second]},
                )
                closing, remaining = (first, second) if closed_first else (second, first)
                released = "ummanu" if closed_first else "other"
                held = "other" if closed_first else "ummanu"

                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=closing,
                    decisions=close_decisions(self.writer, closing),
                )

                self.assertEqual(active_sprint_projects(self.tmp.name), {held: [remaining]})
                # The released project is free for a new sprint; the held one is still refused.
                self._assert_refusal_left_nothing(
                    lambda held=held: self._third(projects=[held]),
                    "resource_conflict",
                    f"{held} held by {remaining}",
                )
                third = self._third(projects=[released])["sprint"]["ref"]
                self.assertEqual(
                    active_sprint_projects(self.tmp.name),
                    {held: [remaining], released: [third]},
                )

    def test_a_card_of_the_remaining_sprints_project_is_still_guarded_after_the_close(self) -> None:
        """The index is what the card guard reads, so the release is checked through it."""
        first, second = self._pair()
        tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        self.writer.close(role="po", actor="operator", reference=first, decisions=KEEP_THE_ISSUE_OPEN)

        # `ummanu` was released with its sprint, so an unrelated role may write there again.
        created = tasks.create(
            role="retro",
            actor="retro",
            project="ummanu",
            task_type="research",
            title="finding",
            target="issues",
            request_id="released-project",
        )
        self.assertEqual(created["task"]["project"], "ummanu")

        with self.assertRaisesRegex(TaskError, second) as denied:
            tasks.create(
                role="retro",
                actor="retro",
                project="other",
                task_type="research",
                title="finding",
                target="issues",
                request_id="held-project",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

    def _second_sprint_card(self, second: str) -> dict:
        """One Ready card of the second sprint, written by that sprint's own head."""
        with as_observer(second):
            return TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                project="other",
                task_type="code",
                title="the other sprint's work",
                target="ready",
                sprint=second,
                request_id="second-sprint-card",
            )["task"]

    def _denials(self) -> list[dict]:
        return [event for event in self._events() if event["kind"] == "sprint_guard_denied"]

    def test_an_observer_of_one_sprint_writes_nothing_of_the_other(self) -> None:
        """The identity half of the guard, across two sprints that share nothing.

        Product, reservations and repository roots are disjoint, so nothing but the caller's own
        binding stands between the first sprint's head and the second sprint's work. Card and
        entity are both refused, and each refusal is in the audit as an identity failure rather
        than as a role that is not permitted.
        """
        first, second = self._pair()
        card = self._second_sprint_card(second)
        tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        before = len(self._denials())
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": card["ref"],
            "dod_state": "open",
            "next_safe_step": "run tests",
        }

        with as_observer(first):
            calls = (
                (
                    "decide",
                    lambda: tasks.decide(
                        role="observer",
                        actor="observer",
                        reference=card["ref"],
                        kind="release",
                        body="releasing a card of a sprint I do not observe",
                        request_id="cross-sprint-decision",
                    ),
                ),
                (
                    "move",
                    lambda: tasks.move(
                        role="observer",
                        actor="observer",
                        reference=card["ref"],
                        target="blocked",
                        reason="blocking a card of a sprint I do not observe",
                        request_id="cross-sprint-move",
                    ),
                ),
                (
                    "resume",
                    lambda: self.writer.resume(
                        role="observer",
                        actor="observer",
                        reference=second,
                        entry=entry,
                        request_id="cross-sprint-resume",
                    ),
                ),
                # The card this points at is linked to the target sprint, which is what its own
                # head would pass: the link is a constraint on the value, not on the caller.
                (
                    "current-task",
                    lambda: self.writer.set_current_task(
                        role="observer",
                        actor="observer",
                        reference=second,
                        task_reference=card["ref"],
                        request_id="cross-sprint-current-task",
                    ),
                ),
            )
            for name, call in calls:
                with self.subTest(call=name), self.assertRaises(TaskError) as refused:
                    call()
                self.assertEqual(refused.exception.code, "observer_sprint_mismatch")

        denials = self._denials()[before:]
        self.assertEqual(
            [event["payload"]["code"] for event in denials],
            ["observer_sprint_mismatch"] * 4,
        )
        self.assertEqual({event["payload"]["sprint"] for event in denials}, {first})
        self.assertEqual([event["outcome"] for event in denials], ["denied"] * 4)
        self.assertEqual(
            [event["ref"] for event in denials],
            [card["ref"], card["ref"], second, second],
        )
        # Nothing moved: the card is where its own sprint left it, and the entity has neither a
        # resume entry nor a current task somebody else chose.
        self.assertEqual(TaskReader(self.client).show(card["ref"])["state"], "ready")  # type: ignore[arg-type]
        other = self.writer.reader.show(second, include_cards=False)
        self.assertIsNone(other["resume"])
        self.assertIsNone(other["current_task"])

    def test_a_head_nobody_bound_writes_nothing_at_all(self) -> None:
        """Fail-closed: an unbound caller cannot prove which sprint it is, so it is not one."""
        first, second = self._pair()
        card = self._second_sprint_card(second)
        tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        before = len(self._denials())

        with unbound_observer():
            with self.assertRaises(TaskError) as moved:
                tasks.move(
                    role="observer",
                    actor="observer",
                    reference=card["ref"],
                    target="blocked",
                    reason="from a head nobody bound",
                    request_id="unbound-move",
                )
            with self.assertRaises(TaskError) as resumed:
                self.writer.resume(
                    role="observer",
                    actor="observer",
                    reference=first,
                    entry={
                        "selected_step": "implement",
                        "selected_why": "needed",
                        "rejected_alternatives": "wait",
                        "current_task": card["ref"],
                        "dod_state": "open",
                        "next_safe_step": "run tests",
                    },
                    request_id="unbound-resume",
                )
            with self.assertRaises(TaskError) as pointed:
                self.writer.set_current_task(
                    role="observer",
                    actor="observer",
                    reference=second,
                    task_reference=card["ref"],
                    request_id="unbound-current-task",
                )

        self.assertEqual(moved.exception.code, "observer_identity_unbound")
        self.assertEqual(resumed.exception.code, "observer_identity_unbound")
        self.assertEqual(pointed.exception.code, "observer_identity_unbound")
        self.assertEqual(
            [event["payload"]["code"] for event in self._denials()[before:]],
            ["observer_identity_unbound"] * 3,
        )

    def test_a_bound_head_still_writes_its_own_sprint(self) -> None:
        """The other side of the same guard: nothing changes for the sprint's own observer."""
        first, second = self._pair()
        card = self._second_sprint_card(second)

        with as_observer(second):
            blocked = TaskWriter(self.client, data_dir=self.tmp.name).move(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                reference=card["ref"],
                target="blocked",
                reason="its own head blocking its own card",
                request_id="own-sprint-move",
            )
            recorded = self.writer.resume(
                role="observer",
                actor="observer",
                reference=second,
                entry={
                    "selected_step": "implement",
                    "selected_why": "needed",
                    "rejected_alternatives": "wait",
                    "current_task": card["ref"],
                    "dod_state": "open",
                    "next_safe_step": "run tests",
                },
                request_id="own-sprint-resume",
            )
            pointed = self.writer.set_current_task(
                role="observer",
                actor="observer",
                reference=second,
                task_reference=card["ref"],
                request_id="own-sprint-current-task",
            )

        self.assertEqual(blocked["task"]["state"], "blocked")
        self.assertEqual(recorded["sprint"]["resume"]["selected_step"], "implement")
        self.assertEqual(pointed["sprint"]["current_task"], card["ref"])
        self.assertEqual(self._denials(), [])
        self.assertEqual(first, "sprint:first")


class SprintTests(SprintFixture):
    def test_create_has_only_contract_fields_and_rejects_duplicate_reference(self) -> None:
        created = self._create(
            goal="Ship sprint entity",
            definition_of_done="tests pass",
            repositories=["ummanu", "ummanu"],
            projects=["ummanu", "ummanu"],
            reference="sprint:entity",
            request_id="create",
        )
        sprint = created["sprint"]
        # A declaration is canonicalized where it is written, so the row persists the
        # absolute root rather than the spelling the caller happened to use.
        self.assertEqual(sprint["repositories"], [str(Path("ummanu").resolve())])
        self.assertEqual(sprint["product"], "ummanu")
        self.assertEqual(sprint["issues"], ["issue:open"])
        self.assertEqual(sprint["reservations"], ["ummanu"])
        self.assertEqual(sprint["status"], "open")
        self.assertEqual(sprint["budget"]["total"], 0)
        self.assertEqual(sprint["budget"]["by_type"], {event: 0 for event in BUDGET_EVENT_TYPES})
        self.assertFalse(sprint["budget"]["signal_reached"])
        self.assertIsNone(sprint["current_task"])
        self.assertNotIn("title", sprint)
        # The reference is only reachable once the installation is free to open a sprint.
        self.writer.close(role="po", actor="operator", reference=sprint["ref"], decisions=KEEP_THE_ISSUE_OPEN)
        with self.assertRaisesRegex(TaskError, "already exists") as raised:
            self._create(goal="another", reference="sprint:entity")
        self.assertEqual(raised.exception.code, "validation")

    def test_restore_rewrites_a_closed_entity_and_refuses_foreign_fields(self) -> None:
        ref = self._create(goal="restore")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=ref, decisions=KEEP_THE_ISSUE_OPEN)
        self.arrange_card_sprint("ummanu-12", ref)

        with self.assertRaisesRegex(TaskError, "unknown sprint fields"):
            self.writer.restore(reference=ref, values={"claim": "worker"})

        result = self.writer.restore(
            reference=ref,
            values={"sprint_goal": "rewritten", "sprint_current_task": "ummanu-12"},
            request_id="restore-once",
        )
        replay = self.writer.restore(
            reference=ref,
            values={"sprint_goal": "rewritten", "sprint_current_task": "ummanu-12"},
            request_id="restore-once",
        )

        self.assertEqual(result["sprint"]["goal"], "rewritten")
        self.assertEqual(result["sprint"]["status"], "closed")
        self.assertEqual(result["sprint"]["current_task"], "ummanu-12")
        self.assertEqual(result["event_id"], replay["event_id"])

    def test_budget_is_validated_and_retry_is_one_event(self) -> None:
        ref = self._create(goal="budget")["sprint"]["ref"]
        with self.assertRaisesRegex(TaskError, "unknown budget"):
            self.writer.record_budget(role="po", actor="operator", reference=ref, event_type="green")
        first = self.writer.record_budget(
            role="po", actor="operator", reference=ref, event_type="red_ci", request_id="budget-once"
        )
        second = self.writer.record_budget(
            role="po", actor="operator", reference=ref, event_type="red_ci", request_id="budget-once"
        )
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["sprint"]["budget"]["total"], 1)
        self.assertEqual(second["sprint"]["budget"]["by_type"]["red_ci"], 1)
        events = SqlTaskAudit(self.client).events(reference=ref)
        self.assertEqual([event["kind"] for event in events], ["created", "budget_recorded"])

    def _budget_of(self, reference: str) -> dict:
        return self.writer.reader.show(reference, include_cards=False)["budget"]

    def test_infrastructure_outcome_is_counted_apart_and_spends_nothing(self) -> None:
        """An infrastructure bring-up is visible in the sprint and moves no threshold."""
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = self._create(goal="infrastructure outcomes")["sprint"]["ref"]

        for index in range(3):
            writer.record_budget(
                role="dispatcher",
                actor="dispatcher",
                reference=ref,
                event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
                request_id=f"infra-{index}",
                source_event_id=f"evt-infra-{index}",
            )

        budget = writer.reader.show(ref, include_cards=False)["budget"]
        self.assertEqual(budget["uncharged"], {BUDGET_UNCHARGED_INFRASTRUCTURE: 3})
        self.assertEqual(budget["total"], 0)
        self.assertEqual(budget["by_type"], {event: 0 for event in BUDGET_EVENT_TYPES})
        self.assertFalse(budget["signal_reached"])
        self.assertFalse(budget["hard_reached"])
        # Three of them under a hard limit of one: the sprint is still open, and no hard stop
        # was written.
        self.assertEqual(writer.reader.show(ref, include_cards=False)["status"], "open")
        self.assertEqual(
            [event["kind"] for event in SqlTaskAudit(self.client).events(reference=ref)],
            ["created"] + ["budget_recorded"] * 3,
        )
        self.assertEqual(
            writer.reader.status(ref)["budget"]["uncharged"],
            {BUDGET_UNCHARGED_INFRASTRUCTURE: 3},
        )

    def test_a_task_class_block_still_charges_beside_an_infrastructure_one(self) -> None:
        ref = self._create(goal="both classes")["sprint"]["ref"]

        self.writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
            request_id="infra-one",
        )
        for index, event_type in enumerate(BUDGET_EVENT_TYPES):
            self.writer.record_budget(
                role="dispatcher",
                actor="dispatcher",
                reference=ref,
                event_type=event_type,
                request_id=f"charge-{index}",
            )

        budget = self._budget_of(ref)
        self.assertEqual(budget["by_type"], {event: 1 for event in BUDGET_EVENT_TYPES})
        self.assertEqual(budget["total"], len(BUDGET_EVENT_TYPES))
        self.assertEqual(budget["uncharged"], {BUDGET_UNCHARGED_INFRASTRUCTURE: 1})

    def test_a_sprint_stored_without_uncharged_counts_reads_them_as_zero(self) -> None:
        """A sprint written before this quantity existed reads back, and reads back as zero."""
        ref = self._create(goal="stored before")["sprint"]["ref"]
        self.arrange_metadata(
            ref,
            sprint_budget=json.dumps({"by_type": {"blocked": 2, "red_ci": 1}}),
        )

        budget = self._budget_of(ref)
        self.assertEqual(budget["total"], 3)
        self.assertEqual(budget["by_type"]["blocked"], 2)
        self.assertEqual(
            budget["uncharged"],
            {event: 0 for event in BUDGET_UNCHARGED_EVENT_TYPES},
        )

        # And the next infrastructure outcome starts from that zero rather than failing on it.
        self.writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
            request_id="infra-after-legacy",
        )
        self.assertEqual(self._budget_of(ref)["uncharged"][BUDGET_UNCHARGED_INFRASTRUCTURE], 1)
        self.assertEqual(self._budget_of(ref)["total"], 3)

    def test_a_hard_stop_keeps_the_uncharged_counts_it_did_not_compute(self) -> None:
        """The hard-stop edge rewrites the charged budget; the counts beside it survive it."""
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = self._create(goal="hard stop with infrastructure")["sprint"]["ref"]
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type=BUDGET_UNCHARGED_INFRASTRUCTURE,
            request_id="infra-before-stop",
        )

        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )

        sprint = writer.reader.show(ref, include_cards=False)
        self.assertEqual(sprint["status"], "stopped")
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 1)
        self.assertEqual(sprint["budget"]["total"], 1)
        self.assertEqual(sprint["budget"]["uncharged"], {BUDGET_UNCHARGED_INFRASTRUCTURE: 1})

    def test_hard_budget_stop_has_its_own_durable_event(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="hard limit",
            product="ummanu",
            issues=["issue:open"],
            projects=["ummanu"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]

        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )

        events = SqlTaskAudit(self.client).events(reference=ref)
        self.assertEqual(
            [event["kind"] for event in events],
            ["created", "sprint.stopped", "budget_recorded", "budget_hard_stopped"],
        )
        self.assertEqual(events[-1]["payload"]["reason"], "budget_hard_limit")
        self.assertEqual(events[-1]["payload"]["source_event_id"], "evt-card-blocked")

    def test_hard_stop_replay_keeps_its_stored_related_refs_after_card_archive(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="stable hard replay",
            product="ummanu",
            issues=["issue:open"],
            projects=["ummanu"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        bind_observer(self, ref)
        card = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="replay-linked-card",
        )["task"]
        first = writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="stable-hard",
        )
        TaskWriter(self.client, data_dir=self.tmp.name).archive(  # type: ignore[arg-type]
            role="po",
            actor="operator",
            reference=card["ref"],
            reason="ordinary archive",
            request_id="archive-linked-card",
        )

        replay = writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="stable-hard",
        )

        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(replay["sprint"]["status"], "stopped")

    def test_close_of_a_stopped_sprint_uses_the_host_lifecycle_edge(self) -> None:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        ref = writer.create(
            role="po",
            actor="operator",
            goal="close stopped",
            product="ummanu",
            issues=["issue:open"],
            projects=["ummanu"],
            observer=head_choice("codex-observer"),
        )["sprint"]["ref"]
        writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=ref,
            event_type="blocked",
            request_id="stop-before-close",
        )

        first = writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="close-stopped",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        replay = writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="close-stopped",
            decisions=KEEP_THE_ISSUE_OPEN,
        )

        self.assertEqual(first["event_id"], replay["event_id"])
        self.assertEqual(first["sprint"]["status"], "closed")
        typed = writer.audit.committed_event("close-stopped:typed-close")
        assert typed is not None
        self.assertEqual(typed["transition"], {"source": "stopped", "target": "closed"})

    def test_lifecycle_events_carry_the_checked_edge_and_available_links(self) -> None:
        ref = self._create(goal="typed lifecycle", reference="sprint:typed-lifecycle")["sprint"]["ref"]
        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="typed-close",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        self.writer.reopen(
            role="po",
            actor="operator",
            reference=ref,
            observer=head_choice("codex-observer"),
            request_id="typed-reopen",
        )

        events = {
            event["kind"]: event
            for event in SqlTaskAudit(self.client).events(reference=ref)
            if event.get("record_type") == "board.protocol_event"
        }
        self.assertEqual(events["sprint.closed"]["subject"], {"kind": "sprint", "ref": ref})
        self.assertEqual(events["sprint.closed"]["actor"], {"role": "po", "id": "operator"})
        self.assertEqual(events["sprint.closed"]["reason"], "Sprint closed")
        self.assertEqual(events["sprint.closed"]["transition"], {"source": "open", "target": "closed"})
        self.assertEqual(events["sprint.closed"]["related_refs"], ["product:ummanu", "issue:open"])
        self.assertEqual(events["sprint.reopened"]["transition"], {"source": "closed", "target": "open"})
        self.assertEqual(
            events["sprint.reopened"]["data"],
            {
                "observer": encode_observer(head_choice("codex-observer")),
                "request_related_refs": [],
            },
        )

    def test_budget_thresholds_reject_hard_limit_below_signal(self) -> None:
        with self.assertRaisesRegex(TaskError, "hard threshold"):
            budget_thresholds({"sprint_budget": {"signal": 3, "hard": 2}})

    def test_task_link_is_live_metadata_and_closed_sprint_rejects_writes(self) -> None:
        ref = self._create(goal="link")["sprint"]["ref"]
        task_writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        task_writer.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="linked-card",
        )
        self.assertEqual(TaskReader(self.client).list(sprint=ref)[0]["sprint"], ref)  # type: ignore[arg-type]
        shown = SprintReader(self.client).show(ref)  # type: ignore[arg-type]
        self.assertEqual([card["ref"] for card in shown["cards"]], ["ummanu-13"])
        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            decisions=drop_cards("ummanu-13"),
        )
        # A comment is admitted on a closed sprint and changes nothing else about it
        # (issue:9eee1d8ee505bc4ecdc2): adding the outcome after the fact is what a PO does, and
        # this assertion used to be the refusal that sent one past the protocol into the board.
        self.writer.comment(role="worker", actor="worker", reference=ref, body="late")
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")  # type: ignore[arg-type]
        with self.assertRaisesRegex(TaskError, "closed"):
            task_writer.create(
                role="po",
                actor="operator",
                project="ummanu",
                task_type="code",
                title="late",
                target="ready",
                sprint=ref,
            )

    def test_task_creation_requires_an_open_reserved_sprint_and_rejects_priority_before_writes(self) -> None:
        ref = self._create(goal="binding")["sprint"]["ref"]
        writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

        # An unlinked card in a project the sprint reserves is answered by the reservation
        # guard, not by the admission rule; the admission rule is what a project outside every
        # reservation still meets. Both are asked of the sprint's observer: since secretary-1641
        # the PO may cut a card outside every sprint, and whether it runs is the dispatcher's
        # admission; since secretary-1709 the steward creates in Ready no more.
        for kwargs, code in (
            ({"project": "other", "role": "observer", "actor": "observer"}, "validation"),
            ({"role": "observer", "actor": "observer"}, "sprint_write_forbidden"),
            ({"sprint": ref, "project": "other"}, "sprint_project_unreserved"),
            ({"sprint": ref, "priority": "P1"}, "validation"),
        ):
            before = (
                self.sprint(ref),
                TaskReader(self.client).list(project="ummanu"),  # type: ignore[arg-type]
            )
            arguments = {
                "role": "po",
                "actor": "operator",
                "project": "ummanu",
                "task_type": "code",
                "title": "rejected",
                "target": "ready",
                **kwargs,
            }
            with as_observer(ref), self.assertRaises(TaskError) as raised:
                writer.create(**arguments)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(
                (
                    self.sprint(ref),
                    TaskReader(self.client).list(project="ummanu"),  # type: ignore[arg-type]
                ),
                before,
            )

    def test_close_propagates_a_terminal_archive_refusal_without_leaving_a_transaction(self) -> None:
        ref = self._create(goal="terminal refusal")["sprint"]["ref"]
        writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        done = writer.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="done",
            target="ready",
            sprint=ref,
            request_id="terminal-done",
        )["task"]
        writer.claim(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            worker="worker",
            request_id="terminal-claim",
        )
        writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            target="validate",
            reason="",
            request_id="terminal-validate",
        )
        writer.move(
            role="dispatcher",
            actor="dispatcher",
            reference=done["ref"],
            target="done",
            reason="",
            request_id="terminal-done-move",
        )

        with mock.patch.object(TaskWriter, "archive", side_effect=TaskError("live_work", "live worker", 3)):
            with self.assertRaises(TaskError) as raised:
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=ref,
                    request_id="terminal-close",
                    decisions=KEEP_THE_ISSUE_OPEN,
                )

        self.assertEqual(raised.exception.code, "live_work")
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(
            [event["kind"] for event in SqlTaskAudit(self.client).events(reference=ref)],
            ["created"],
        )

    def test_cli_create_and_list_return_stable_json(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "create",
                    "--role",
                    "po",
                    "--data-dir",
                    self.tmp.name,
                    "--instance",
                    str(self.instance),
                    "--goal",
                    "CLI sprint",
                    "--repository",
                    "ummanu",
                    "--product",
                    "ummanu",
                    "--issue",
                    "issue:open",
                    "--project",
                    "ummanu",
                    "--request-id",
                    "cli-create",
                    "--observer",
                    "codex-observer",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["sprint"]["repositories"], [str(Path("ummanu").resolve())])
        self.assertEqual(result["sprint"]["product"], "ummanu")
        self.assertEqual(result["sprint"]["issues"], ["issue:open"])
        self.assertEqual(result["sprint"]["reservations"], ["ummanu"])

    def test_cli_observer_can_set_current_task(self) -> None:
        ref = self._create(goal="observer current task")["sprint"]["ref"]
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
        )["task"]
        output, errors = io.StringIO(), io.StringIO()

        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "current-task",
                    "--ref",
                    ref,
                    "--role",
                    "observer",
                    "--actor",
                    "observer",
                    "--task",
                    task["ref"],
                    "--instance",
                    str(self.instance),
                    "--data-dir",
                    self.tmp.name,
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["sprint"]["current_task"], task["ref"])

    def test_resume_requires_all_fields_and_staleness_uses_card_audit(self) -> None:
        ref = self._create(goal="resume")["sprint"]["ref"]
        with self.assertRaisesRegex(TaskError, "missing required fields"):
            self.writer.resume(role="po", actor="operator", reference=ref, entry={"selected_step": "x"})
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "next card",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
            "recorded_at": "2000-01-01T00:00:00Z",
        }
        self.writer.resume(role="po", actor="operator", reference=ref, entry=entry, request_id="resume")
        fresh = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]
        self.assertTrue(fresh["resume_freshness"]["fresh"])
        task_writer = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        task = task_writer.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="resume-card",
        )["task"]
        SqlTaskAudit(self.client).append(
            "later",
            {
                "event_id": "evt_later",
                "request_id": "later",
                "ref": task["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )
        stale = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]
        self.assertFalse(stale["resume_freshness"]["fresh"])
        self.assertEqual(stale["resume_freshness"]["error"], "resume_stale")

    def test_naive_resume_timestamp_is_rejected_and_legacy_data_fails_closed(self) -> None:
        ref = self._create(goal="naive resume")["sprint"]["ref"]
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "next card",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
            "recorded_at": "2026-07-29T12:00:00",
        }
        with self.assertRaisesRegex(TaskError, "must include a timezone"):
            self.writer.resume(role="observer", actor="observer", reference=ref, entry=entry)

        self.arrange_metadata(ref, sprint_resume=json.dumps(entry))
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="naive-card",
        )["task"]
        SqlTaskAudit(self.client).append(
            "naive-event",
            {
                "event_id": "evt_naive_event",
                "request_id": "naive-event",
                "ref": task["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )

        shown = SprintReader(self.client, data_dir=self.tmp.name).show(ref)  # type: ignore[arg-type]

        self.assertFalse(shown["resume_freshness"]["fresh"])
        self.assertEqual(shown["resume_freshness"]["error"], "resume_stale")
        self.assertIsNone(shown["resume_freshness"]["lag_seconds"])

    def test_observer_can_record_a_complete_resume_entry(self) -> None:
        ref = self._create(goal="observer resume")["sprint"]["ref"]
        entry = {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "ummanu-14",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
        }

        result = self.writer.resume(
            role="observer",
            actor="observer-head",
            reference=ref,
            entry=entry,
            request_id="observer-resume",
            delivery_id="delivery-1",
            through_event="evt-card-1",
        )

        self.assertEqual(result["action"], "resume_recorded")
        self.assertEqual(result["sprint"]["resume"]["selected_step"], "implement")
        self.assertNotIn("delivery_id", result["sprint"]["resume"])
        resume_event = SqlTaskAudit(self.client).events(reference=ref)[-1]
        self.assertEqual(resume_event["payload"]["delivery_id"], "delivery-1")
        self.assertEqual(resume_event["payload"]["through_event"], "evt-card-1")
        with self.assertRaisesRegex(TaskError, "requires both"):
            self.writer.resume(
                role="observer",
                actor="observer-head",
                reference=ref,
                entry=entry,
                delivery_id="delivery-2",
            )
        with self.assertRaisesRegex(TaskError, "only an observer"):
            self.writer.resume(
                role="po",
                actor="operator",
                reference=ref,
                entry=entry,
                delivery_id="delivery-2",
                through_event="evt-card-2",
            )

    def test_resume_freshness_ignores_denied_and_failed_card_events(self) -> None:
        ref = self._create(goal="event predicate")["sprint"]["ref"]
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=ref,
            request_id="predicate-card",
        )["task"]
        entry = {
            "selected_step": "wait",
            "selected_why": "no durable transition",
            "rejected_alternatives": "act",
            "current_task": task["ref"],
            "dod_state": "open",
            "next_safe_step": "wait",
        }
        self.writer.resume(role="observer", actor="observer", reference=ref, entry=entry)
        audit = SqlTaskAudit(self.client)
        baseline = SprintReader(self.client, data_dir=self.tmp.name).show(ref)["resume_freshness"]  # type: ignore[arg-type]
        for request_id, kind, outcome in (
            ("predicate-denied", "sprint_guard_denied", "denied"),
            ("predicate-guard-success", "sprint_guard_denied", "success"),
            ("predicate-failed", "commented", "failed"),
            ("predicate-missing-outcome", "commented", ""),
        ):
            audit.append(
                request_id,
                {
                    "event_id": "evt_" + request_id,
                    "request_id": request_id,
                    "ref": task["ref"],
                    "kind": kind,
                    "outcome": outcome,
                    "occurred_at": "2099-01-01T00:00:00Z",
                },
            )

        freshness = SprintReader(self.client, data_dir=self.tmp.name).show(ref)["resume_freshness"]  # type: ignore[arg-type]

        self.assertTrue(freshness["fresh"])
        self.assertEqual(freshness["last_event_at"], baseline["last_event_at"])


class SprintStatusHeadlessCommandTests(SprintFixture):
    """`ummanu sprint status` must not answer healthy for a card nobody is working on.

    This is the command the observer skill opens with and the one the runbooks name, read by the
    actor who creates this state. Round 5 shipped `degraded_cards` on the projection and never
    passed the map into it from here, so this command answered `{}` for every sprint — an
    affirmative claim of health. The earlier test called `SprintReader._status` directly, which is
    exactly why it did not catch that; this one goes through the CLI.
    """

    def _headless_card(self, sprint: str) -> str:
        """One Pipeline card of this sprint, with a production record that names no worker."""
        card = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="a card whose worker is gone",
            sprint=sprint,
        )["task"]["ref"]
        dispatcher = Path(self.tmp.name) / "dispatcher"
        dispatcher.mkdir(parents=True, exist_ok=True)
        (dispatcher / "production-state.json").write_text(
            json.dumps(
                {
                    "records": {
                        card: {
                            "state": "adopted",
                            "worker_headless": {
                                "since": 1_700_000_000.0,
                                "record_state": "adopted",
                                "handle_known": False,
                                "heartbeat": "absent",
                                "workspace": "/work/card",
                                "branch": f"pipeline/{card}",
                                "expected_branch": f"pipeline/{card}",
                                "dirty": False,
                                "candidate_sha": "6cc7ca0c8cdf0719629e1e01bb5c72614983d7ef",
                                "report_generation": 1,
                                "recovery_error": "round_already_answered",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return card

    def _worked_card(self, sprint: str) -> str:
        """One card of this sprint whose worker the dispatcher's record does name."""
        card = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="a card somebody is working",
            sprint=sprint,
        )["task"]["ref"]
        dispatcher = Path(self.tmp.name) / "dispatcher"
        dispatcher.mkdir(parents=True, exist_ok=True)
        (dispatcher / "production-state.json").write_text(
            json.dumps({"records": {card: {"state": "working", "worker": "head-1"}}}),
            encoding="utf-8",
        )
        return card

    def _status_json(self, ref: str) -> dict:
        output, errors = io.StringIO(), io.StringIO()
        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "status",
                    "--ref",
                    ref,
                    "--data-dir",
                    self.tmp.name,
                    "--instance",
                    str(self.instance),
                ]
            )
        self.assertEqual(code, 0, errors.getvalue())
        return json.loads(output.getvalue())

    def test_the_command_names_the_sprints_headless_cards(self) -> None:
        ref = self._create(goal="a sprint with a headless card")["sprint"]["ref"]
        card = self._headless_card(ref)

        section = self._status_json(ref)["work"]["degraded_cards"]

        self.assertEqual(section["source"]["state"], "available")
        self.assertIn(card, section["items"], "the command must not answer healthy")
        degraded = section["items"][card]
        self.assertEqual(degraded["state"], "adopted")
        self.assertFalse(degraded["handle_known"])
        self.assertEqual(degraded["candidate_sha"], "6cc7ca0c8cdf0719629e1e01bb5c72614983d7ef")
        self.assertEqual(degraded["recovery_error"], "round_already_answered")

    def test_the_command_says_nothing_when_every_card_owns_its_worker(self) -> None:
        """The control: `degraded_cards` empty must mean observed-healthy, not never-asked.

        Which the section now says for itself: an empty mapping under an `available` source is the
        answer "asked, and nothing is degraded", and the production state nobody could read has
        `items: null` under an `unavailable` one.
        """
        ref = self._create(goal="a sprint whose cards are worked")["sprint"]["ref"]
        self._worked_card(ref)

        section = self._status_json(ref)["work"]["degraded_cards"]

        self.assertEqual(section["source"]["state"], "available")
        self.assertEqual(section["items"], {})

    def test_a_production_state_nobody_can_read_is_not_an_empty_degraded_list(self) -> None:
        """And the other half of the same distinction, which the old shape could not express."""
        ref = self._create(goal="a sprint whose dispatcher state is gone")["sprint"]["ref"]

        section = self._status_json(ref)["work"]["degraded_cards"]

        self.assertEqual(section["source"]["state"], "unavailable")
        self.assertIsNone(section["items"])


class SprintAuditTraversalTests(SprintFixture):
    """A mass sprint summary costs one audit traversal, not one per sprint."""

    @contextlib.contextmanager
    def _traversals(self):
        """Count the committed-audit traversals a block performs."""
        counter: dict[str, int] = {"count": 0}
        original = SqlTaskAudit.events

        def counting(audit: SqlTaskAudit, *args: Any, **kwargs: Any) -> list[dict]:
            counter["count"] += 1
            return original(audit, *args, **kwargs)

        with mock.patch.object(SqlTaskAudit, "events", counting):
            yield counter

    def _resumed(self, goal: str) -> str:
        """An open sprint holding a recorded resume, ready to be judged for freshness."""
        ref = self._create(goal=goal)["sprint"]["ref"]
        self.writer.resume(
            role="po",
            actor="operator",
            reference=ref,
            request_id=f"resume-{goal}",
            entry={
                "selected_step": "implement",
                "selected_why": "needed",
                "rejected_alternatives": "wait",
                "current_task": "next card",
                "dod_state": "tests pending",
                "next_safe_step": "run tests",
                "recorded_at": "2000-01-01T00:00:00Z",
            },
        )
        return ref

    def _entry(self, recorded_at: str = "2000-01-01T00:00:00Z") -> dict[str, str]:
        return {
            "selected_step": "implement",
            "selected_why": "needed",
            "rejected_alternatives": "wait",
            "current_task": "next card",
            "dod_state": "tests pending",
            "next_safe_step": "run tests",
            "recorded_at": recorded_at,
        }

    def _terminal(self, reference: str, *, status: str, recorded_at: str = "2000-01-01T00:00:00Z") -> str:
        """A closed or stopped sprint carrying a resume, seeded straight onto the board.

        A terminal sprint holds no reservation, so a live board accumulates them beside the
        open one; the writer cannot open a second sprint over the fixture's project.  The real
        `close`, hard-budget stop and restore transitions are traced separately, over rows this
        fixture's writer produces itself.
        """
        self.writer.restore_create(
            reference=reference,
            goal="seeded",
            definition_of_done="done",
            repositories=["ummanu"],
            observer=head_choice("codex-observer"),
            status=status,
            request_id=f"fixture-{reference}",
        )
        self.arrange_metadata(
            reference,
            sprint_current_task="",
            sprint_resume=json.dumps(self._entry(recorded_at)),
        )
        return reference

    def _sprint_event(self, reference: str, request_id: str, occurred_at: str) -> None:
        """A significant sprint-scoped event: it would age the resume of an open sprint."""
        SqlTaskAudit(self.client).append(
            request_id,
            {
                "event_id": f"evt_{request_id.replace('-', '_')}",
                "request_id": request_id,
                "ref": reference,
                "kind": "budget_recorded",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"event_type": "red_ci"},
                "occurred_at": occurred_at,
            },
        )

    def _reader(self) -> SprintReader:
        return SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    @contextlib.contextmanager
    def _round_trips(self):
        """Count the board round trips a block performs, a batched read counting as the one it is."""
        trips: list[str] = []
        client = type(self.client)
        original_call, original_batch = client.call, client.call_batch
        batching = False

        def counting_call(fake, method: str, **params: Any) -> Any:
            if not batching:
                trips.append(method)
            return original_call(fake, method, **params)

        def counting_batch(fake, calls: Any) -> Any:
            nonlocal batching
            calls = list(calls)
            trips.append("batch:" + ",".join(sorted({method for method, _ in calls})))
            batching = True
            try:
                return original_batch(fake, calls)
            finally:
                batching = False

        with (
            mock.patch.object(client, "call", counting_call),
            mock.patch.object(client, "call_batch", counting_batch),
        ):
            yield trips

    def test_mass_sprint_status_reads_the_audit_once_for_one_and_for_many_sprints(self) -> None:
        open_ref = self._resumed("audit traversal")
        task = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=open_ref,
            request_id="traversal-card",
        )["task"]
        SqlTaskAudit(self.client).append(
            "traversal-later",
            {
                "event_id": "evt_traversal_later",
                "request_id": "traversal-later",
                "ref": task["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )

        with self._traversals() as single:
            one = self._reader().statuses()

        for index in range(4):
            self._terminal(f"sprint:seeded-{index}", status="closed" if index % 2 else "stopped")

        with self._traversals() as many:
            all_sprints = self._reader().statuses()

        self.assertEqual(len(one), 1)
        self.assertEqual(len(all_sprints), 5)
        self.assertEqual(single["count"], 1)
        # The cost is the traversal, not the sprint count: five summaries read the journal once.
        self.assertEqual(many["count"], 1)
        for summary in (one[0], next(item for item in all_sprints if item["ref"] == open_ref)):
            # The open sprint still sees the significant later linked-card event.
            self.assertFalse(summary["resume_freshness"]["fresh"])
            self.assertEqual(summary["resume_freshness"]["error"], "resume_stale")
            self.assertEqual(summary["resume_freshness"]["last_event_at"], "2099-01-01T00:00:00Z")

    def test_sprint_list_reads_no_audit_and_a_single_status_reads_it_once(self) -> None:
        ref = self._resumed("single status")

        with self._traversals() as listing:
            listed = self._reader().list()
        with self._traversals() as single:
            summary = self._reader().status(ref)

        self.assertEqual([sprint["ref"] for sprint in listed], [ref])
        self.assertEqual(listing["count"], 0)
        self.assertEqual(single["count"], 1)
        self.assertTrue(summary["resume_freshness"]["fresh"])

    def test_closed_and_stopped_summaries_keep_the_documented_freshness_shape(self) -> None:
        closed = self._terminal("sprint:closed-summary", status="closed")
        stopped = self._terminal("sprint:stopped-summary", status="stopped")
        # Events that would age an open sprint's resume.  A terminal sprint is judged by its own
        # frozen record, so they are never read: the whole summary costs no traversal at all.
        self._sprint_event(closed, "closed-later", "2099-01-01T00:00:00Z")
        self._sprint_event(stopped, "stopped-later", "2099-01-01T00:00:00Z")

        with self._traversals() as traversals:
            summaries = {item["ref"]: item for item in self._reader().statuses()}

        self.assertEqual(traversals["count"], 0)
        self.assertEqual(summaries[closed]["status"], "closed")
        self.assertEqual(summaries[stopped]["status"], "stopped")
        self.assertEqual(summaries[stopped]["stop_reason"], "budget_hard_limit")
        self.assertIsNone(summaries[closed]["stop_reason"])
        for ref in (closed, stopped):
            freshness = summaries[ref]["resume_freshness"]
            self.assertEqual(
                sorted(freshness),
                ["error", "fresh", "lag_seconds", "last_event_at", "recorded_at", "threshold_seconds"],
            )
            self.assertEqual(freshness["recorded_at"], "2000-01-01T00:00:00Z")
            self.assertIsNone(freshness["last_event_at"])
            self.assertEqual(freshness["lag_seconds"], 0)
            self.assertTrue(freshness["fresh"])
            self.assertIsNone(freshness["error"])

    def test_an_all_terminal_installation_summarises_without_touching_the_audit(self) -> None:
        """The acceptance case: only closed and stopped rows, every one with a valid resume."""
        refs = [
            self._terminal(f"sprint:terminal-{index}", status="closed" if index % 2 else "stopped")
            for index in range(6)
        ]
        for index, ref in enumerate(refs):
            self._sprint_event(ref, f"terminal-later-{index}", "2099-01-01T00:00:00Z")

        with self._traversals() as mass:
            summaries = {item["ref"]: item for item in self._reader().statuses()}
        with self._traversals() as single:
            direct = self._reader().status(refs[0])

        self.assertEqual(sorted(summaries), sorted(refs))
        self.assertEqual(mass["count"], 0)
        self.assertEqual(single["count"], 0)
        for freshness in [item["resume_freshness"] for item in summaries.values()] + [
            direct["resume_freshness"]
        ]:
            self.assertTrue(freshness["fresh"])
            self.assertIsNone(freshness["error"])
            self.assertIsNone(freshness["last_event_at"])

    def test_every_terminal_transition_freezes_freshness_on_the_sprint_record(self) -> None:
        """Close, hard-budget stop and restore all reach the same read-time rule."""
        closing = self._resumed("close transition")
        card = TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="linked",
            target="ready",
            sprint=closing,
            request_id="close-card",
        )["task"]
        SqlTaskAudit(self.client).append(
            "close-later",
            {
                "event_id": "evt_close_later",
                "request_id": "close-later",
                "ref": card["ref"],
                "kind": "moved",
                "outcome": "success",
                "actor": {"role": "dispatcher"},
                "payload": {"to": "assessment"},
                "occurred_at": "2099-01-01T00:00:00Z",
            },
        )

        with self._traversals() as while_open:
            open_summary = self._reader().status(closing)
        self.writer.close(
            role="po",
            actor="operator",
            reference=closing,
            request_id="close-it",
            decisions=drop_cards(card["ref"]),
        )
        with self._traversals() as after_close:
            closed_summary = self._reader().status(closing)

        # Open: one traversal and the later card event is seen.  Closed: the same record, no read.
        self.assertEqual(while_open["count"], 1)
        self.assertEqual(open_summary["resume_freshness"]["error"], "resume_stale")
        self.assertEqual(open_summary["resume_freshness"]["last_event_at"], "2099-01-01T00:00:00Z")
        self.assertEqual(after_close["count"], 0)
        self.assertEqual(closed_summary["status"], "closed")
        self.assertTrue(closed_summary["resume_freshness"]["fresh"])
        self.assertEqual(closed_summary["resume_freshness"]["recorded_at"], "2000-01-01T00:00:00Z")
        # The record cannot move after the transition, which is what makes it the durable source.
        with self.assertRaisesRegex(TaskError, "sprint is closed"):
            self.writer.resume(
                role="po",
                actor="operator",
                reference=closing,
                request_id="resume-after-close",
                entry=self._entry("2030-01-01T00:00:00Z"),
            )

        stopping = self._resumed("stop transition")
        budget_writer = SprintWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmp.name,
            instance=self.instance,
            thresholds={"signal": 1, "hard": 1},
        )
        budget_writer.record_budget(
            role="dispatcher",
            actor="dispatcher",
            reference=stopping,
            event_type="blocked",
            request_id="hard-stop",
            source_event_id="evt-card-blocked",
        )

        with self._traversals() as after_stop:
            stopped_summary = self._reader().status(stopping)

        self.assertEqual(stopped_summary["status"], "stopped")
        self.assertEqual(stopped_summary["stop_reason"], "budget_hard_limit")
        # The stop itself writes a significant sprint event, and it still ages nothing.
        self.assertEqual(after_stop["count"], 0)
        self.assertTrue(stopped_summary["resume_freshness"]["fresh"])
        self.assertEqual(stopped_summary["resume_freshness"]["recorded_at"], "2000-01-01T00:00:00Z")

        restored = self.writer.restore_create(
            reference="sprint:restored",
            goal="restored",
            status="closed",
            request_id="restore-create",
        )["sprint"]["ref"]
        self.writer.restore(
            reference=restored,
            values={"sprint_resume": json.dumps(self._entry("2001-01-01T00:00:00Z"))},
            request_id="restore-resume",
        )
        self._sprint_event(restored, "restored-later", "2099-01-01T00:00:00Z")

        with self._traversals() as after_restore:
            restored_summary = self._reader().status(restored)

        self.assertEqual(restored_summary["status"], "closed")
        self.assertEqual(after_restore["count"], 0)
        self.assertTrue(restored_summary["resume_freshness"]["fresh"])
        self.assertEqual(restored_summary["resume_freshness"]["recorded_at"], "2001-01-01T00:00:00Z")

    def test_a_reopened_sprint_is_judged_against_the_audit_again(self) -> None:
        """The rule reads the sprint's state, so it is not sticky once the sprint reopens."""
        ref = self._resumed("reopen transition")
        self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="close-before-reopen",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        self._sprint_event(ref, "reopen-later", "2099-01-01T00:00:00Z")

        with self._traversals() as closed:
            closed_summary = self._reader().status(ref)
        self.writer.reopen(
            role="po",
            actor="operator",
            reference=ref,
            request_id="reopen-it",
            observer=head_choice("codex-observer"),
        )
        with self._traversals() as reopened:
            reopened_summary = self._reader().status(ref)

        self.assertEqual(closed["count"], 0)
        self.assertTrue(closed_summary["resume_freshness"]["fresh"])
        self.assertEqual(reopened["count"], 1)
        self.assertEqual(reopened_summary["resume_freshness"]["error"], "resume_stale")
        self.assertEqual(reopened_summary["resume_freshness"]["last_event_at"], "2099-01-01T00:00:00Z")

    def test_a_missing_resume_still_answers_without_reading_the_audit(self) -> None:
        ref = self._create(goal="no resume")["sprint"]["ref"]

        with self._traversals() as traversals:
            summary = self._reader().status(ref)

        self.assertEqual(traversals["count"], 0)
        self.assertEqual(summary["resume_freshness"]["error"], "resume_missing")
        self.assertFalse(summary["resume_freshness"]["fresh"])


class SprintSingleWriterGuardTests(SprintBackendFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.client = self.make_sprint_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sprints = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        # The guard is about card writes against an open sprint, not about opening one, so the
        # sprint is seeded through the restore route instead of `create`.
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
        # Restore publishes the reservation through the writer; seed the derived index the way
        # a live installation reconstructs it from persisted sprint state.
        refresh_active_sprint_projects(self.tmp.name, SprintReader(self.client))  # type: ignore[arg-type]
        bind_observer(self, self.ref)

    def test_observer_must_link_to_its_open_sprint_and_other_roles_are_denied(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="owned",
            sprint=self.ref,
            request_id="observer-create",
        )["task"]
        self.assertEqual(card["sprint"], self.ref)
        with self.assertRaisesRegex(TaskError, self.ref) as missing:
            self.tasks.create(
                role="observer",
                actor="observer",
                project="ummanu",
                task_type="code",
                title="unlinked",
                request_id="observer-unlinked",
            )
        self.assertEqual(missing.exception.code, "sprint_write_forbidden")
        with self.assertRaisesRegex(TaskError, self.ref) as retro:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="ummanu",
                task_type="research",
                title="finding",
                target="issues",
                request_id="retro-denied",
            )
        self.assertEqual(retro.exception.code, "sprint_write_forbidden")
        denied = [
            event for event in SqlTaskAudit(self.client).events() if event["kind"] == "sprint_guard_denied"
        ]
        self.assertEqual(len(denied), 2)
        self.assertEqual(denied[0]["payload"]["sprint"], self.ref)

    def test_po_override_requires_reason_and_is_audited_once(self) -> None:
        with self.assertRaisesRegex(TaskError, "non-empty reason") as missing:
            self.tasks.create(
                role="po",
                actor="operator",
                project="ummanu",
                task_type="code",
                title="urgent",
                sprint_override=True,
                request_id="override-empty",
            )
        self.assertEqual(missing.exception.code, "validation")
        first = self.tasks.create(
            role="po",
            actor="operator",
            project="ummanu",
            task_type="code",
            title="urgent",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="override-once",
        )
        second = self.tasks.create(
            role="po",
            actor="operator",
            project="ummanu",
            task_type="code",
            title="urgent",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="override-once",
        )
        self.assertEqual(first["event_id"], second["event_id"])
        event = next(
            event for event in SqlTaskAudit(self.client).events() if event["request_id"] == "override-once"
        )
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")

    def test_linked_task_still_obeys_the_held_project_guard(self) -> None:
        # Since secretary-1709 the steward, like retro, creates only proposals in Issues (besides
        # its report); a proposal linked to the holding sprint meets the same guard as a PO card.
        for role, actor, target, request_id in (
            ("po", "operator", "ready", "linked-po-denied"),
            ("steward", "steward", "issues", "linked-steward-denied"),
        ):
            with self.subTest(role=role), self.assertRaises(TaskError) as raised:
                self.tasks.create(
                    role=role,
                    actor=actor,
                    project="ummanu",
                    task_type="code",
                    title="guarded",
                    target=target,
                    sprint=self.ref,
                    request_id=request_id,
                )
            self.assertEqual(raised.exception.code, "sprint_write_forbidden")

        created = self.tasks.create(
            role="po",
            actor="operator",
            project="ummanu",
            task_type="code",
            title="overridden",
            sprint=self.ref,
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="linked-po-override",
        )
        event = next(
            event
            for event in SqlTaskAudit(self.client).events()
            if event["request_id"] == "linked-po-override"
        )
        self.assertEqual(created["task"]["sprint"], self.ref)
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")
        denied = [
            event for event in SqlTaskAudit(self.client).events() if event["kind"] == "sprint_guard_denied"
        ]
        self.assertEqual(
            [event["payload"]["operation_request_id"] for event in denied],
            ["linked-po-denied", "linked-steward-denied"],
        )

    def test_po_cannot_edit_a_held_card_without_an_audited_override(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.edit(role="po", actor="operator", reference=card["ref"], description="outside edit")
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        edited = self.tasks.edit(
            role="po",
            actor="operator",
            reference=card["ref"],
            description="incident edit",
            sprint_override=True,
            sprint_override_reason="production incident",
        )
        self.assertEqual(edited["task"]["description"], "incident edit")
        event = SqlTaskAudit(self.client).events()[-1]
        self.assertEqual(event["payload"]["sprint_override_reason"], "production incident")

    def test_override_retry_reuses_the_denied_request_id_for_the_write(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.move(
                role="po",
                actor="operator",
                reference=card["ref"],
                target="blocked",
                reason="",
                request_id="po-override-retry",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

        moved = self.tasks.move(
            role="po",
            actor="operator",
            reference=card["ref"],
            target="blocked",
            reason="",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="po-override-retry",
        )

        self.assertEqual(moved["task"]["state"], "blocked")
        events = SqlTaskAudit(self.client).events()
        denial = next(event for event in events if event["kind"] == "sprint_guard_denied")
        self.assertEqual(denial["payload"]["operation_request_id"], "po-override-retry")
        success = next(event for event in events if event["request_id"] == "po-override-retry")
        self.assertEqual(success["record_type"], "board.protocol_event")
        self.assertEqual(success["transition"], {"source": "ready", "target": "blocked"})
        # The typed event describes the lifecycle edge; the authority the PO used to make it is
        # its own generic record, and the journal still answers who overrode the sprint and why.
        grant = next(event for event in events if event["kind"] == "sprint_guard_override")
        self.assertEqual(grant["payload"]["sprint_override_reason"], "production incident")
        self.assertEqual(grant["payload"]["operation_request_id"], "po-override-retry")
        self.assertEqual(grant["payload"]["sprint"], self.ref)

    def test_every_move_path_through_the_sprint_guard_records_its_decision(self) -> None:
        """Ordinary, refused and granted-override moves all pass through the one guard.

        A first-attempt override never produces a denial, so before this it was the one decision
        the guard made that left nothing behind at all.
        """
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]

        ordinary = self.tasks.move(
            role="dispatcher",
            actor="d",
            reference=card["ref"],
            target="in_progress",
            reason="",
            request_id="guard-ordinary-move",
        )
        with self.assertRaisesRegex(TaskError, self.ref) as refused:
            self.tasks.move(
                role="po",
                actor="operator",
                reference=card["ref"],
                target="blocked",
                reason="",
                request_id="guard-refused-move",
            )
        granted = self.tasks.move(
            role="po",
            actor="operator",
            reference=card["ref"],
            target="blocked",
            reason="",
            sprint_override=True,
            sprint_override_reason="production incident",
            request_id="guard-granted-move",
        )

        self.assertEqual(ordinary["task"]["state"], "in_progress")
        self.assertEqual(refused.exception.code, "sprint_write_forbidden")
        self.assertEqual(granted["task"]["state"], "blocked")
        decisions = {
            str(event["payload"].get("operation_request_id")): event["kind"]
            for event in SqlTaskAudit(self.client).events()
            if event["kind"] in {"sprint_guard_denied", "sprint_guard_override"}
        }
        self.assertEqual(
            decisions,
            {
                "guard-refused-move": "sprint_guard_denied",
                "guard-granted-move": "sprint_guard_override",
            },
        )

    def test_a_granted_override_is_recorded_once_and_survives_reconcile(self) -> None:
        """The grant is idempotent under retry and is a repairable pending record like the denial."""
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="owned",
            sprint=self.ref,
        )["task"]

        def override_move() -> dict[str, object]:
            return self.tasks.move(
                role="po",
                actor="operator",
                reference=card["ref"],
                target="blocked",
                reason="",
                sprint_override=True,
                sprint_override_reason="production incident",
                request_id="override-recorded-once",
            )

        with mock.patch.object(self.tasks.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "audit repair") as pending:
                override_move()
        self.assertEqual(pending.exception.code, "audit_pending")
        self.assertEqual(self.tasks.reader.show(card["ref"])["state"], "ready")

        self.assertEqual(self.tasks.reconcile(), (1, 0))
        first = override_move()
        second = override_move()

        self.assertEqual(first["event_id"], second["event_id"])
        grants = [
            event for event in SqlTaskAudit(self.client).events() if event["kind"] == "sprint_guard_override"
        ]
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["payload"]["sprint_override_reason"], "production incident")

    def test_denied_create_request_can_succeed_after_sprint_closes(self) -> None:
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="ummanu",
                task_type="research",
                title="finding",
                target="issues",
                request_id="retro-after-close",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        self.sprints.close(role="po", actor="operator", reference=self.ref)

        created = self.tasks.create(
            role="retro",
            actor="retro",
            project="ummanu",
            task_type="research",
            title="finding",
            target="issues",
            request_id="retro-after-close",
        )

        self.assertEqual(created["task"]["project"], "ummanu")
        self.assertEqual(created["task"]["state"], "issues")

    def test_dispatcher_cycle_and_observer_move_are_allowed(self) -> None:
        card = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="cycle",
            sprint=self.ref,
        )["task"]
        self.assertEqual(card["state"], "ready")
        self.tasks.claim(role="dispatcher", actor="dispatcher", reference=card["ref"], worker="worker")
        result = self.tasks.move(
            role="dispatcher", actor="dispatcher", reference=card["ref"], target="validate", reason=""
        )
        self.assertEqual(result["task"]["state"], "validate")

    def test_missing_index_bootstraps_from_live_open_sprints(self) -> None:
        (Path(self.tmp.name) / "sprints" / "active-repositories.json").unlink()
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(
                role="steward",
                actor="steward",
                project="ummanu",
                task_type="code",
                title="blocked",
                target="issues",
            )
        self.assertEqual(denied.exception.code, "sprint_write_forbidden")

    def _steward_report(self, slug: str) -> dict:
        """A report created through the steward's own port, the chain its tick runs."""
        board = StewardReportBoard(TaskReader(self.client), self.tasks, actor="steward")  # type: ignore[arg-type]
        reference = board.create_report(project="ummanu", title=f"steward: {slug}", slug=slug)
        return TaskReader(self.client).show(reference)  # type: ignore[arg-type]

    def test_steward_writes_its_own_report_on_a_reserved_project(self) -> None:
        """secretary-1712: the steward's report is its tick's accounting, not the holding sprint's work."""
        done = self._steward_report("steward-sweep-done")
        blocked = self._steward_report("steward-sweep-blocked")
        self.assertEqual((done["state"], done["sprint"]), ("in_progress", None))
        # Created In progress for the steward's own tick, it is never the dispatcher's to claim.
        with self.assertRaises(TaskError) as claimed:
            self.tasks.claim(role="dispatcher", actor="dispatcher", reference=done["ref"], worker="worker")
        self.assertEqual(claimed.exception.code, "claim_conflict")
        board = StewardReportBoard(TaskReader(self.client), self.tasks, actor="steward")  # type: ignore[arg-type]

        for card, target in ((done, "done"), (blocked, "blocked")):
            self.tasks.comment(
                role="steward",
                actor="steward",
                reference=card["ref"],
                body=f"report for {target}",
                request_id=f"report-comment-{target}",
            )
            board.move_report(reference=card["ref"], target=target, reason=f"sweep closed as {target}")
            self.assertEqual(TaskReader(self.client).show(card["ref"])["state"], target)  # type: ignore[arg-type]

        events = SqlTaskAudit(self.client).events()
        self.assertEqual([event for event in events if event["kind"] == "sprint_guard_denied"], [])
        written = [event for event in events if event.get("ref") in {done["ref"], blocked["ref"]}]
        self.assertTrue(written)
        for event in written:
            self.assertEqual(event["actor"], {"role": "steward", "id": "steward"})
            self.assertNotIn("sprint_override_reason", event.get("payload") or {})

    def test_steward_writes_on_a_non_report_card_stay_refused(self) -> None:
        unlinked = self.tasks.create(
            role="po",
            actor="operator",
            project="ummanu",
            task_type="research",
            title="not a report",
            request_id="po-unlinked",
        )["task"]
        self.tasks.claim(role="dispatcher", actor="dispatcher", reference=unlinked["ref"], worker="worker")
        linked = self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title="sprint work",
            sprint=self.ref,
            request_id="observer-linked",
        )["task"]

        for card in (unlinked, linked):
            with self.subTest(card=card["ref"]), self.assertRaisesRegex(TaskError, self.ref) as denied:
                self.tasks.move(
                    role="steward",
                    actor="steward",
                    reference=card["ref"],
                    target="blocked",
                    reason="escalated by the steward",
                )
            self.assertEqual(denied.exception.code, "sprint_write_forbidden")

        denied_events = [
            event for event in SqlTaskAudit(self.client).events() if event["kind"] == "sprint_guard_denied"
        ]
        self.assertEqual(len(denied_events), 2)

    def test_sql_unique_live_reservation_rolls_back_an_unrepresentable_overlap(self) -> None:
        """SQL rejects the impossible duplicate reservation without a partial restore."""
        other_ref = self.sprints.restore_create(
            reference="sprint:overlap",
            goal="overlap",
            repositories=["ummanu"],
            request_id="seed-overlap-sprint",
        )["sprint"]["ref"]
        with self.assertRaises(TaskError) as raised:
            self.sprints.restore(
                reference=other_ref,
                values={"sprint_reservations": json.dumps(["ummanu"])},
                request_id="seed-overlap-reservation",
            )
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertIsNone(self.sprints.audit.event("seed-overlap-reservation"))
        self.assertEqual(
            self.client._query(
                "SELECT sprint_ref FROM sprint_projects WHERE project_id=%s AND reserved",
                ("ummanu",),
            ),
            [(self.ref,)],
        )


class SprintReservedProjectGuardTests(SprintBackendFixture, unittest.TestCase):
    """The guards compare a card's project against reservations, not repository paths.

    A live sprint's `repositories` are filesystem paths and its `reservations` are project
    ids, so a fixture where the two lists differ is what tells the two key spaces apart.
    """

    def setUp(self) -> None:
        self.client = self.make_sprint_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sprints = SprintWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.ref = self.sprints.restore_create(
            reference="sprint:reserved",
            goal="reserved projects",
            repositories=["/home/dev/ummanu"],
            request_id="seed-reserved-sprint",
        )["sprint"]["ref"]
        self.sprints.restore(
            reference=self.ref,
            values={"sprint_reservations": json.dumps(["ummanu"])},
            request_id="seed-reserved-project",
        )
        refresh_active_sprint_projects(self.tmp.name, SprintReader(self.client))  # type: ignore[arg-type]
        bind_observer(self, self.ref)

    def _card(self, title: str = "owned") -> dict:
        return self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title=title,
            sprint=self.ref,
        )["task"]

    def test_index_is_keyed_by_reserved_project(self) -> None:
        self.assertEqual(active_sprint_projects(self.tmp.name), {"ummanu": [self.ref]})

    def test_a_stale_repository_keyed_index_is_rebuilt_before_it_answers(self) -> None:
        path = Path(self.tmp.name) / "sprints" / "active-repositories.json"
        path.write_text(
            json.dumps({"version": 1, "repositories": {"/home/dev/ummanu": [self.ref]}}),
            encoding="utf-8",
        )
        self.assertEqual(active_sprint_projects(self.tmp.name), {})

        with self.assertRaises(TaskError) as denied:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="ummanu",
                task_type="research",
                title="finding",
                target="issues",
            )

        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {
                "version": 2,
                "projects": {"ummanu": [self.ref]},
            },
        )

    def test_observer_moves_and_edits_a_card_of_its_reserved_project(self) -> None:
        card = self._card()

        moved = self.tasks.move(
            role="observer",
            actor="observer",
            reference=card["ref"],
            target="blocked",
            reason="waiting on review",
        )
        edited = self.tasks.edit(
            role="observer",
            actor="observer",
            reference=card["ref"],
            description="revised spec",
        )

        self.assertEqual(moved["task"]["state"], "blocked")
        self.assertEqual(edited["task"]["description"], "revised spec")

    def test_observer_may_not_move_a_card_of_an_unreserved_project(self) -> None:
        outside = self.tasks.create(
            role="retro",
            actor="retro",
            project="other",
            task_type="research",
            title="outside",
            target="issues",
        )["task"]

        with self.assertRaises(TaskError) as denied:
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=outside["ref"],
                target="ready",
                reason="",
            )

        self.assertEqual(denied.exception.code, "role_forbidden")

    def test_the_reservation_guard_denies_an_unauthorized_write(self) -> None:
        with self.assertRaisesRegex(TaskError, self.ref) as denied:
            self.tasks.create(
                role="retro",
                actor="retro",
                project="ummanu",
                task_type="research",
                title="finding",
                target="issues",
                request_id="retro-denied",
            )

        self.assertEqual(denied.exception.code, "sprint_write_forbidden")
        events = [
            event for event in SqlTaskAudit(self.client).events() if event["kind"] == "sprint_guard_denied"
        ]
        self.assertEqual([event["payload"]["sprint"] for event in events], [self.ref])

    def test_a_project_no_sprint_reserves_is_unaffected(self) -> None:
        created = self.tasks.create(
            role="retro",
            actor="retro",
            project="other",
            task_type="research",
            title="finding",
            target="issues",
        )

        self.assertEqual(created["task"]["project"], "other")
        self.assertEqual(active_sprint_projects(self.tmp.name), {"ummanu": [self.ref]})


class SprintCloseDecisionTests(SprintFixture):
    """A close decides every issue the sprint declared and every card it still holds."""

    def setUp(self) -> None:
        super().setUp()
        # A second open issue of the same product, so a close can decide two issues
        # differently and the refusal has more than one ref to be silent about.
        self.second_issue = self.arrange_issue("second", product="ummanu")["ref"]
        self.tasks = TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    def _open(self, **kwargs) -> str:
        kwargs.setdefault("issues", ["issue:open", self.second_issue])
        return self._create(goal="decided close", **kwargs)["sprint"]["ref"]

    def _card(self, sprint: str, title: str, request_id: str) -> str:
        return self.tasks.create(
            role="observer",
            actor="observer",
            project="ummanu",
            task_type="code",
            title=title,
            target="ready",
            sprint=sprint,
            request_id=request_id,
        )["task"]["ref"]

    def _store(self):

        return ProductIssueStore(self.client, data_dir=self.tmp.name, instance=self.instance)

    def _contract_state(self, reference: str) -> dict[str, object]:
        store = self._store()
        return {
            "sprint": self.sprint(reference),
            "issues": store.list_issues(product="ummanu", include_closed=True),
            "events": list(self._events()),
            "transactions": self.transaction_state(),
        }

    def test_a_close_short_of_an_issue_verdict_refuses_and_writes_nothing(self) -> None:
        ref = self._open()
        before = self._contract_state(ref)

        with self.assertRaises(TaskError) as raised:
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions={"issues": [{"ref": "issue:open", "verdict": "resolved", "reason": "landed"}]},
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertIn(self.second_issue, raised.exception.message)
        self.assertNotIn("issue:open,", raised.exception.message)
        self.assertEqual(self._contract_state(ref), before)
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]

    def test_a_verdict_for_an_issue_the_sprint_never_declared_is_refused(self) -> None:
        ref = self._open(issues=["issue:open"])
        before = self._contract_state(ref)

        with self.assertRaisesRegex(TaskError, "did not declare"):
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions={
                    "issues": [
                        {"ref": "issue:open", "verdict": "open", "reason": "unfinished"},
                        {"ref": self.second_issue, "verdict": "resolved", "reason": "not this sprint's"},
                    ]
                },
            )

        self.assertEqual(self._contract_state(ref), before)

    def test_a_closing_verdict_closes_the_issue_and_a_kept_one_stays_open_with_its_basis(self) -> None:
        ref = self._open()

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="decided-close",
            decisions={
                "issues": [
                    {"ref": "issue:open", "verdict": "resolved", "reason": "the fix landed in this sprint"},
                    {"ref": self.second_issue, "verdict": "open", "reason": "only half of it was reached"},
                ]
            },
        )

        store = self._store()
        closed = store.show_issue("issue:open")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["close_reason"], "resolved")
        self.assertFalse(store.show_issue(self.second_issue)["closed"])
        # Closed with its reason where an operator reads issues, and gone from the open list.
        self.assertEqual(
            sorted(
                item["ref"]
                for item in store.list_issues(product="ummanu", include_closed=True)
                if item["closed"]
            ),
            ["issue:done", "issue:open"],
        )
        self.assertNotIn("issue:open", [item["ref"] for item in store.list_issues(product="ummanu")])
        self.assertEqual(result["closed_issues"], ["issue:open"])
        # The basis of the issue left open is on the close itself, which is where the audit
        # keeps it: nothing about the issue record says why the sprint let it stand.
        recorded = next(
            event for event in self._events() if event["kind"] == "closed" and event["ref"] == ref
        )
        self.assertEqual(
            recorded["payload"]["decisions"]["issues"],
            sorted(
                [
                    {
                        "ref": "issue:open",
                        "verdict": "resolved",
                        "reason": "the fix landed in this sprint",
                    },
                    {
                        "ref": self.second_issue,
                        "verdict": "open",
                        "reason": "only half of it was reached",
                    },
                ],
                key=lambda item: item["ref"],
            ),
        )

    def test_a_close_short_of_a_disposition_names_the_cards_and_their_states(self) -> None:
        ref = self._open(issues=["issue:open"])
        ready = self._card(ref, "still ready", "undisposed-ready")
        blocked = self._card(ref, "still blocked", "undisposed-blocked")
        with as_observer(ref):
            self.tasks.move(
                role="observer",
                actor="observer",
                reference=blocked,
                target="blocked",
                reason="waiting on an answer",
                request_id="undisposed-block",
            )
        before = self._contract_state(ref)

        with self.assertRaises(TaskError) as raised:
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions=KEEP_THE_ISSUE_OPEN,
            )

        self.assertEqual(raised.exception.code, "validation")
        self.assertIn(f"{ready} (ready)", raised.exception.message)
        self.assertIn(f"{blocked} (blocked)", raised.exception.message)
        self.assertEqual(self._contract_state(ref), before)
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]

    def test_dispositions_take_every_card_into_a_recorded_end(self) -> None:
        ref = self._open(issues=["issue:open"])
        landed = self._card(ref, "landed after all", "disposed-done")
        dropped = self._card(ref, "will not be done", "disposed-drop")

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="disposing-close",
            decisions={
                "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
                "cards": [
                    {"ref": landed, "verdict": "done", "reason": "merged in the last hour of the sprint"},
                    {"ref": dropped, "verdict": "drop", "reason": "superseded by the next sprint's cut"},
                ],
            },
        )

        self.assertEqual(result["disposed_tasks"], sorted([landed, dropped]))
        self.assertEqual({item["ref"] for item in result["cleanup"]}, {landed, dropped})
        self.assertTrue(all(item["status"] == "pending" for item in result["cleanup"]))
        # No card of the sprint is left in a working state on the closed contract.
        self.assertEqual(TaskReader(self.client).list(sprint=ref), [])  # type: ignore[arg-type]
        for reference in (landed, dropped):
            self.assertFalse(self.record_is_active(reference))
        self.assertIn(
            f"[po]\ndone when sprint {ref} closed: merged in the last hour of the sprint",
            self.record_comments(landed),
        )
        self.assertIn(
            f"archived when sprint {ref} closed: merged in the last hour of the sprint",
            "\n".join(self.record_comments(landed)),
        )

    def test_close_and_retry_leave_previously_archived_cards_and_cleanup_unchanged(self) -> None:
        ref = self._open(issues=["issue:open"])
        cards = {}
        for name in ("drop", "retained", "active"):
            cards[name] = self.tasks.create(
                role="observer", actor="observer", project="ummanu", task_type="code",
                title=name, description=f"exact {name} description\nКириллица", target="ready",
                sprint=ref, request_id=f"history-close-create-{name}",
            )["task"]["ref"]
        self.tasks.move(
            role="po", actor="operator", reference=cards["retained"], target="done",
            reason="completed before close", sprint_override=True,
            sprint_override_reason="PO records the completed work",
            request_id="history-close-done",
        )
        archived = {cards["drop"], cards["retained"]}
        for reference in sorted(archived):
            self.tasks.archive(
                role="po", actor="operator", reference=reference, reason="archived before close",
                request_id=f"history-close-archive-{reference}",
            )
        reader = TaskReader(self.client)
        cleanup = CleanupJournal(Path(self.tmp.name))

        def archived_state():
            intents = cleanup.read()["intents"]
            return {
                reference: {
                    "task": reader.show(reference),
                    "comments": self.record_comments(reference),
                    "events": self.tasks.audit.events(reference=reference),
                    "cleanup": {key: intent for key, intent in intents.items()
                                if intent["task"]["ref"] == reference},
                }
                for reference in archived
            }

        def cli_history(*flags):
            output = io.StringIO()
            with (mock.patch("ummanu.task_commands.card_client", return_value=self.client),
                  contextlib.redirect_stdout(output)):
                code = main(["task", "list", "--instance", str(self.instance),
                             "--sprint", ref, *flags])
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())

        before = archived_state()
        for name, state in (("drop", "ready"), ("retained", "done")):
            saved = before[cards[name]]
            self.assertEqual(saved["task"]["description"], f"exact {name} description\nКириллица")
            self.assertEqual((saved["task"]["state"], saved["task"]["closed"]), (state, True))
            self.assertEqual(sum(event["kind"] == "archived" for event in saved["events"]), 1)
            self.assertEqual(len(saved["cleanup"]), 1)
        self.assertEqual(self.sprint(ref)["status"], "open")
        self.assertEqual({card["ref"] for card in cli_history()}, set(cards.values()))
        self.assertEqual([card["ref"] for card in cli_history("--state", "done", "--project", "ummanu")],
                         [cards["retained"]])
        self.assertEqual(cli_history("--project", "other"), [])
        self.assertEqual([card["ref"] for card in reader.list(sprint=ref)], [cards["active"]])
        self.assertEqual([card["ref"] for card in self.sprint(ref, include_cards=True)["cards"]],
                         [cards["active"]])
        self.assertEqual(self.writer._host().read(EntityKind.SPRINT, ref).card_refs, (cards["active"],))
        self.assertEqual(archived_state(), before)

        decisions = {
            "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
            "cards": [{"ref": cards["active"], "verdict": "done", "reason": "last active target landed"}],
        }
        first = self.writer.close(
            role="po", actor="operator", reference=ref, decisions=decisions,
            request_id="history-close",
        )
        self.assertEqual(first["sprint"]["status"], "closed")
        self.assertEqual(first["sprint"]["cards"], [])
        self.assertEqual(first["disposed_tasks"], [cards["active"]])
        self.assertEqual(first["archived_tasks"], [])
        event = self.writer.audit.committed_event("history-close")
        self.assertEqual(event["payload"]["targets"], {
            "archive": [], "remaining": [cards["active"]],
            "remaining_states": {cards["active"]: "ready"},
        })
        typed = self.writer.audit.committed_event("history-close:typed-close")
        self.assertEqual(typed["related_refs"], ["product:ummanu", "issue:open"])
        self.assertEqual(archived_state(), before)
        self.assertEqual((reader.show(cards["active"])["state"], reader.show(cards["active"])["closed"]),
                         ("done", True))
        self.assertIn("[po]\ndone when sprint " + ref + " closed: last active target landed",
                      self.record_comments(cards["active"]))
        self.assertEqual({card["ref"] for card in cli_history()}, set(cards.values()))
        self.assertEqual({item["ref"] for item in cleanup.summary(sprint=ref)}, set(cards.values()))
        self.assertEqual(len(cleanup.summary(sprint=ref)), 3)
        events_after_close = self._events()
        cleanup_after_close = cleanup.read()
        active_after_close = reader.show(cards["active"])
        active_comments_after_close = self.record_comments(cards["active"])
        with mock.patch.object(self.writer, "_close_targets", side_effect=AssertionError("reconstructed targets")):
            replay = self.writer.close(
                role="po", actor="operator", reference=ref, decisions=decisions,
                request_id="history-close",
            )
        self.assertEqual(replay, first)
        self.assertEqual(archived_state(), before)
        self.assertEqual(self._events(), events_after_close)
        self.assertEqual(cleanup.read(), cleanup_after_close)
        self.assertEqual(reader.show(cards["active"]), active_after_close)
        self.assertEqual(self.record_comments(cards["active"]), active_comments_after_close)
        self.assertEqual(self.transaction_state(), {"ok": True, "pending": 0})

    def test_the_observer_closes_its_own_sprint_end_to_end_in_its_own_name(self) -> None:
        """secretary-1765: the observer bound to its sprint closes it on a decisions file, on SQL.

        Nothing of the close is mocked: the Done card is archived, the Ready card is dropped and
        archived, the declared issue is closed as resolved, and the sprint reaches `closed`, each
        written with role `observer`.
        """
        from ummanu.sprint_close import parse_close_decisions

        ref = self._open(issues=["issue:open"])
        landed = self._card(ref, "landed", "observer-close-landed")
        self.tasks.claim(
            role="dispatcher", actor="dispatcher", reference=landed, worker="worker", request_id="landed-claim"
        )
        for target in ("validate", "done"):
            self.tasks.move(
                role="dispatcher",
                actor="dispatcher",
                reference=landed,
                target=target,
                reason="",
                request_id=f"landed-{target}",
            )
        dropped = self._card(ref, "not in this sprint", "observer-close-dropped")
        decisions = parse_close_decisions(
            "issues:\n"
            "  - {ref: 'issue:open', verdict: resolved, reason: 'the fix landed'}\n"
            "cards:\n"
            f"  - {{ref: '{dropped}', verdict: drop, reason: 'the next sprint cuts it again'}}\n"
        )
        before = {event["event_id"] for event in self._events()}

        with as_observer(ref):
            result = self.writer.close(
                role="observer",
                actor="observer",
                reference=ref,
                request_id="observer-close",
                reason="the goal is reached",
                decisions=decisions,
            )

        self.assertEqual(self.sprint(ref)["status"], "closed")
        self.assertEqual(result["closed_issues"], ["issue:open"])
        self.assertIn(landed, result["archived_tasks"])
        self.assertEqual(result["disposed_tasks"], [dropped])
        for reference in (landed, dropped):
            self.assertFalse(self.record_is_active(reference))
        issue = self._store().show_issue("issue:open")
        self.assertEqual((issue["closed"], issue["close_reason"]), (True, "resolved"))
        written = [event for event in self._events() if event["event_id"] not in before]
        # Every record this close wrote, the close itself, each step and the typed transitions,
        # names the observer: none of it is the PO's.
        self.assertTrue(written)
        self.assertEqual({event["actor"]["role"] for event in written}, {"observer"})
        self.assertEqual({event["actor"]["id"] for event in written}, {"observer"})
        kinds = {(event["kind"], event["ref"]) for event in written}
        self.assertIn(("closed", ref), kinds)
        self.assertIn(("sprint.closed", ref), kinds)
        self.assertIn(("issue.closed", "issue:open"), kinds)
        self.assertIn(("archived", landed), kinds)
        self.assertIn(("archived", dropped), kinds)
        self.assertEqual(self.writer.transactions.status(), {"ok": True, "pending": 0})

    def test_a_disposition_for_a_card_that_is_not_open_work_is_refused(self) -> None:
        ref = self._open(issues=["issue:open"])
        before = self._contract_state(ref)

        with self.assertRaisesRegex(TaskError, "not open work of this sprint"):
            self.writer.close(
                role="po",
                actor="operator",
                reference=ref,
                decisions={
                    "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
                    "cards": [{"ref": "product:ummanu", "verdict": "drop", "reason": "no"}],
                },
            )

        self.assertEqual(self._contract_state(ref), before)

    def test_no_verdict_makes_a_product_or_issue_record_a_close_target(self) -> None:
        ref = self._open(issues=["issue:open"])
        # Even malformed metadata cannot enrol a typed record in the close.
        self.arrange_pipeline_metadata("product:ummanu", sprint_ref=ref)
        self.arrange_pipeline_metadata("issue:open", sprint_ref=ref)

        result = self.writer.close(
            role="po",
            actor="operator",
            reference=ref,
            request_id="typed-records-close",
            decisions=KEEP_THE_ISSUE_OPEN,
        )

        self.assertEqual(result["disposed_tasks"], [])
        self.assertEqual(result["archived_tasks"], [])
        self.assertEqual(result["remaining_tasks"], [])
        for reference in ("product:ummanu", "issue:open"):
            self.assertTrue(self.record_is_active(reference))

    def test_cli_close_reads_its_decisions_from_a_file(self) -> None:
        """The command is a client of the operation, and prints what the operation answered.

        The decisions and the closeout arrive as files for the same reason: the reasons are prose,
        and prose is written before the command runs.
        """
        init_state_repo(self.instance)
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "cli card", "cli-disposed")
        closeout = Path(self.tmp.name) / "closeout.md"
        closeout.write_text(CLOSEOUT_BODY, encoding="utf-8")
        path = Path(self.tmp.name) / "decisions.yaml"
        path.write_text(
            "issues:\n"
            "  - ref: issue:open\n"
            "    verdict: wont_do\n"
            "    reason: the product moved on\n"
            "cards:\n"
            f"  - ref: {card}\n"
            "    verdict: drop\n"
            "    reason: nobody will pick this up\n",
            encoding="utf-8",
        )
        output, errors = io.StringIO(), io.StringIO()

        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "close",
                    "--ref",
                    ref,
                    "--role",
                    "po",
                    "--actor",
                    "operator",
                    "--data-dir",
                    self.tmp.name,
                    "--instance",
                    str(self.instance),
                    "--decisions-file",
                    str(path),
                    "--reason",
                    "the product moved on and the sprint is not worth extending",
                    "--closeout-file",
                    str(closeout),
                    "--request-id",
                    "cli-close",
                ]
            )

        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(code, 0)
        answer = json.loads(output.getvalue())
        self.assertEqual(answer["kind"], "sprint_closed")
        closed = answer["result"]["close"]
        self.assertEqual(closed["closed_issues"], ["issue:open"])
        self.assertEqual(closed["disposed_tasks"], [card])
        self.assertEqual(self._store().show_issue("issue:open")["close_reason"], "wont_do")
        # The command prints the operation's document, and that document refuses to read as a
        # satisfied contract.
        self.assertFalse(answer["definition_of_done"]["satisfied"])
        self.assertEqual(list_knowledge_documents(self.instance), (closed["closeout"]["document"],))

    def test_cli_close_without_the_file_refuses_before_it_writes(self) -> None:
        init_state_repo(self.instance)
        ref = self._open(issues=["issue:open"])
        closeout = Path(self.tmp.name) / "closeout.md"
        closeout.write_text(CLOSEOUT_BODY, encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()

        with (
            self.board_injected(),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "sprint",
                    "close",
                    "--ref",
                    ref,
                    "--role",
                    "po",
                    "--actor",
                    "operator",
                    "--data-dir",
                    self.tmp.name,
                    "--instance",
                    str(self.instance),
                    "--reason",
                    "closing without deciding the issue",
                    "--closeout-file",
                    str(closeout),
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")
        self.assertIn("issue:open", json.loads(errors.getvalue())["error"]["message"])
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")  # type: ignore[arg-type]

    def test_sql_rolled_back_claim_allows_a_changed_intent_as_a_new_request(self) -> None:
        """A SQL failure erases the claim, so the retry may state a new complete intent."""
        ref = self._open(issues=["issue:open"])
        card = self._card(ref, "disposed once", "restated-card")
        decisions = {
            "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
            "cards": [{"ref": card, "verdict": "drop", "reason": "not finished"}],
        }
        with (
            mock.patch.object(TaskWriter, "archive", side_effect=OSError("disk full")),
            self.assertRaises(TaskError) as raised,
        ):
            self.writer.close(
                role="po", actor="operator", reference=ref,
                request_id="restated-close", decisions=decisions,
            )
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertIsNone(self.writer.audit.event("restated-close"))
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "open")
        self.assertEqual([row["ref"] for row in TaskReader(self.client).list(sprint=ref)], [card])

        changed = {
            "issues": list(KEEP_THE_ISSUE_OPEN["issues"]),
            "cards": [{"ref": card, "verdict": "done", "reason": "landed after all"}],
        }
        result = self.writer.close(
            role="po", actor="operator", reference=ref,
            request_id="restated-close", decisions=changed,
        )
        self.assertEqual(result["archived_tasks"] + result["disposed_tasks"], [card])
        self.assertEqual(SprintReader(self.client).show(ref, include_cards=False)["status"], "closed")
        self.assertEqual(len(self.writer.audit.events(reference=ref, kind="closed")), 1)


class CloseDecisionFileTests(unittest.TestCase):
    """The decisions file is read strictly, and every refusal is a validation refusal."""

    def _refusal(self, text: str) -> TaskError:
        with self.assertRaises(TaskError) as raised:
            parse_close_decisions(text)
        self.assertEqual(raised.exception.code, "validation")
        return raised.exception

    def test_a_well_formed_file_normalizes_to_its_two_sections(self) -> None:
        parsed = parse_close_decisions(
            "issues:\n"
            "  - ref: issue:abc\n"
            "    verdict: duplicate\n"
            "    reason: 'the same as issue:def '\n"
            "cards:\n"
            "  - ref: ummanu-1\n"
            "    verdict: done\n"
            "    reason: merged\n"
        )

        self.assertEqual(
            parsed,
            {
                "issues": [{"ref": "issue:abc", "verdict": "duplicate", "reason": "the same as issue:def"}],
                "cards": [{"ref": "ummanu-1", "verdict": "done", "reason": "merged"}],
            },
        )

    def test_an_empty_file_decides_nothing_rather_than_failing_to_parse(self) -> None:
        self.assertEqual(parse_close_decisions(""), {"issues": [], "cards": []})

    def test_every_malformed_decision_is_refused_by_name(self) -> None:
        cases = [
            ("issues: [{ref: issue:a, verdict: nonsense, reason: why}]", "needs a verdict"),
            ("issues: [{ref: issue:a, verdict: resolved, reason: '  '}]", "non-empty reason"),
            ("issues: [{ref: issue:a, verdict: resolved}]", "non-empty reason"),
            ("issues: [{verdict: resolved, reason: why}]", "needs a ref"),
            (
                "cards: [{ref: c-1, verdict: drop, reason: a}, {ref: c-1, verdict: done, reason: b}]",
                "more than one decision",
            ),
            ("cards: [{ref: c-1, verdict: archived, reason: a}]", "needs a verdict"),
            ("cards: [{ref: c-1, verdict: drop, reason: a, decision: b}]", "unknown field"),
            ("verdicts: []", "unknown section"),
            ("- issue:a", "must be a mapping"),
            ("issues: 3", "must be a mapping"),
            ("issues: [\n", "not valid YAML"),
            # YAML types its scalars, so a key can be an integer sitting next to string ones.
            # That is a refusal by name, not a TypeError out of the parser.
            ("1: x\nunknown: x", "non-string key"),
            ("issues: [{1: x, unknown: x}]", "non-string key"),
            # A confirmation has to name the fact it confirms, and only a confirmation may.
            ("issues: [{ref: issue:a, verdict: already_closed, reason: why}]", "must name it in 'actual'"),
            (
                "issues: [{ref: issue:a, verdict: already_closed, reason: why, actual: ready}]",
                "must name it in 'actual'",
            ),
            (
                "cards: [{ref: c-1, verdict: already_moved, reason: why, actual: blocked}]",
                "must name it in 'actual'",
            ),
            (
                "issues: [{ref: issue:a, verdict: resolved, reason: why, actual: duplicate}]",
                "only a already_closed decision carries",
            ),
            (
                "cards: [{ref: c-1, verdict: drop, reason: why, actual: ready}]",
                "only a already_moved decision carries",
            ),
        ]
        for text, message in cases:
            with self.subTest(text=text):
                self.assertIn(message, self._refusal(text).message)


if __name__ == "__main__":
    unittest.main()
