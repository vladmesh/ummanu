"""Owner events and the bell (secretary-1770), unit-level.

The entity's rules on the in-memory store of `tests.owner_event_fakes` (the same CHECK vocabularies as
`owner_events`), every producer writing exactly one event and nothing on a repeat, a writer that fails
never failing its producer, the stay-unread rule by click, by "mark all read" and by the card's
completion, the web's bell, list, pin, filter and the two writes, and the two carried fixes. The
PostgreSQL half (migration 0018 on a pre-0018 store, dedup and mark-read on a real store) is in
`tests/test_board_store_schema.py`, integration-board.
"""

from __future__ import annotations

import copy
import importlib
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.owner_event_fakes import CheckViolation, FakeOwnerEvents
from tests.po_card_fakes import DECISION_BODY, REF
from tests.po_handover_fakes import REASON, HandedOverFixture, MemoryAudit, OneCardClient, decision_card
from tests.web_fakes import Recording
from ummanu.board import owner_events, schema, schema_gate
from ummanu.board.owner_events import (
    CLASSES,
    KIND_CLASS,
    KINDS,
    NEEDS_OWNER,
    NOTICE,
    OwnerEventsUnavailable,
    ReadRefused,
    class_of,
    needs_human_section,
    record,
    settle,
)
from ummanu.board.owner_handover import mark_values, waiting_owner
from ummanu.sprints import BUDGET_EVENT_TYPES
from ummanu.tasks import TaskWriter
from ummanu.web import pages
from ummanu.web.app import WebApp
from ummanu.webproto.errors import OwnerConflict, OwnerEventMissing, RuntimeUnavailable
from ummanu.webproto.owner_events import OwnerEventLayer

SPRINT = "sprint:1"


def fake_for(card: dict[str, Any] | None = None) -> FakeOwnerEvents:
    """A store whose subject lookup answers `card` for its own ref and nothing else."""
    return FakeOwnerEvents(lambda ref: card if card is not None and card.get("ref") == ref else None)


# --- the entity ------------------------------------------------------------------------------


