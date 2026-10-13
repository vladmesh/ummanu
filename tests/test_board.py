"""Contract tests for the additive, backend-neutral board protocol."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from ummanu.board.card_transitions import (
    CARD_TRANSITIONS,
    CardTransitionForbidden,
    card_transition,
)
from ummanu.board.events import BoardEventCanon, BoardEventPending, MutationEventTransaction
from ummanu.board.fake import FakeBoardHost, MemoryAudit
from ummanu.board.host import Create, Replace, SprintSupplement, TransitionRequest
from ummanu.board.models import (
    Actor,
    Card,
    CardState,
    EntityKind,
    Event,
    EventKind,
    Issue,
    IssueState,
    Product,
    RelatedRefs,
    Sprint,
    SprintState,
)
from ummanu.board.sql_host import SqlBoardHost
from ummanu.board.transitions import TRANSITIONS, BoardProtocolError, InvalidTransition


class BoardHostContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeBoardHost()
        self.actor = Actor("worker", "worker-1417", "head-run:1417")

    def test_create_read_and_transition_return_normalized_event_result(self) -> None:
        created = self.host.create(
            Create(
                Card("ummanu-1417", "Protocol seam", CardState.READY, sprint_ref="sprint:943"),
                self.actor,
                "accepted into the sprint",
                RelatedRefs(("sprint:943",)),
            )
        )

        result = self.host.transition(
            TransitionRequest(
                EntityKind.CARD,
                "ummanu-1417",
                CardState.IN_PROGRESS,
                self.actor,
                "worker started",
                RelatedRefs(("sprint:943", "head-run:1417")),
            )
        )

        self.assertEqual(created.event.actor.head_run_ref, "head-run:1417")
        self.assertEqual(result.entity, self.host.read(EntityKind.CARD, "ummanu-1417"))
        self.assertEqual(result.entity.state, CardState.IN_PROGRESS)
        self.assertEqual(result.event.kind, EventKind.CARD_STARTED)
        self.assertEqual(result.event.related_refs.refs, ("sprint:943", "head-run:1417"))

    def test_same_state_card_transition_is_rejected_by_the_registry(self) -> None:
        self.host.create(
            Create(Card("ummanu-1417", "Protocol seam", CardState.READY), self.actor, "create")
        )

        with self.assertRaises(InvalidTransition):
            self.host.transition(
                TransitionRequest(
                    EntityKind.CARD,
                    "ummanu-1417",
                    CardState.READY,
                    self.actor,
                    "no lifecycle change",
                )
            )

    def test_fake_sprint_replay_preserves_lifecycle_evidence(self) -> None:
        sprint = Sprint("sprint:943", "Host lifecycle", SprintState.OPEN, "product:ummanu", ("issue:1",))
        host = FakeBoardHost([sprint])
        operation = TransitionRequest(
            EntityKind.SPRINT,
            sprint.ref,
            SprintState.CLOSED,
            self.actor,
            "Sprint closed",
            RelatedRefs(("product:ummanu", "issue:1")),
            "close-943",
        )

        first = host.transition(operation)
        replay = host.transition(operation)

        self.assertEqual(first.event, replay.event)
        self.assertEqual(first.event.source_state, "open")
        self.assertEqual(first.event.target_state, "closed")
        with self.assertRaises(ValueError):
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    sprint.ref,
                    SprintState.CLOSED,
                    self.actor,
                    "Sprint closed",
                    RelatedRefs(("product:ummanu", "issue:1")),
                    "close-943",
                    SprintSupplement(observer="different"),
                )
            )
        with self.assertRaises(ValueError):
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    sprint.ref,
                    SprintState.CLOSED,
                    self.actor,
                    "Sprint closed",
                    RelatedRefs(("product:ummanu", "head-run:changed")),
                    "close-943",
                )
            )

    def test_fake_sprint_pending_replay_rejects_changed_related_refs(self) -> None:
        sprint = Sprint("sprint:pending", "Host lifecycle", SprintState.OPEN)
        operation = TransitionRequest(
            EntityKind.SPRINT,
            sprint.ref,
            SprintState.CLOSED,
            self.actor,
            "Sprint closed",
            RelatedRefs(("head-run:first",)),
            "pending-close",
        )
        audit = MemoryAudit()
        host = FakeBoardHost([sprint], audit=audit)
        with mock.patch.object(host.canon.audit, "append", side_effect=OSError("disk full")), self.assertRaises(BoardEventPending):
            host.transition(operation)

        with self.assertRaises(ValueError):
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    sprint.ref,
                    SprintState.CLOSED,
                    self.actor,
                    "Sprint closed",
                    RelatedRefs(("head-run:changed",)),
                    "pending-close",
                )
            )
        self.assertEqual(host.transition(operation).entity.state, SprintState.CLOSED)

    def test_fake_host_commits_each_sprint_edge_and_recovers_a_pending_edge(self) -> None:
        edges = (
            (SprintState.OPEN, SprintState.CLOSED),
            (SprintState.CLOSED, SprintState.OPEN),
            (SprintState.OPEN, SprintState.STOPPED),
            (SprintState.STOPPED, SprintState.CLOSED),
        )
        audit = MemoryAudit()
        for index, (source, target) in enumerate(edges):
            sprint = Sprint(f"sprint:{index}", "Sprint", source)
            host = FakeBoardHost([sprint], audit=audit)
            operation = TransitionRequest(
                EntityKind.SPRINT,
                sprint.ref,
                target,
                self.actor,
                "checked edge",
                request_id=f"edge-{index}",
            )
            if index == 0:
                with mock.patch.object(host.canon.audit, "append", side_effect=OSError("disk full")), self.assertRaises(BoardEventPending):
                    host.transition(operation)
                self.assertEqual(host.read(EntityKind.SPRINT, sprint.ref).state, target)
                self.assertEqual(host.transition(operation).entity.state, target)
            else:
                self.assertEqual(host.transition(operation).entity.state, target)
            self.assertEqual(host.canon.committed(f"edge-{index}").target_state, target.value)

    def test_fake_sprint_refusal_precedes_event_staging(self) -> None:
        host = FakeBoardHost([Sprint("sprint:refusal", "Sprint", SprintState.CLOSED)])
        with self.assertRaises(InvalidTransition):
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    "sprint:refusal",
                    SprintState.STOPPED,
                    self.actor,
                    "unsupported",
                    request_id="refused-sprint-edge",
                )
            )
        self.assertEqual(host.events, [])

    def test_replace_cannot_bypass_the_lifecycle_registry(self) -> None:
        self.host.create(
            Create(Card("ummanu-1417", "Protocol seam", CardState.READY), self.actor, "create")
        )

        with self.assertRaises(InvalidTransition):
            self.host.replace(
                Replace(
                    Card("ummanu-1417", "Protocol seam", CardState.DONE),
                    self.actor,
                    "skip review",
                )
            )

        self.assertEqual(self.host.read(EntityKind.CARD, "ummanu-1417").state, CardState.READY)

    def test_every_declared_edge_names_a_semantic_event_kind(self) -> None:
        declared = {event.value for event in EventKind}
        for entity_kind, edges in TRANSITIONS.items():
            self.assertTrue(edges, entity_kind)
            for edge, transition in edges.items():
                self.assertEqual(edge, (transition.source, transition.target))
                self.assertIn(transition.event_kind.value, declared)

    def test_role_aware_registry_allows_a_previously_missing_steward_recovery(self) -> None:
        declaration = card_transition("steward", CardState.BLOCKED, CardState.DONE)

        self.assertEqual(declaration.source, CardState.BLOCKED)
        self.assertEqual(declaration.target, CardState.DONE)
        self.assertEqual(declaration.event_kind, EventKind.CARD_MOVED)

    def test_role_aware_registry_rejects_invalid_role_and_edge(self) -> None:
        with self.assertRaises(CardTransitionForbidden):
            card_transition("worker", CardState.READY, CardState.IN_PROGRESS)
        self.assertEqual(
            card_transition("dispatcher", CardState.READY, CardState.IN_PROGRESS).event_kind,
            EventKind.CARD_STARTED,
        )
        with self.assertRaises(CardTransitionForbidden):
            card_transition("po", CardState.READY, CardState.READY)

    def test_every_authorized_card_edge_has_a_matching_lifecycle_declaration(self) -> None:
        for role, edges in CARD_TRANSITIONS.items():
            for source, target in edges:
                declaration = card_transition(role, source, target)
                self.assertEqual(declaration, TRANSITIONS[EntityKind.CARD][(source, target)])
                self.assertEqual((declaration.source, declaration.target), (source, target))

    def test_task_writer_can_import_the_registry_without_loading_the_board_host(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(source_root)
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-c",
                "import ummanu.tasks, sys; assert 'ummanu.board.sql_host' not in sys.modules",
            ],
            cwd=source_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_event_is_typed_deterministic_and_deduplicates_related_refs(self) -> None:
        event = Event(
            "event-1",
            EventKind.CARD_STARTED,
            EntityKind.CARD,
            "ummanu-1419",
            self.actor,
            "worker started",
            datetime(2026, 8, 11, 18, 0, 0, tzinfo=UTC),
            RelatedRefs(("sprint:943", "head-run:1417", "sprint:943")),
        )

        record = event.to_record("start-1")

        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["subject"], {"kind": "card", "ref": "ummanu-1419"})
        self.assertEqual(record["related_refs"], ["sprint:943", "head-run:1417"])
        self.assertEqual(Event.from_record(record), event)
        with self.assertRaisesRegex(ValueError, "EventKind"):
            Event(
                "event-2",
                "card.started",
                EntityKind.CARD,
                "ummanu-1419",
                self.actor,
                "reason",
                event.occurred_at,
            )

    def test_event_round_trip_preserves_fractional_occurrence_and_canonicalizes_timezone(self) -> None:
        event = Event(
            "event-precise",
            EventKind.CARD_STARTED,
            EntityKind.CARD,
            "ummanu-1419",
            self.actor,
            "worker started",
            datetime(2026, 8, 11, 20, 0, 0, 123456, tzinfo=UTC),
        )

        record = event.to_record("start-precise")

        self.assertEqual(record["occurred_at"], "2026-08-11T20:00:00.123456Z")
        self.assertEqual(Event.from_record(record), event)
        record["related_refs"] = ["sprint:943", "sprint:943"]
        with self.assertRaisesRegex(ValueError, "deduplicated"):
            Event.from_record(record)

    def test_legacy_decision_without_prerequisites_remains_readable(self) -> None:
        event = Event(
            "legacy-decision",
            EventKind.CARD_DECIDED,
            EntityKind.CARD,
            "ummanu-1419",
            self.actor,
            "keep the repair local",
            datetime(2026, 9, 3, 22, 0, 0, tzinfo=UTC),
            data={
                "marker": "decision:rework",
                "decision": "rework",
                "body": "keep the repair local",
                "body_sha256": "a" * 64,
                "assessment_visit": "assessment-1",
                "description_sha256": "b" * 64,
                "specification_revision": "specification-1",
                "protocol_prerequisites": [],
            },
        )
        record = event.to_record("legacy-decision-request")
        del record["data"]["protocol_prerequisites"]

        restored = Event.from_record(record)

        self.assertNotIn("protocol_prerequisites", restored.data)
        audit = MemoryAudit()
        audit.append("legacy-decision-request", record)
        self.assertEqual(BoardEventCanon(audit).events(ref="ummanu-1419"), (restored,))
        malformed = dict(record)
        malformed["data"] = {**record["data"], "protocol_prerequisites": "not-a-list"}
        with self.assertRaisesRegex(ValueError, "invalid protocol prerequisites"):
            Event.from_record(malformed)

    def test_durable_fake_mutations_append_one_complete_protocol_event(self) -> None:
        audit = MemoryAudit()
        host = FakeBoardHost(audit=audit)
        result = host.create(
            Create(
                Card("ummanu-1419", "Typed canon", CardState.READY),
                self.actor,
                "accepted",
                RelatedRefs(("sprint:943",)),
                "create-1",
            )
        )
        replacement = host.replace(
            Replace(
                Card("ummanu-1419", "Typed canon v2", CardState.READY),
                self.actor,
                "correct title",
                request_id="replace-1",
            )
        )

        events = BoardEventCanon(audit).events(ref="ummanu-1419")
        self.assertEqual(events, (result.event, replacement.event))
        self.assertEqual(events[0].kind, EventKind.ENTITY_CREATED)
        self.assertEqual(events[1].kind, EventKind.ENTITY_UPDATED)
        self.assertTrue(all(event.reason and event.occurred_at.tzinfo for event in events))

    def test_fake_host_supports_the_product_issue_command_shapes(self) -> None:
        """The protocol double exercises Product/Issue create, replace and close."""
        audit = MemoryAudit()
        host = FakeBoardHost(audit=audit)
        product = host.create(
            Create(
                Product("product:ummanu", "Ummanu", projects=("ummanu",)),
                Actor("po", "po"),
                "Product created",
                request_id="product",
            )
        )
        issue = host.create(
            Create(
                Issue("issue:1", "Crash", product.entity.ref, priority="P2", issue_kind="bug"),
                Actor("po", "po"),
                "Issue created",
                RelatedRefs((product.entity.ref,)),
                "issue",
            )
        )
        priority = host.replace(
            Replace(
                Issue("issue:1", "Crash", product.entity.ref, priority="P0", issue_kind="bug"),
                Actor("po", "po"),
                "urgent",
                RelatedRefs((product.entity.ref,)),
                "priority",
            )
        )
        closed = host.transition(
            TransitionRequest(
                EntityKind.ISSUE,
                issue.entity.ref,
                IssueState.CLOSED,
                Actor("po", "po"),
                "resolved",
                RelatedRefs((product.entity.ref,)),
                "close",
            )
        )

        self.assertEqual(
            [event.kind for event in BoardEventCanon(audit).events()],
            [
                EventKind.ENTITY_CREATED,
                EventKind.ENTITY_CREATED,
                EventKind.ENTITY_UPDATED,
                EventKind.ISSUE_CLOSED,
            ],
        )
        self.assertEqual(priority.entity.priority, "P0")
        self.assertEqual(closed.entity.state, IssueState.CLOSED)

    def test_every_registry_transition_persists_its_declared_event(self) -> None:
        # The concrete lifecycle values differ by entity type, but this test's
        # contract is intentionally registry-driven: every declared edge gets a
        # completed fake mutation and exactly its declared kind in the canon.
        from ummanu.board.models import Issue, Product, Sprint

        factories = {
            EntityKind.PRODUCT: lambda state, index: Product(f"product:{index}", "Product", state),
            EntityKind.ISSUE: lambda state, index: Issue(f"issue:{index}", "Issue", "product:1", state),
            EntityKind.SPRINT: lambda state, index: Sprint(f"sprint:{index}", "Sprint", state),
            EntityKind.CARD: lambda state, index: Card(f"card:{index}", "Card", state),
        }
        audit = MemoryAudit()
        for index, (entity_kind, edges) in enumerate(TRANSITIONS.items(), start=1):
            for edge_index, transition in enumerate(edges.values(), start=1):
                entity = factories[entity_kind](transition.source, f"{index}-{edge_index}")
                host = FakeBoardHost([entity], audit=audit)
                result = host.transition(
                    TransitionRequest(
                        entity_kind,
                        entity.ref,
                        transition.target,
                        self.actor,
                        "registry edge",
                        request_id=f"transition-{index}-{edge_index}",
                    )
                )
                self.assertEqual(result.event.kind, transition.event_kind)
        recorded = BoardEventCanon(audit).events()
        self.assertEqual(len(recorded), sum(len(edges) for edges in TRANSITIONS.values()))

    def test_no_fake_mutation_completes_when_its_event_cannot_be_recorded(self) -> None:
        audit = MemoryAudit()
        host = FakeBoardHost([Card("card:1", "Card", CardState.READY)], audit=audit)
        mutations = (
            lambda: host.create(
                Create(
                    Card("card:2", "Card", CardState.READY),
                    self.actor,
                    "accepted",
                    request_id="create-fail",
                )
            ),
            lambda: host.replace(
                Replace(
                    Card("card:1", "Card v2", CardState.READY),
                    self.actor,
                    "retitle",
                    request_id="replace-fail",
                )
            ),
            lambda: host.transition(
                TransitionRequest(
                    EntityKind.CARD,
                    "card:1",
                    CardState.IN_PROGRESS,
                    self.actor,
                    "start",
                    request_id="transition-fail",
                )
            ),
        )

        with mock.patch.object(host.canon.audit, "append", side_effect=OSError("disk full")):
            for mutation in mutations:
                with self.assertRaises(BoardEventPending):
                    mutation()

        self.assertEqual(BoardEventCanon(audit).events(), ())
        self.assertEqual(host.canon.audit.status(), {"ok": False, "pending": len(mutations)})

    def test_recreated_durable_hosts_never_reuse_an_event_id(self) -> None:
        audit = MemoryAudit()
        first = FakeBoardHost(audit=audit).create(
            Create(
                Card("card:1", "First", CardState.READY),
                self.actor,
                "accepted",
                request_id="create-1",
            )
        )
        second = FakeBoardHost(audit=audit).create(
            Create(
                Card("card:2", "Second", CardState.READY),
                self.actor,
                "accepted",
                request_id="create-2",
            )
        )
        # The same request against a rebuilt host is still one occurrence, so it keeps
        # the identifier the journal already published for it.
        replayed = FakeBoardHost([Card("card:1", "First", CardState.READY)], audit=audit).create(
            Create(
                Card("card:1", "First", CardState.READY),
                self.actor,
                "accepted",
                request_id="create-1",
            )
        )

        recorded = BoardEventCanon(audit).events()
        self.assertNotEqual(first.event.event_id, second.event.event_id)
        self.assertEqual(replayed.event, first.event)
        self.assertEqual(
            [event.event_id for event in recorded],
            [first.event.event_id, second.event.event_id],
        )


class BoardMutationTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = MemoryAudit()
        self.canon = BoardEventCanon(self.audit)
        self.event = Event(
            "event-transaction",
            EventKind.CARD_STARTED,
            EntityKind.CARD,
            "ummanu-1419",
            Actor("worker", "worker-1419"),
            "start work",
            datetime(2026, 8, 11, 18, 0, 0, tzinfo=UTC),
        )

    def test_backend_failure_before_commit_removes_the_staged_event(self) -> None:
        transaction = MutationEventTransaction(self.canon, request_id="failure-1", event=self.event)

        with self.assertRaisesRegex(RuntimeError, "backend failed"):
            transaction.execute(
                lambda: (_ for _ in ()).throw(RuntimeError("backend failed")), confirm=lambda: "never"
            )

        self.assertIsNone(self.canon.event("failure-1"))
        self.assertEqual(self.canon.audit.status(), {"ok": True, "pending": 0})

    def test_event_write_failure_is_pending_and_replay_does_not_repeat_backend(self) -> None:
        transaction = MutationEventTransaction(self.canon, request_id="pending-1", event=self.event)
        with mock.patch.object(self.canon.audit, "append", side_effect=OSError("disk full")), self.assertRaises(BoardEventPending):
            transaction.execute(lambda: "backend result", confirm=lambda: "replayed")
        self.assertEqual(self.canon.audit.status(), {"ok": False, "pending": 1})

        recovered = transaction.execute(
            lambda: (_ for _ in ()).throw(AssertionError("effect repeated")),
            confirm=lambda: "confirmed",
        )
        self.assertEqual(recovered, "confirmed")
        self.assertEqual(self.canon.events(), (self.event,))

    def test_the_transaction_runs_effect_confirm_finish_and_commit_in_that_order(self) -> None:
        """The whole contract in one order: issue, confirm, finish, publish."""
        order: list[str] = []
        transaction = MutationEventTransaction(self.canon, request_id="finish-1", event=self.event)
        with mock.patch.object(
            self.canon.audit,
            "append",
            side_effect=lambda *a, **k: order.append("commit"),
        ):
            result = transaction.execute(
                lambda: order.append("effect"),
                confirm=lambda: order.append("confirm") or "confirmed",
                finish=lambda value: order.append(f"finish:{value}"),
            )

        self.assertEqual(order, ["effect", "confirm", "finish:confirmed", "commit"])
        self.assertEqual(result, "confirmed")

    def test_the_effect_call_is_the_only_window_in_which_a_record_is_discarded(self) -> None:
        """One enforcement point, one discard window: the `effect` call and nothing else.

        A read that fails after the write returned is not evidence that the write did not
        happen, so every outcome after `effect` returns keeps the exact staged record.
        """
        for index, (label, kwargs) in enumerate(
            (
                ("confirm", {"confirm": lambda: (_ for _ in ()).throw(RuntimeError("read timed out"))}),
                (
                    "finish",
                    {
                        "confirm": lambda: "confirmed",
                        "finish": lambda _r: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
                    },
                ),
            )
        ):
            with self.subTest(label):
                request_id = f"post-effect-{index}"
                transaction = MutationEventTransaction(
                    self.canon,
                    request_id=request_id,
                    event=self.event,
                )

                with self.assertRaises(BoardEventPending):
                    transaction.execute(lambda: "issued", **kwargs)

                self.assertEqual(self.canon.event(request_id), self.event)
                self.assertEqual(self.canon.events(), ())
                self.canon.audit.discard(request_id, self.event.to_record(request_id))

        transaction = MutationEventTransaction(
            self.canon,
            request_id="pre-effect",
            event=self.event,
        )
        with self.assertRaisesRegex(RuntimeError, "backend refused"):
            transaction.execute(
                lambda: (_ for _ in ()).throw(RuntimeError("backend refused")),
                confirm=lambda: (_ for _ in ()).throw(AssertionError("confirmed a refused effect")),
            )

        self.assertIsNone(self.canon.event("pre-effect"))
        self.assertEqual(self.canon.audit.status(), {"ok": True, "pending": 0})

    def test_an_unconfirmed_pending_record_is_never_resolved_by_the_read_that_failed(self) -> None:
        """A pending replay that cannot confirm keeps the record instead of publishing it."""
        transaction = MutationEventTransaction(self.canon, request_id="unconfirmed", event=self.event)
        with mock.patch.object(self.canon.audit, "append", side_effect=OSError("disk full")), self.assertRaises(BoardEventPending):
            transaction.execute(lambda: "issued", confirm=lambda: "confirmed")

        with self.assertRaises(BoardEventPending):
            transaction.execute(
                lambda: (_ for _ in ()).throw(AssertionError("effect repeated")),
                confirm=lambda: (_ for _ in ()).throw(RuntimeError("read timed out")),
            )

        self.assertEqual(self.canon.event("unconfirmed"), self.event)
        self.assertEqual(self.canon.events(), ())
        self.assertEqual(self.canon.audit.status(), {"ok": False, "pending": 1})

    def test_incomplete_follow_up_work_keeps_the_exact_pending_event(self) -> None:
        """A follow-up that does not land publishes nothing and leaves the record to recover."""
        transaction = MutationEventTransaction(self.canon, request_id="finish-2", event=self.event)

        with self.assertRaises(BoardEventPending):
            transaction.execute(
                lambda: "backend result",
                confirm=lambda: "never",
                finish=lambda _result: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
            )

        self.assertEqual(self.canon.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(self.canon.event("finish-2"), self.event)
        self.assertEqual(self.canon.events(), ())

        finished: list[str] = []
        recovered = transaction.execute(
            lambda: (_ for _ in ()).throw(AssertionError("effect repeated")),
            confirm=lambda: "confirmed",
            finish=finished.append,
        )

        self.assertEqual(recovered, "confirmed")
        self.assertEqual(finished, ["confirmed"])
        self.assertEqual(self.canon.events(), (self.event,))

    def test_a_committed_occurrence_owes_the_backend_no_further_follow_up(self) -> None:
        """The commit is proof the follow-up completed, so a replay does not repeat it."""
        transaction = MutationEventTransaction(self.canon, request_id="finish-3", event=self.event)
        transaction.execute(lambda: "backend result", confirm=lambda: "never", finish=lambda _r: None)

        replayed = transaction.execute(
            lambda: (_ for _ in ()).throw(AssertionError("effect repeated")),
            confirm=lambda: "confirmed",
            finish=lambda _r: (_ for _ in ()).throw(AssertionError("follow-up repeated")),
        )

        self.assertEqual(replayed, "confirmed")
        self.assertEqual(self.canon.events(), (self.event,))

    def test_concurrent_same_request_staging_leaves_exactly_one_owner(self) -> None:
        """Compare-and-stage is one critical section, not a read followed by a write.

        Each thread owns its own canon over the one shared owner, so they contend on its claim
        lock as separate writers retrying the same request id would.
        """
        contenders = [
            Event(
                f"event-race-{index}",
                EventKind.CARD_STARTED,
                EntityKind.CARD,
                "ummanu-1419",
                Actor("worker", f"worker-{index}"),
                "start work",
                datetime(2026, 8, 11, 18, 0, 0, tzinfo=UTC),
            )
            for index in range(4)
        ]
        start = threading.Barrier(len(contenders))
        guard = threading.Lock()
        staged: list[Event] = []
        refused: list[str] = []

        def stage(event: Event) -> None:
            canon = BoardEventCanon(self.audit)
            start.wait()
            try:
                owned = canon.stage("race-1", event)
            except ValueError as exc:
                with guard:
                    refused.append(str(exc))
                return
            with guard:
                staged.append(owned)

        threads = [threading.Thread(target=stage, args=(event,)) for event in contenders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(staged), 1)
        self.assertEqual(len(refused), len(contenders) - 1)
        self.assertTrue(all("another operation or payload" in message for message in refused))
        self.assertEqual(self.canon.event("race-1"), staged[0])
        self.assertEqual(self.canon.audit.status(), {"ok": False, "pending": 1})

    def test_a_second_owner_of_the_request_id_is_refused_before_any_backend_effect(self) -> None:
        BoardEventCanon(self.audit).stage("shared-1", self.event)
        conflicting = Event(
            "event-conflicting",
            EventKind.CARD_BLOCKED,
            EntityKind.CARD,
            "ummanu-1419",
            Actor("steward", "steward-1419"),
            "blocked",
            self.event.occurred_at,
        )
        transaction = MutationEventTransaction(self.canon, request_id="shared-1", event=conflicting)

        with self.assertRaisesRegex(ValueError, "another operation or payload"):
            transaction.execute(
                lambda: (_ for _ in ()).throw(AssertionError("effect ran against a foreign event")),
                confirm=lambda: (_ for _ in ()).throw(AssertionError("replayed a foreign event")),
            )

        self.assertEqual(self.canon.event("shared-1"), self.event)
        self.assertEqual(self.canon.audit.status(), {"ok": False, "pending": 1})

    def test_staging_refuses_an_event_id_already_owned_by_another_request(self) -> None:
        duplicate = Event(
            self.event.event_id,
            EventKind.CARD_BLOCKED,
            EntityKind.CARD,
            "ummanu-1419",
            Actor("steward", "steward-1419"),
            "blocked",
            self.event.occurred_at,
        )

        self.canon.stage("owner-1", self.event)
        with self.assertRaisesRegex(ValueError, "already belongs to another request"):
            self.canon.stage("duplicate-1", duplicate)

        self.canon.commit("owner-1", self.event)
        with self.assertRaisesRegex(ValueError, "already belongs to another request"):
            BoardEventCanon(self.audit).stage("duplicate-2", duplicate)
        self.assertEqual(self.canon.events(), (self.event,))

    def test_every_typed_staging_route_reaches_the_atomic_claim(self) -> None:
        class ClaimReached(Exception):
            """Raised in place of the one staging primitive, to prove it was reached."""

        actor = Actor("worker", "worker-1419")
        host = FakeBoardHost([Card("card:1", "Card", CardState.READY)], audit=self.audit)
        routes = (
            lambda: self.canon.stage("route-stage", self.event),
            lambda: self.canon.commit("route-commit", self.event),
            lambda: MutationEventTransaction(
                self.canon,
                request_id="route-transaction",
                event=self.event,
            ).execute(lambda: "effect", confirm=lambda: "replay"),
            lambda: host.create(
                Create(
                    Card("card:2", "Card", CardState.READY),
                    actor,
                    "accepted",
                    request_id="route-create",
                )
            ),
            lambda: host.replace(
                Replace(
                    Card("card:1", "Card v2", CardState.READY),
                    actor,
                    "retitle",
                    request_id="route-replace",
                )
            ),
            lambda: host.transition(
                TransitionRequest(
                    EntityKind.CARD,
                    "card:1",
                    CardState.IN_PROGRESS,
                    actor,
                    "start",
                    request_id="route-transition",
                )
            ),
        )

        with mock.patch.object(MemoryAudit, "claim", side_effect=ClaimReached):
            for route in routes:
                with self.assertRaises(ClaimReached):
                    route()

        self.assertEqual(self.canon.events(), ())
        self.assertEqual(self.canon.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(host.read(EntityKind.CARD, "card:1"), Card("card:1", "Card", CardState.READY))

    def test_committed_request_id_replay_does_not_repeat_backend_effect(self) -> None:
        transaction = MutationEventTransaction(self.canon, request_id="replay-1", event=self.event)
        # The confirming read is the result, on a fresh occurrence as much as on a replay: what
        # the effect call happened to return is not evidence of anything on the board.
        self.assertEqual(transaction.execute(lambda: "first", confirm=lambda: "read"), "read")
        with mock.patch.object(self.canon.audit, "claim", wraps=self.canon.audit.claim) as claim:
            self.assertEqual(
                transaction.execute(
                    lambda: (_ for _ in ()).throw(AssertionError("effect repeated")),
                    confirm=lambda: "replayed",
                ),
                "replayed",
            )
        claim.assert_called_once()
        self.assertEqual(self.canon.events(), (self.event,))


class SqlBoardHostTests(unittest.TestCase):
    def test_cards_exclude_typed_rows_and_read_refuses_them(self) -> None:
        execution = {
            "ref": "ummanu-1417",
            "title": "Protocol seam",
            "state": "ready",
            "record_type": "task",
        }
        issue = {
            "ref": "issue:1417",
            "title": "Typed issue",
            "state": "issues",
            "record_type": "issue",
        }
        product = {
            "ref": "product:ummanu",
            "title": "Typed product",
            "state": "issues",
            "record_type": "product",
        }
        with mock.patch("ummanu.board.sql_host.TaskReader") as reader_class:
            reader = reader_class.return_value
            reader.list.return_value = [execution, issue, product]
            reader.show.return_value = issue
            host = SqlBoardHost(mock.sentinel.client)

            cards = host.list(EntityKind.CARD)
            self.assertEqual([card.ref for card in cards], ["ummanu-1417"])
            with self.assertRaises(BoardProtocolError):
                host.read(EntityKind.CARD, "issue:1417")

    def test_sprint_lifecycle_edges_are_declared_for_host_migration(self) -> None:
        """Sprint close, reopen and hard-stop have explicit typed declarations."""
        host = SqlBoardHost(mock.sentinel.client, data_dir="/data", instance="/instance")
        card = Card("ummanu-1420", "Card host transitions", CardState.READY)

        with self.assertRaisesRegex(BoardProtocolError, "create for card is not migrated"):
            host.create(Create(card, Actor("po", "operator"), "accepted"))
        with self.assertRaisesRegex(BoardProtocolError, "replace for card is not migrated"):
            host.replace(Replace(card, Actor("po", "operator"), "edited"))
        self.assertEqual(
            TRANSITIONS[EntityKind.SPRINT][(SprintState.OPEN, SprintState.CLOSED)].event_kind,
            EventKind.SPRINT_CLOSED,
        )
        self.assertEqual(
            TRANSITIONS[EntityKind.SPRINT][(SprintState.CLOSED, SprintState.OPEN)].event_kind,
            EventKind.SPRINT_REOPENED,
        )
        self.assertEqual(
            TRANSITIONS[EntityKind.SPRINT][(SprintState.OPEN, SprintState.STOPPED)].event_kind,
            EventKind.SPRINT_STOPPED,
        )
        self.assertEqual(
            TRANSITIONS[EntityKind.SPRINT][(SprintState.STOPPED, SprintState.CLOSED)].event_kind,
            EventKind.SPRINT_CLOSED,
        )

    def test_sprint_transition_rejects_a_raw_metadata_escape_before_any_backend_read(self) -> None:
        host = SqlBoardHost(mock.sentinel.client, data_dir="/data", instance="/instance")
        with self.assertRaisesRegex(BoardProtocolError, "supplement must be normalized"):
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    "sprint:943",
                    SprintState.STOPPED,
                    Actor("po", "operator"),
                    "hard stop",
                    request_id="raw-metadata",
                    sprint={"sprint_status": "closed"},  # type: ignore[arg-type]
                )
            )

    def test_sprint_product_ref_can_read_the_linked_product(self) -> None:
        sprint = {
            "ref": "sprint:943",
            "goal": "Board protocol",
            "status": "open",
            "product": "ummanu",
            "issues": ["issue:1417"],
            "cards": [],
        }
        product = {
            "ref": "product:ummanu",
            "title": "Ummanu",
            "closed": False,
            "projects": ["ummanu"],
        }
        with (
            mock.patch("ummanu.board.sql_host.SprintReader") as sprint_reader_class,
            mock.patch("ummanu.board.sql_host.ProductIssueStore") as store_class,
        ):
            sprint_reader_class.return_value.show.return_value = sprint
            store_class.return_value.show_product.return_value = product
            host = SqlBoardHost(mock.sentinel.client, data_dir="/data", instance="/instance")

            normalized_sprint = host.read(EntityKind.SPRINT, "sprint:943")
            self.assertEqual(normalized_sprint.product_ref, "product:ummanu")
            linked_product = host.read(EntityKind.PRODUCT, normalized_sprint.product_ref)
            self.assertEqual(linked_product.ref, normalized_sprint.product_ref)


if __name__ == "__main__":
    unittest.main()