class EntityTests(unittest.TestCase):
    def test_the_class_is_derived_from_the_kind(self) -> None:
        self.assertEqual(set(CLASSES), {NEEDS_OWNER, NOTICE})
        self.assertEqual(
            {kind for kind in KINDS if class_of(kind) == NEEDS_OWNER},
            # `e2e_budget_spent` since 0023 (secretary-1796): a card whose e2e cap only the owner can raise;
            # `e2e_after_merge` since 0024 (secretary-1807): an after-merge e2e run that needs the owner.
            {"card_handed_to_owner", "steward_needs_human", "po_card_escalated"},
        )
        for kind in ("sprint_closed", "sprint_stopped", "budget_signal", "observer_dead", "head_dead",
                     "po_turn_failed", "provider_red", "delegated_card_settled"):
            self.assertEqual(class_of(kind), NOTICE, kind)
        with self.assertRaises(ValueError):
            class_of("card_moved")

    def test_the_schema_and_the_revision_spell_the_same_vocabularies(self) -> None:
        """The CHECKs of `board/schema.py`, 0018 and 0024 are the lists this module derives classes from."""
        table = schema.metadata.tables["owner_events"]
        import sqlalchemy as sa

        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        # A kind may carry a digit (`e2e_budget_spent`, secretary-1796).
        spelled = set(re.findall(r"'([a-z0-9_]+)'", checks["owner_event_kind_in_vocabulary"]))
        self.assertEqual(spelled, set(KINDS))
        self.assertEqual(set(re.findall(r"'([a-z_]+)'", checks["owner_event_class_in_vocabulary"])), set(CLASSES))
        needs = re.search(r"kind IN \(([^)]*)\)", checks["owner_event_class_follows_kind"]).group(1)
        self.assertEqual(set(re.findall(r"'([a-z0-9_]+)'", needs)), set(owner_events.NEEDS_OWNER_KINDS))
        revision = importlib.import_module("ummanu.board.migrations.versions.0018_owner_events")
        source = Path(revision.__file__).read_text(encoding="utf-8")
        # 0021 restated the kind vocabulary with `delegated_card_settled` (secretary-1792); 0023 restates
        # it and the class rule with `e2e_budget_spent`, a `needs_owner` kind (secretary-1796); 0024 restates
        # both again with `e2e_after_merge`, a `needs_owner` kind (secretary-1807). The class vocabulary is
        # still the one 0018 created.
        restated = importlib.import_module("ummanu.board.migrations.versions.0028_owner_turns")
        restated_source = Path(restated.__file__).read_text(encoding="utf-8")
        restated_names = {"owner_event_kind_in_vocabulary", "owner_event_class_follows_kind"}
        for name, text in checks.items():
            spelled_in = restated_source if name in restated_names else source
            self.assertIn(
                text.replace("\n", ""),
                spelled_in.replace('"\n            "', "").replace('"\n        "', ""),
                name,
            )
        self.assertEqual(revision.down_revision, "0017_po_card_kinds")
        self.assertEqual(restated.down_revision, "0027_sprint_owner_decisions")

    def test_the_fake_store_refuses_what_the_checks_refuse(self) -> None:
        store = FakeOwnerEvents()
        for kind, event_class in (
            ("card_moved", NOTICE),
            ("sprint_closed", "urgent"),
            ("sprint_closed", NEEDS_OWNER),
            ("card_handed_to_owner", NOTICE),
        ):
            with self.subTest(kind=kind, event_class=event_class), self.assertRaises(CheckViolation):
                store.insert_row(kind, event_class, None, "x", f"{kind}:{event_class}")
        self.assertEqual(store.rows, {})
        for kind, event_class in KIND_CLASS.items():
            self.assertTrue(store.insert_row(kind, event_class, None, "x", kind))
        self.assertEqual(sorted(store.kinds()), sorted(KINDS))

    def test_the_writer_is_idempotent_on_its_dedup_key(self) -> None:
        store = FakeOwnerEvents()
        self.assertTrue(record("sprint_closed", SPRINT, "closed", "sprint_closed:sprint:1:e1", to=store))
        self.assertFalse(record("sprint_closed", SPRINT, "closed again", "sprint_closed:sprint:1:e1", to=store))
        [event] = store.rows.values()
        self.assertEqual((event.kind, event.event_class, event.text, event.read_at), ("sprint_closed", NOTICE, "closed", None))

    def test_a_writer_that_fails_never_fails_its_caller(self) -> None:
        missing = FakeOwnerEvents()
        missing.missing_table = True
        broken = FakeOwnerEvents()
        broken.failing = RuntimeError("connection reset")
        with self.assertLogs("ummanu.board.owner_events", level="WARNING") as logged:
            self.assertFalse(record("sprint_closed", SPRINT, "x", "k1", to=missing))
            self.assertFalse(record("sprint_closed", SPRINT, "x", "k2", to=broken))
            self.assertFalse(record("not_a_kind", SPRINT, "x", "k3", to=FakeOwnerEvents()))
            self.assertFalse(record("sprint_closed", SPRINT, "x", "", to=FakeOwnerEvents()))
            self.assertEqual(settle(REF, to=missing), 0)
        self.assertTrue(any("migration 0018" in line for line in logged.output), logged.output)
        # Nowhere to write: nothing configured, nothing raised.
        self.assertFalse(record("sprint_closed", SPRINT, "x", "k4", to=None))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(record("sprint_closed", SPRINT, "x", "k5", to=Path(tmp)))
            self.assertFalse(record("sprint_closed", SPRINT, "x", "k6", to=SimpleNamespace(call=None)))

    def test_the_strict_writer_answers_written_present_failed_or_not_applicable(self) -> None:
        """secretary-1792: the one caller whose work is complete only with its event hears how it went."""
        store = FakeOwnerEvents()
        self.assertEqual(owner_events.record_strict("delegated_card_settled", REF, "x", "d1", to=store), "written")
        self.assertEqual(
            owner_events.record_strict("delegated_card_settled", REF, "again", "d1", to=store), "already_present"
        )
        self.assertEqual(len(store.rows), 1)
        missing = FakeOwnerEvents()
        missing.missing_table = True
        broken = FakeOwnerEvents()
        broken.failing = RuntimeError("connection reset")
        with self.assertLogs("ummanu.board.owner_events", level="WARNING"):
            for to, kind, key in (
                (missing, "delegated_card_settled", "d2"),
                (broken, "delegated_card_settled", "d3"),
                (FakeOwnerEvents(), "not_a_kind", "d4"),
                (FakeOwnerEvents(), "delegated_card_settled", ""),
            ):
                with self.subTest(kind=kind, key=key):
                    self.assertEqual(owner_events.record_strict(kind, REF, "x", key, to=to), "failed")
        # No board store configured at all: nothing to wait for, logged, never raised.
        with self.assertLogs("ummanu.board.owner_events", level="INFO"):
            self.assertEqual(
                owner_events.record_strict("delegated_card_settled", REF, "x", "d5", to=None), "not_applicable"
            )
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("ummanu.board.owner_events", level="INFO"):
            self.assertEqual(
                owner_events.record_strict("delegated_card_settled", REF, "x", "d6", to=Path(tmp)),
                "not_applicable",
            )

    def test_a_postgres_store_without_the_table_is_unavailable_not_an_error_of_the_caller(self) -> None:
        import psycopg

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, *_args, **_kwargs):
                # A store one revision before owner events: the schema gate reads it, and nothing
                # may reach the table it does not have.
                if sql == schema_gate.VERSION_QUERY:
                    return SimpleNamespace(fetchall=lambda: [("0017_po_card_kinds",)])
                raise psycopg.errors.UndefinedTable('relation "owner_events" does not exist')

        store = owner_events.OwnerEventStore(SimpleNamespace(conninfo=lambda: "dbname=x"))
        with mock.patch("psycopg.connect", return_value=Connection()):
            with self.assertRaisesRegex(OwnerEventsUnavailable, "owes 11 migration.*0018_owner_events") as raised:
                store.unread_count()
            self.assertEqual(raised.exception.code, "schema_owed")
            self.assertEqual(raised.exception.pending[0], "0018_owner_events")
            with self.assertLogs("ummanu.board.owner_events", level="WARNING"):
                self.assertFalse(record("sprint_closed", SPRINT, "x", "k", to=store))

    def test_a_long_text_is_cut_not_refused(self) -> None:
        store = FakeOwnerEvents()
        record("head_dead", REF, "x" * 5000, "k", to=store)
        [event] = store.rows.values()
        self.assertEqual(len(event.text), owner_events.TEXT_LIMIT)

    def test_the_needs_a_human_section_is_read_from_the_report(self) -> None:
        report = "# Steward\n\n## Actions\nnone\n\n## Needs a human\n- pay the relay (ummanu-12)\n\n## Next\nx"
        self.assertEqual(needs_human_section(report), "- pay the relay (ummanu-12)")
        self.assertEqual(needs_human_section("**Needs a human:**\nrotate the key"), "rotate the key")
        for empty in ("## Needs a human\nnone", "## Needs a human\n\n## Next\n", "## Needs a human\n- None.", "no section"):
            with self.subTest(empty=empty):
                self.assertIsNone(needs_human_section(empty))


# --- the stay-unread rule and mark-read -----------------------------------------------------------


class StayUnreadStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.card = {**decision_card(), "extensions": {"extra": mark_values("2026-09-26T15:00:00Z", REASON, "po")}}
        self.store = fake_for(self.card)
        record("card_handed_to_owner", REF, "handed", "h", to=self.store)
        record("sprint_closed", SPRINT, "closed", "c", to=self.store)
        record("budget_signal", SPRINT, "signal", "b", to=self.store)
        self.needs, self.closed, self.signal = sorted(self.store.rows.values(), key=lambda row: row.id)

    def test_a_click_refuses_a_needs_owner_event_whose_card_still_waits(self) -> None:
        with self.assertRaises(ReadRefused):
            self.store.mark_read(self.needs.id)
        self.assertIsNone(self.store.rows[self.needs.id].read_at)
        self.assertIsNotNone(self.store.mark_read(self.closed.id).read_at)
        self.assertEqual(self.store.unread_count(), 2)

    def test_mark_all_read_takes_notices_only(self) -> None:
        self.assertEqual(self.store.mark_all_read(), 2)
        self.assertEqual(self.store.unread_count(), 1)
        self.assertIsNone(self.store.rows[self.needs.id].read_at)

    def test_the_mark_clearing_reads_the_cards_needs_owner_events(self) -> None:
        self.card["extensions"]["extra"] = {}
        self.assertEqual(settle(REF, to=self.store), 1)
        self.assertIsNotNone(self.store.rows[self.needs.id].read_at)
        self.assertIsNone(self.store.rows[self.closed.id].read_at, "a notice is not the card's to clear")

    def test_a_needs_owner_event_whose_card_carries_no_mark_is_read_by_a_click(self) -> None:
        record("steward_needs_human", "ummanu-77", "needs a human", "s", to=self.store)
        steward = self.store.of_kind("steward_needs_human")[0]
        self.assertIsNotNone(self.store.mark_read(steward.id).read_at)

    def test_the_list_pins_open_needs_owner_events_and_then_goes_newest_first(self) -> None:
        events = self.store.events()
        self.assertEqual([event.kind for event in events], ["card_handed_to_owner", "budget_signal", "sprint_closed"])
        self.assertTrue(events[0].pinned and events[0].held)
        self.card["extensions"]["extra"] = {}
        settle(REF, to=self.store)
        self.assertEqual([event.kind for event in self.store.events()], ["budget_signal", "sprint_closed", "card_handed_to_owner"])
        self.store.mark_read(self.signal.id)
        self.assertEqual([event.kind for event in self.store.events(unread_only=True)], ["sprint_closed"])


class StayUnreadThroughTheCardTests(unittest.TestCase):
    """The three paths of the PO's decision on the real writer: a click, mark-all, the card's completion."""

    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.card = decision_card()
        self.client = OneCardClient(self.card, tmp)
        self.events = fake_for(self.card)
        self.client.owner_events = self.events
        self.writer = TaskWriter(self.client, data_dir=tmp)  # type: ignore[arg-type]
        self.writer.audit = MemoryAudit()
        self.writer.reader = mock.Mock(show=lambda reference: copy.deepcopy(self.card))
        self.enterContext(mock.patch("ummanu.tasks._task_number", return_value=1900))
        typed: dict[str, Any] = {}
        self.writer._typed_event = lambda request_id: typed.get(request_id)  # type: ignore[method-assign]

        def transition(**fields: Any) -> Any:
            with self.writer._mutation():
                fields["finish"](None)
                self.card["state"] = fields["target"].value
                typed[fields["request_id"]] = SimpleNamespace(
                    ref=fields["reference"], reason=fields["reason"], source_state="in_progress"
                )
            return SimpleNamespace(event=SimpleNamespace(event_id="evt-done"))

        self.writer._transition_card = transition  # type: ignore[method-assign]
        self.layer = OwnerEventLayer("/nonexistent", store=self.events)

    def hand_over(self, request_id: str = "handover-1") -> dict[str, Any]:
        return self.writer.handover(
            role="po", actor="po", reference=REF, to="owner", reason=REASON, request_id=request_id
        )

    def test_the_handover_writes_one_needs_owner_event_and_a_repeat_writes_none(self) -> None:
        first = self.hand_over()
        again = self.hand_over()
        self.assertTrue(again["replayed"])
        [event] = self.events.rows.values()
        self.assertEqual((event.kind, event.event_class, event.subject_ref), ("card_handed_to_owner", NEEDS_OWNER, REF))
        self.assertIn(REASON, event.text)
        self.assertEqual(event.dedup_key, f"card_handed_to_owner:{REF}:{first['event_id']}")
        self.assertEqual(self.layer.unread_count()["count"], 1)

    def test_click_and_mark_all_leave_it_unread_and_the_completion_reads_it(self) -> None:
        self.hand_over()
        [event] = self.events.rows.values()
        with self.assertRaises(OwnerConflict):
            self.layer.mark_read(event.id)
        self.assertEqual(self.layer.mark_all_read()["marked"], 0)
        self.assertEqual(self.layer.unread_count()["count"], 1)

        self.writer.complete(role="po", actor="po", reference=REF, kind="decision", body=DECISION_BODY,
                             request_id="complete-1")

        self.assertIsNone(waiting_owner(self.card))
        self.assertIsNotNone(self.events.rows[event.id].read_at)
        self.assertEqual(self.layer.unread_count()["count"], 0)

    def test_a_card_moved_on_without_a_mark_settles_nothing(self) -> None:
        record("card_handed_to_owner", REF, "an older handover's event", "old", to=self.events)
        self.writer._reset_transition_metadata(copy.deepcopy(self.card), source="in_progress", target="blocked")
        self.assertIsNone(self.events.of_kind("card_handed_to_owner")[0].read_at)

    def test_required_handover_and_completion_refuse_an_unavailable_wait_store(self) -> None:
        from ummanu.tasks import TaskError

        self.events.missing_table = True
        with self.assertRaisesRegex(TaskError, "required owner wait.*retry the same request ID"):
            self.hand_over()
        self.assertIsNone(waiting_owner(self.card))
        self.events.missing_table = False
        self.hand_over()
        self.events.missing_table = True
        with self.assertRaisesRegex(TaskError, "required owner wait.*retry the same request ID"):
            self.writer.complete(role="po", actor="po", reference=REF, kind="decision", body=DECISION_BODY,
                                 request_id="complete-1")
        self.assertEqual(self.card["state"], "in_progress")
        self.assertIsNotNone(waiting_owner(self.card))


# --- the producers ----------------------------------------------------------------------------


class StewardProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.card = {
            "ref": "ummanu-77",
            "id": 77,
            "type": "research",
            "state": "in_progress",
            "project": "ummanu",
            "sprint": None,
            "extensions": {"extra": {"steward_report": "1"}},
            "comments": [],
        }
        self.client = OneCardClient(self.card, tmp)
        self.events = FakeOwnerEvents()
        self.client.owner_events = self.events
        self.writer = TaskWriter(self.client, data_dir=tmp)  # type: ignore[arg-type]
        self.writer.audit = MemoryAudit()
        self.writer.reader = mock.Mock(show=lambda reference: copy.deepcopy(self.card))
        self.enterContext(mock.patch("ummanu.tasks._task_number", return_value=77))
        self.writer._typed_event = lambda request_id: None  # type: ignore[method-assign]
        self.writer._guard_sprint_write = lambda **_: {}  # type: ignore[method-assign]

        def transition(**fields: Any) -> Any:
            fields["finish"](None)
            self.card["state"] = fields["target"].value
            return SimpleNamespace(event=SimpleNamespace(event_id=f"evt-{fields['request_id']}"))

        self.writer._transition_card = transition  # type: ignore[method-assign]

    def move(self, reason: str, *, request_id: str = "m-1", **fields: Any) -> dict[str, Any]:
        call = {"role": "steward", "actor": "steward", "reference": "ummanu-77", "target": "blocked",
                "reason": reason, "request_id": request_id, **fields}
        return self.writer.move(**call)

    def test_a_report_card_blocked_with_needs_a_human_writes_one_needs_owner_event(self) -> None:
        self.move("## Needs a human\n- rotate the relay key (ummanu-12)\n")
        [event] = self.events.rows.values()
        self.assertEqual((event.kind, event.event_class, event.subject_ref), ("steward_needs_human", NEEDS_OWNER, "ummanu-77"))
        self.assertIn("rotate the relay key", event.text)
        self.assertEqual(event.dedup_key, "steward_needs_human:ummanu-77:evt-m-1")

    def test_no_section_another_card_or_another_role_writes_nothing(self) -> None:
        self.move("the preflight failed; no sweep ran", request_id="m-2")
        self.card.update(state="in_progress", extensions={"extra": {}})
        self.move("## Needs a human\n- x\n", request_id="m-3")
        self.assertEqual(self.events.rows, {})


class SprintProducerTests(unittest.TestCase):
    """`SprintWriter` writes the close, the budget signal and the hard stop, each once."""

    def writer(self) -> Any:
        from ummanu.sprints import SprintWriter

        writer = SprintWriter.__new__(SprintWriter)
        writer.client = SimpleNamespace(owner_events=self.events, _depth=1)
        return writer

    def setUp(self) -> None:
        self.events = FakeOwnerEvents()

    def test_a_close_writes_one_sprint_closed_and_its_replay_none(self) -> None:
        writer = self.writer()
        closed = {"action": "sprint_closed", "event_id": "evt-close", "sprint": {"ref": SPRINT}}
        with mock.patch.object(type(writer), "_close_atomic", return_value=closed), mock.patch.object(
            type(writer), "_role"
        ), mock.patch.object(type(writer), "_guard_observer_identity"):
            for _ in range(2):
                writer.close(role="observer", actor="observer", reference=SPRINT, request_id="close-1",
                             reason="all done")
        [event] = self.events.rows.values()
        self.assertEqual((event.kind, event.subject_ref, event.dedup_key), ("sprint_closed", SPRINT, "sprint_closed:sprint:1:evt-close"))
        self.assertIn("all done", event.text)

    def test_the_budget_signal_is_written_once_per_sprint(self) -> None:
        writer = self.writer()

        def charged(signal: bool) -> dict[str, Any]:
            budget = {"total": 12, "signal_reached": signal, "thresholds": {"signal": 12, "hard": 30}}
            return {"action": "budget_recorded", "sprint": {"ref": SPRINT, "status": "open", "budget": budget}}

        with mock.patch.object(type(writer), "_record_budget", side_effect=[charged(False), charged(True), charged(True)]):
            for number in range(3):
                writer.record_budget(role="dispatcher", actor="d", reference=SPRINT, event_type="red_ci",
                                     request_id=f"b-{number}")
        [event] = self.events.rows.values()
        self.assertEqual((event.kind, event.dedup_key), ("budget_signal", "budget_signal:sprint:1"))

    def test_the_charge_that_stops_the_sprint_writes_one_sprint_stopped(self) -> None:
        writer = self.writer()
        writer.audit = SimpleNamespace(committed_event=lambda _id: None, event=lambda _id: None)
        writer.reader = SimpleNamespace(show=lambda _ref: {"ref": SPRINT, "status": "stopped"})
        event = {
            "kind": "budget_recorded",
            "ref": SPRINT,
            "actor": {"role": "dispatcher", "id": "d"},
            "event_id": "evt-charge",
            "payload": {
                "event_type": "red_ci",
                "source_event_id": None,
                "hard_limit_stop": True,
                "budget": {"by_type": {name: 0 for name in BUDGET_EVENT_TYPES}},
            },
        }
        with (
            mock.patch.object(type(writer), "_transition_host"),
            mock.patch.object(type(writer), "_record_hard_stop"),
            mock.patch.object(type(writer), "_pending", return_value={"action": "budget_recorded"}),
        ):
            for _ in range(2):
                writer._finish_hard_budget(role="dispatcher", actor="d", reference=SPRINT, event_type="red_ci",
                                           request_id="charge-9", source_event_id="", event=event)
        [written] = self.events.rows.values()
        self.assertEqual((written.kind, written.subject_ref, written.dedup_key), ("sprint_stopped", SPRINT, "sprint_stopped:sprint:1:charge-9"))


class DispatcherProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = FakeOwnerEvents()
        self.runtime = SimpleNamespace(reader=SimpleNamespace(client=SimpleNamespace(owner_events=self.events)))

    def test_a_dead_observer_the_tick_did_not_relaunch_writes_one_notice_per_dead_head(self) -> None:
        from ummanu.dispatch import observer

        runtime = SimpleNamespace(
            **vars(self.runtime), sprints=SimpleNamespace(list=lambda statuses: [{"ref": SPRINT}])
        )
        outcomes = iter(
            [
                {"action": "observer-launch-deferred", "reason": "waiting out its backoff"},
                {"action": "observer-launch-deferred", "reason": "waiting out its backoff"},
                {"action": "observer-relaunched"},
            ]
        )
        with (
            mock.patch.object(observer, "load_observers", return_value={SPRINT: SimpleNamespace()}),
            mock.patch.object(observer, "put_observers"),
            mock.patch.object(observer, "_dead_launch", return_value=3),
            mock.patch.object(observer, "_reconcile_open_sprint", side_effect=lambda *a, **k: next(outcomes)),
        ):
            for _ in range(3):
                observer.reconcile_observers(runtime, {})
        [event] = self.events.rows.values()
        self.assertEqual((event.kind, event.subject_ref, event.dedup_key), ("observer_dead", SPRINT, "observer_dead:sprint:1:3"))
        self.assertIn("waiting out its backoff", event.text)

    def test_a_live_or_relaunched_observer_writes_nothing(self) -> None:
        from ummanu.dispatch import observer

        runtime = SimpleNamespace(
            **vars(self.runtime), sprints=SimpleNamespace(list=lambda statuses: [{"ref": SPRINT}])
        )
        with (
            mock.patch.object(observer, "load_observers", return_value={}),
            mock.patch.object(observer, "put_observers"),
            mock.patch.object(observer, "_reconcile_open_sprint", return_value={"action": "observer-launch-deferred"}),
        ):
            observer.reconcile_observers(runtime, {})
        self.assertIsNone(observer._dead_launch(None))
        self.assertEqual(self.events.rows, {})

    def test_a_head_given_up_after_its_respawn_writes_one_head_dead(self) -> None:
        from ummanu.dispatch import wait_vitality

        record_ = SimpleNamespace(attempt_id="att-1", comment_baseline=4, worker_respawns=1, review_respawns=0)
        runtime = SimpleNamespace(
            **vars(self.runtime), _stop_worker_confirmed=lambda *a, **k: None, save_records=lambda *a: None
        )
        with mock.patch.object(wait_vitality.attempt_accounting, "terminal_effect") as effect:
            for _ in range(2):
                outcome = wait_vitality._escalate_wait(
                    runtime, {"ref": REF}, record_, {REF: record_}, {}, "att-1", kind="worker", stall=900,
                    trigger="the pid heartbeat names a gone or unreaped process",
                )
        self.assertEqual(outcome["to"], "blocked")
        [event] = self.events.rows.values()
        request_id = effect.call_args.kwargs["request_id"]
        self.assertEqual((event.kind, event.subject_ref, event.dedup_key), ("head_dead", REF, f"head_dead:{request_id}"))
        self.assertIn("worker head", event.text)

    def test_doctor_writes_one_provider_red_per_provider_condition_and_day(self) -> None:
        from ummanu import cli

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "board-store.env").write_text("", encoding="utf-8")
            report = SimpleNamespace(instance_path=instance / "instance.yaml")
            rows = [
                {"resource": "claude-sub", "state": "unauthenticated", "reason": "resource authentication failed"},
                {"resource": "openai-sub", "state": "ready", "reason": "probe succeeded"},
                {"resource": "openrouter", "state": "exhausted", "reason": "resource quota is spent"},
            ]
            with mock.patch.object(owner_events.OwnerEventStore, "for_instance", return_value=self.events):
                self.assertEqual(cli.record_provider_owner_events(report, rows), 2)
                self.assertEqual(cli.record_provider_owner_events(report, rows), 0)
        kinds = sorted((event.kind, event.subject_ref, event.dedup_key.rsplit(":", 1)[0]) for event in self.events.rows.values())
        self.assertEqual(
            kinds,
            [("provider_red", None, "provider_red:claude-sub:unauthenticated"),
             ("provider_red", None, "provider_red:openrouter:exhausted")],
        )
        self.assertIn("expired", self.events.of_kind("provider_red")[0].text)

    def test_a_doctor_dry_run_records_nothing(self) -> None:
        from ummanu import cli

        with mock.patch.object(cli, "record_provider_owner_events") as recorded, mock.patch.object(
            cli, "collect_host_inventory"
        ), mock.patch.object(cli, "production_runtime_provenance_finding", return_value=None), mock.patch.object(
            cli, "dispatcher_findings", return_value=[]
        ), mock.patch.object(cli, "checkpoint_rpo_findings", return_value=[]), mock.patch.object(
            cli, "checkpoint_findings", return_value=[]
        ), mock.patch.object(cli, "secret_store_findings", return_value=[]), mock.patch.object(
            cli, "_load_dispatcher_state", return_value={}
        ), mock.patch.object(cli, "checkpoint_snapshot", return_value={}), mock.patch.object(
            cli, "collect_recovery_inventory", return_value={"resources": []}
        ), mock.patch.object(cli, "_restore_findings", return_value=[]), mock.patch.object(
            cli, "_codex_home_status", return_value={"login_missing": "", "codex_required": False}
        ), mock.patch.object(cli, "_recovery_findings", return_value=[]):
            report = SimpleNamespace(instance_path=Path("/i/instance.yaml"), data_dir=Path("/d"), warnings=[])
            for dry_run, calls in ((True, 0), (False, 1)):
                args = SimpleNamespace(offline=True, dry_run=dry_run, host=False, host_fixture=None, strict=False)
                cli.collect_doctor_inspection(report, args)
                self.assertEqual(recorded.call_count, calls)


class PoTurnFailedTests(HandedOverFixture):
    """The PO service writes `po_turn_failed` when its runner settles a turn failed; the subject is the card."""

    def test_a_failed_follow_up_writes_one_event_on_the_card_and_the_wait_says_so(self) -> None:
        runtime, session = self.submitted_card()
        events = FakeOwnerEvents()
        self.services[-1].owner_events = events
        self.hand_over()
        self.owner_says("FAIL: use the company card.", "evt-owner-1")

        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.assertEqual(self.settled(session, 3).state, "failed")

        outcome = self.tick(runtime)
        self.assertEqual(outcome["action"], "po-card-owner-answer-turn-ended")
        self.assertIn("PO execution failed; unresolved episode escalated", outcome["reason"])
        self.assertNotIn("is with the PO", outcome["reason"])
        [event] = events.rows.values()
        self.assertEqual((event.kind, event.subject_ref, event.dedup_key), ("po_turn_failed", REF, f"po_turn_failed:{session}:3"))
        self.assertIn("the owner's answer", event.text)
        # The service settles a turn once, so a later tick writes nothing more.
        self.tick(runtime)
        self.assertEqual(len(events.rows), 1)

    def test_a_failed_web_turn_names_its_session_and_a_stop_writes_nothing(self) -> None:
        service = self.start()
        events = FakeOwnerEvents()
        service.owner_events = events
        created = service.create_session(request_id="c-1", cli="claude", model="opus", effort="high")
        session = created["session_id"]
        service.submit(session_id=session, text="FAIL at once", request_id="s-1")
        self.assertEqual(self.settled(session, 1).state, "failed")
        service.submit(session_id=session, text="GATE hold", request_id="s-2")
        self.reached_gate(session, 2)
        service.stop_turn(session_id=session, seq=2)
        self.assertEqual(self.settled(session, 2).state, "interrupted")
        [event] = events.rows.values()
        self.assertEqual((event.kind, event.subject_ref), ("po_turn_failed", f"po-session:{session}"))


# --- the web ----------------------------------------------------------------------------------


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.card = {**decision_card(), "extensions": {"extra": mark_values("2026-09-26T15:00:00Z", REASON, "po")}}
        self.store = fake_for(self.card)
        record("card_handed_to_owner", REF, "handed to the owner", "h", to=self.store)
        record("sprint_closed", SPRINT, "sprint closed", "c", to=self.store)
        record("po_turn_failed", "po-session:s-9", "turn failed", "p", to=self.store)
        record("head_dead", REF, "head dead", "d", to=self.store)
        self.layer = OwnerEventLayer("/nonexistent", store=self.store)
        layers = [Recording() for _ in range(8)]
        self.app = WebApp(*layers, owner_events=self.layer)

    def get(self, path: str, query: str = "") -> str:
        response = self.app.handle("GET", path, query=query)
        self.assertEqual(response.status, 200, response.body[:400])
        return response.body.decode("utf-8")

    def post(self, path: str, body: bytes = b"") -> Any:
        return self.app.handle("POST", path, body=body)

    def test_the_bell_shows_the_unread_count_on_every_page(self) -> None:
        for path in ("/owner-events", "/doctor", "/history"):
            with self.subTest(path=path):
                page = self.get(path)
                self.assertEqual(page.count('id="owner-bell"'), 1)
                self.assertIn('<span class="bell-count">4</span>', page)
        self.store.mark_all_read()
        self.assertIn('<span class="bell-count">1</span>', self.get("/doctor"))

    def test_the_list_pins_the_open_needs_owner_event_and_orders_newest_first(self) -> None:
        document = self.layer.owner_event_list()
        self.assertEqual(
            [event["kind"] for event in document["events"]],
            ["card_handed_to_owner", "head_dead", "po_turn_failed", "sprint_closed"],
        )
        page = self.get("/owner-events")
        positions = [page.index(f"<code>{kind}</code>") for kind in ("card_handed_to_owner", "head_dead", "sprint_closed")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("needs the owner", page)
        self.assertIn(f'href="/tasks/{REF}"', page)
        self.assertIn('href="/sprints/sprint%3A1"', page)
        self.assertIn('href="/po/sessions/s-9"', page)
        self.assertIn("current handover has no recorded owner answer", page)
        self.assertEqual(page.count("Mark read</button>"), 3, "every unread notice has its button; the held one has none")
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', page)

    def filter_marks(self, page: str) -> dict[str, str]:
        """The two filter links as {label: href}, plus `current`: the one marked `aria-current`."""
        found = re.findall(r'<a href="(/owner-events[^"]*)"( aria-current="true")?>(Unread|All)</a>', page)
        return {**{label: href for href, _mark, label in found}, "current": ",".join(label for _h, mark, label in found if mark)}

    def test_the_list_opens_on_the_unread_and_all_is_one_link_away(self) -> None:
        """secretary-1778: no query is the unread view, `?all=1` is every event, `?unread=1` still works."""
        self.store.mark_read(self.store.of_kind("sprint_closed")[0].id)
        links = {"Unread": "/owner-events", "All": "/owner-events?all=1"}
        for query in ("", "unread=1", "all=0", "all=garbage", "unread=nonsense", "other=1"):
            with self.subTest(query=query):
                page = self.get("/owner-events", query)
                self.assertEqual(self.filter_marks(page), {**links, "current": "Unread"})
                self.assertNotIn("<code>sprint_closed</code>", page)
                self.assertIn("<code>head_dead</code>", page)
                # The open `needs_owner` event is in the unread view, pinned at the top.
                self.assertIn('<li class="unread pinned"', page)
                self.assertLess(page.index("<code>card_handed_to_owner</code>"), page.index("<code>head_dead</code>"))
                self.assertIn('<input type="hidden" name="view" value="unread">', page)
                self.assertNotIn('value="all"', page)
        for query in ("all=1", "all=true", "all=1&unread=1"):
            with self.subTest(query=query):
                page = self.get("/owner-events", query)
                self.assertEqual(self.filter_marks(page), {**links, "current": "All"})
                self.assertIn("<code>sprint_closed</code>", page)
                self.assertIn("<code>card_handed_to_owner</code>", page)
                self.assertIn('<input type="hidden" name="view" value="all">', page)
                self.assertNotIn('value="unread"', page)

    def test_the_read_buttons_return_to_the_view_they_were_pressed_from(self) -> None:
        closed, failed = (self.store.of_kind(kind)[0] for kind in ("sprint_closed", "po_turn_failed"))
        # The forms each view draws post its own field back, so replay exactly what the page carries.
        for query, back in (("all=1", "/owner-events?all=1"), ("", "/owner-events")):
            with self.subTest(view=query or "default"):
                page = self.get("/owner-events", query)
                [field] = set(re.findall(r'<input type="hidden" name="view" value="(\w+)">', page))
                body = f"view={field}".encode()
                response = self.post(f"/owner-events/{(closed if query else failed).id}/read", body)
                self.assertEqual((response.status, response.headers["Location"]), (303, back))
                response = self.post("/owner-events/read-all", body)
                self.assertEqual(response.status, 303)
                self.assertTrue(response.headers["Location"].startswith(back + ("&" if "?" in back else "?") + "marked="))
                self.assertIn("Marked", response.body.decode())
        # A missing or unknown view returns to the unread default.
        for body in (b"", b"view=", b"view=bogus", b"view=unread"):
            with self.subTest(body=body):
                response = self.post("/owner-events/read-all", body)
                self.assertEqual((response.status, response.headers["Location"]), (303, "/owner-events?marked=0"))
                response = self.post(f"/owner-events/{closed.id}/read", body)
                self.assertEqual((response.status, response.headers["Location"]), (303, "/owner-events"))

    def test_mark_read_and_mark_all_keep_the_held_event(self) -> None:
        closed = self.store.of_kind("sprint_closed")[0]
        self.assertEqual(self.post(f"/owner-events/{closed.id}/read", b"view=all").status, 303)
        self.assertNotIn("<code>sprint_closed</code>", self.get("/owner-events"))
        self.assertIn("<code>sprint_closed</code>", self.get("/owner-events", "all=1"))

        held = self.store.of_kind("card_handed_to_owner")[0]
        self.assertEqual(self.post(f"/owner-events/{held.id}/read").status, 409)
        self.assertEqual(self.post("/owner-events/999/read").status, 404)
        self.assertEqual(self.post("/owner-events/read-all").status, 303)
        self.assertEqual(self.layer.unread_count()["count"], 1)
        self.assertIsNone(self.store.rows[held.id].read_at)
        self.assertEqual(self.post("/owner-events/read-all", b"other=1").status, 400)
        # The old filter field is no longer part of the form.
        self.assertEqual(self.post("/owner-events/read-all", b"unread=1").status, 400)

    def test_bulk_feedback_accounts_for_unheld_steward_with_no_notices(self) -> None:
        self.store.rows.clear()
        record("steward_needs_human", REF, "Steward needs a routing decision", "steward", to=self.store)
        [steward] = self.store.events()
        page = self.get("/owner-events")
        self.assertIn("No unread notices to mark.", page)
        self.assertIn("Bulk read marks notices only.", page)
        self.assertIn('disabled title="No unread notices', page)
        self.assertIn(f'action="/owner-events/{steward.id}/read"', page)
        result = self.layer.mark_all_read()
        self.assertEqual((result["marked"], result["needs_owner_count"], result["held_count"]), (0, 1, 0))
        response = self.post("/owner-events/read-all")
        self.assertIn("1 owner-attention events remain", response.body.decode())
        self.assertIn("1 can be marked read individually", self.get("/owner-events", "marked=0"))
        self.assertEqual(self.post(f"/owner-events/{steward.id}/read").status, 303)
        self.assertEqual(self.layer.unread_count()["count"], 0)

    def test_snapshot_counts_all_needs_owner_beyond_the_bounded_event_list(self) -> None:
        for index in range(501):
            record("steward_needs_human", REF, "Steward needs a routing decision", f"steward-{index}", to=self.store)
        document = self.layer.owner_event_list()
        self.assertEqual(len(document["events"]), 500)
        self.assertEqual((document["unread"], document["notice_count"], document["needs_owner_count"], document["held_count"]), (505, 3, 502, 1))
        result = self.layer.mark_all_read()
        self.assertEqual((result["marked"], result["remaining"], result["needs_owner_count"], result["held_count"]), (3, 502, 502, 1))

    def test_bulk_feedback_distinguishes_held_and_unheld_needs_owner(self) -> None:
        record("steward_needs_human", REF, "Steward needs a routing decision", "steward", to=self.store)
        result = self.layer.mark_all_read()
        self.assertEqual((result["marked"], result["remaining"], result["needs_owner_count"], result["held_count"]), (3, 2, 2, 1))
        document = self.layer.owner_event_list()
        self.assertEqual((document["notice_count"], document["needs_owner_count"], document["held_count"]), (0, 2, 1))
        page = self.get("/owner-events", "marked=3")
        for text in ("Marked 3 notices read", "2 owner-attention events remain", "1 held by an unanswered handover", "1 can be marked read individually", "current handover has no recorded owner answer"):
            self.assertIn(text, page)
        held = self.store.of_kind("card_handed_to_owner")[0]
        steward = self.store.of_kind("steward_needs_human")[0]
        self.assertEqual(self.post(f"/owner-events/{held.id}/read").status, 409)
        self.assertEqual(self.post(f"/owner-events/{steward.id}/read").status, 303)

    def test_the_bell_leads_to_the_unread_view_in_every_state(self) -> None:
        def bell_href() -> str:
            [href] = re.findall(r'<a class="bell[^"]*" id="owner-bell" href="([^"]*)"', self.get("/doctor"))
            return href

        self.assertEqual(bell_href(), "/owner-events")  # some unread
        for event in list(self.store.rows.values()):
            self.store.rows[event.id] = replace(event, read_at=event.created_at)
        self.assertIn('<span class="bell-count">0</span>', self.get("/doctor"))
        self.assertEqual(bell_href(), "/owner-events")  # nothing unread
        self.store.missing_table = True
        self.assertIn('<span class="bell-count">?</span>', self.get("/doctor"))
        self.assertEqual(bell_href(), "/owner-events")  # unknown

    def test_a_board_without_the_table_reads_as_no_events_and_the_writes_refuse(self) -> None:
        self.store.missing_table = True
        document = self.layer.owner_event_list()
        self.assertEqual((document["source"]["state"], document["events"]), ("unavailable", []))
        self.assertIn("migration 0018", document["source"]["reason"])
        page = self.get("/owner-events")
        self.assertIn("could not find out the owner events", page)
        self.assertIn('<span class="bell-count">?</span>', page)
        self.assertEqual(self.post("/owner-events/read-all").status, 503)
        with self.assertRaises(RuntimeUnavailable):
            self.layer.mark_read(1)
        with self.assertRaises(OwnerEventMissing):
            OwnerEventLayer("/x", store=FakeOwnerEvents()).mark_read("x")

    def test_a_process_built_without_the_layer_draws_no_bell(self) -> None:
        app = WebApp(*(Recording() for _ in range(8)))
        page = app.handle("GET", "/doctor").body.decode("utf-8")
        self.assertNotIn('id="owner-bell"', page)
        self.assertEqual(app.handle("GET", "/owner-events").status, 503)

    def test_the_count_and_list_come_from_the_board_on_every_request(self) -> None:
        self.assertIn('<span class="bell-count">4</span>', self.get("/doctor"))
        record("provider_red", None, "provider red", "r", to=self.store)
        self.assertIn('<span class="bell-count">5</span>', self.get("/doctor"))

    def test_the_list_page_works_at_phone_width(self) -> None:
        self.assertIn("ol.owner-events li { display: flex; flex-wrap: wrap;", pages.STYLE)
        self.assertIn("overflow-wrap: anywhere", pages.STYLE)


# --- the carried fixes ------------------------------------------------------------------------


class CardCommentRoleTests(unittest.TestCase):
    """(a) The dashboard's comment form writes as the owner on a card that waits for the owner."""

    def writer(self, card: dict[str, Any], recorded: dict[str, Any] | None = None) -> Any:
        return SimpleNamespace(
            reader=SimpleNamespace(show=lambda _ref: card),
            audit=SimpleNamespace(committed_event=lambda _id: recorded, pending_event=lambda _id: None),
        )

    def test_the_role_follows_the_mark_and_a_replay_keeps_its_first_role(self) -> None:
        from ummanu.webproto.card_ops import _comment_role

        marked = {"ref": REF, "extensions": {"extra": mark_values("2026-09-26T15:00:00Z", REASON, "po")}}
        plain = {"ref": REF, "extensions": {"extra": {}}}
        self.assertEqual(_comment_role(self.writer(marked), "po", REF, "r-1"), "owner")
        self.assertEqual(_comment_role(self.writer(plain), "po", REF, "r-1"), "po")
        self.assertEqual(
            _comment_role(self.writer(plain, {"payload": {"marker": "owner"}}), "po", REF, "r-1"), "owner"
        )
        self.assertEqual(_comment_role(self.writer(marked), "observer", REF, "r-1"), "observer")

    def test_the_layer_writes_the_owners_comment_through_the_writer(self) -> None:
        from ummanu.webproto.card_ops import CardOperationLayer

        with tempfile.TemporaryDirectory() as tmp:
            card = decision_card()
            client = OneCardClient(card, tmp)
            layer = CardOperationLayer(tmp, data_dir=tmp, board_client=client, clock=lambda: 0.0)
            writer = TaskWriter(client, data_dir=tmp)  # type: ignore[arg-type]
            writer.audit = MemoryAudit()
            writer.reader = mock.Mock(show=lambda reference: copy.deepcopy(card))
            with (
                mock.patch.object(CardOperationLayer, "report", return_value=SimpleNamespace(data_dir=Path(tmp))),
                mock.patch("ummanu.webproto.card_ops.TaskWriter", return_value=writer),
                mock.patch("ummanu.tasks._task_number", return_value=1900),
            ):
                writer.handover(role="po", actor="po", reference=REF, to="owner", reason=REASON, request_id="real-handover")
                layer.task_comment(request_id="r-1", actor="web", reference=REF, body="Yes, pay it.")
        self.assertEqual(card["comments"][-1]["body"], "[owner]\nYes, pay it.")
        event = writer.audit.committed_event("r-1")
        self.assertEqual(event["actor"], {"role": "owner", "id": "owner"})

    def test_the_card_page_says_the_form_answers_as_the_owner(self) -> None:
        value = {"ref": REF, "title": "The decision", "state": "in_progress",
                 "waiting_owner": {"since": "2026-09-26T15:00:00Z", "reason": REASON, "by": "po"}}
        html = pages.task(
            {"ref": REF, "card": {"source": None, "value": value}, "project": {}, "events": {}, "agents": {}}, runs={}
        )
        self.assertIn("Answer the PO as the owner", html)


class OwnerEventsCliTests(unittest.TestCase):
    """`ummanu owner-events list`: the list page's read, through the read role, marking nothing."""

    def run_cli(self, *argv: str, store: Any) -> tuple[int, str, str]:
        import contextlib
        import io

        from ummanu.cli import main

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(owner_events.OwnerEventStore, "for_instance", return_value=store) as opened,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main(["owner-events", "list", "--instance", "/srv/instance", *argv])
        self.assertEqual(opened.call_args.kwargs, {"role": "read"})
        return code, out.getvalue(), err.getvalue()

    def test_it_lists_what_needs_the_owner_first_and_marks_nothing(self) -> None:
        card = {**decision_card(), "extensions": {"extra": mark_values("2026-09-26T15:00:00Z", REASON, "po")}}
        store = fake_for(card)
        record("sprint_closed", SPRINT, "closed", "c", to=store)
        record("card_handed_to_owner", REF, "handed\nsecond line", "h", to=store)
        code, out, _err = self.run_cli(store=store)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "2 unread")
        self.assertRegex(lines[1], rf"^\* #2 \S+ needs_owner card_handed_to_owner {REF}$")
        self.assertEqual(lines[2:4], ["    handed", "    second line"])
        self.assertIn("notice sprint_closed sprint:1", out)
        self.assertEqual(store.unread_count(), 2)
        code, out, _err = self.run_cli("--json", "--unread", store=store)
        self.assertEqual(code, 0)
        import json

        document = json.loads(out)
        self.assertEqual((document["unread"], [event["kind"] for event in document["events"]]),
                         (2, ["card_handed_to_owner", "sprint_closed"]))

    def test_a_board_without_the_table_exits_one_with_the_reason(self) -> None:
        store = FakeOwnerEvents()
        store.missing_table = True
        code, _out, err = self.run_cli(store=store)
        self.assertEqual(code, 1)
        self.assertIn("migration 0018", err)


class DispatcherRecordsLoadUnchangedTests(unittest.TestCase):
    """A dispatcher record of a PO card written before the bell loads and saves byte for byte."""

    def test_a_pre_bell_po_record_round_trips(self) -> None:
        from ummanu.dispatch.state import DispatcherRecord

        document = {
            "worker": "po", "workspace": "", "handle": "", "head": "", "review_head": "", "attempt_id": "a-1",
            "comment_baseline": 0, "review_baseline": 0, "state": "po_submitted", "claimed_at": 1.0,
        }
        loaded = DispatcherRecord.from_json(document)
        self.assertEqual(DispatcherRecord.from_json(loaded.to_json()).to_json(), loaded.to_json())
        self.assertEqual(loaded.state, "po_submitted")


if __name__ == "__main__":
    unittest.main()
