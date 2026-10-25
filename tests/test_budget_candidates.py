"""The budget pass reads its uncharged candidate set, not the audit's history (secretary-1661).

`_reconcile_sprint_budget` used to read every committed `requests` row each tick and classify it in
Python. It now reads a page of `SqlTaskAudit.uncharged_budget_candidates`: committed events meeting
the classifier's necessary conditions (`budget_candidates.CANDIDATE_PREDICATE`) whose charge id
`sprint-budget-<identity>` has no committed record. The proofs, over a real PostgreSQL 16:

* **Nothing the classifier charges is missed.** Fixtures spanning every kind, both event shapes and
  every transition shape: each event `_budget_event_type` types (or refuses as a malformed taxonomy)
  is a candidate, and the file journal's Python predicate answers exactly what the SQL answers.
* **Late commit.** A record committed by a second connection after a tick ran, behind a record that
  tick already charged in claim order, is charged on the next tick.
* **Deferred for any length of time.** A card whose lookup fails for many ticks is charged exactly
  once when it recovers.
* **Exactly once, and the same charges.** The pre-secretary-1661 full scan is kept below as the
  oracle (`_oracle_reconcile`, verbatim but for its writer); over a realistic fixture drained a small
  page per tick, the charges and unlinked markers equal the oracle's, repeated ticks add nothing, and
  the oracle run afterwards finds nothing left to charge.
* **Cost.** The candidate read touches the same rows over ten times the unrelated history, walks
  `requests_budget_candidates` and never scans `requests`; without the index the answers are the same.

Like the other `*_sql_backend` suites this needs Docker and never skips.
"""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import random
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board import budget_candidates, sql_audit
from ummanu.board.migrate import SCRIPT_LOCATION
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.terminal_taxonomy import TerminalTaxonomyValidationError
from ummanu.dispatch import production
from ummanu.dispatch.production import (
    _budget_event_type,
    _event_sprint,
    _reconcile_sprint_budget,
    _record_unlinked_budget_event,
)
from ummanu.tasks import TaskError

BOARD: PostgresBoard


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


PROTOCOL = "board.protocol_event"
SPRINT = "sprint:1"
_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)


def _taxonomy(disposition: str, budget_class: str | None, *, reason: str | None = None) -> dict[str, Any]:
    return {
        "version": 2,
        "disposition": disposition,
        "blocked_reason": reason,
        "source_evidence": reason,
        "budget_class": budget_class,
        "provenance": "forward",
    }


TAXONOMIES: dict[str, Any] = {
    "none": None,
    "reslice": _taxonomy("reslice", "blocked"),
    "infrastructure": _taxonomy("blocked", "infrastructure_blocked", reason="infrastructure"),
    "drop": _taxonomy("drop", None),
    "malformed": {"version": 1},
}


def _event(
    request_id: str,
    *,
    shape: str,
    kind: str,
    ref: str = "ummanu-1",
    marker: str | None = None,
    target: str | None = None,
    source: str | None = None,
    budget_event: str | None = None,
    taxonomy: str = "none",
    sprint: str | None = None,
) -> dict[str, Any]:
    """One committed record, in the generic `kind`/`payload` shape or the typed protocol shape."""
    body: dict[str, Any] = {"note": "x" * 400}
    if marker is not None:
        body["marker"] = marker
    if budget_event is not None:
        body["budget_event"] = budget_event
    if TAXONOMIES[taxonomy] is not None:
        body["terminal_taxonomy"] = TAXONOMIES[taxonomy]
    if sprint is not None:
        body["sprint"] = sprint
    record: dict[str, Any] = {
        "request_id": request_id,
        "event_id": "evt_" + request_id,
        "ref": ref,
        "kind": kind,
        "outcome": "success",
        "actor": {"role": "dispatcher", "id": "fixture"},
    }
    if shape == "typed":
        record["record_type"] = PROTOCOL
        record["data"] = body
        if target is not None or source is not None:
            record["transition"] = {"source": source, "target": target}
    else:
        if target is not None:
            body["to"] = target
        if source is not None:
            body["from"] = source
        record["payload"] = body
    return record


def _typed(event: dict[str, Any]) -> bool:
    """Whether the classifier answers anything for it: a type, or a refused taxonomy."""
    try:
        return _budget_event_type(event) is not None
    except TerminalTaxonomyValidationError:
        return True


class _Store:
    """One fresh migrated database, its client and audit, and a clock one second per claim."""

    def __init__(self, case: unittest.TestCase) -> None:
        root = tempfile.TemporaryDirectory()
        case.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.config = BOARD.fresh_database().for_role("owner")
        self.client = self.connect(case)
        self.audit = SqlTaskAudit(self.client)
        self.clock = _EPOCH
        patch = mock.patch.object(sql_audit, "_now", self.now)
        patch.start()
        case.addCleanup(patch.stop)

    def connect(self, case: unittest.TestCase) -> SqlCardClient:
        client = SqlCardClient(self.config, self.root)
        case.addCleanup(client.close)
        return client

    def now(self) -> datetime:
        self.clock += timedelta(seconds=1)
        return self.clock

    def seed(self, records: list[dict[str, Any]]) -> None:
        with self.client.transaction():
            for record in records:
                self.audit._claim_row(record["request_id"], record, status="committed")
        self.client._execute("ANALYZE requests")
        self.client._commit_unless_nested()

    def charge_ids(self) -> dict[str, str]:
        rows = self.client._query(
            "SELECT request_id, intent->>'kind' FROM requests "
            "WHERE status = 'committed' AND request_id LIKE 'sprint-budget-%%'"
        )
        self.client._commit_unless_nested()
        return dict(rows)


class _Writer:
    """`SprintWriter.record_budget`'s part the pass depends on: a charge committed under its id."""

    def __init__(self, audit: Any) -> None:
        self.audit = audit
        self.charges: list[tuple[str, str, str]] = []

    def record_budget(
        self, *, role: str, actor: str, reference: str, event_type: str, request_id: str, source_event_id: str
    ) -> dict[str, Any]:
        self.charges.append((source_event_id, reference, event_type))
        self.audit.append(
            request_id,
            {
                "event_id": "evt_" + request_id,
                "request_id": request_id,
                "ref": reference,
                "kind": "budget_recorded",
                "outcome": "success",
                "actor": {"role": role, "id": actor},
                "payload": {"event_type": event_type, "source_event_id": source_event_id},
            },
        )
        return {"sprint": {"status": "open"}}


class _Reader:
    """Card lookups: a sprint per card, and cards whose lookup fails while they are listed."""

    def __init__(self, links: dict[str, str]) -> None:
        self.links = links
        self.failing: set[str] = set()
        self.client = None

    def show(self, reference: str) -> dict[str, Any]:
        if reference in self.failing:
            raise TaskError("unavailable", "the board did not answer", 1)
        return {"ref": reference, "sprint": self.links.get(reference)}


def _runtime(audit: Any, links: dict[str, str], root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        catalog=SimpleNamespace(instance={}),
        reader=_Reader(links),
        audit=audit,
        data_dir=root,
        owner="dispatcher",
    )


@contextlib.contextmanager
def _writer(audit: Any) -> Any:
    writer = _Writer(audit)
    with mock.patch.object(production, "SprintWriter", lambda *args, **kwargs: writer):
        yield writer


def _oracle_reconcile(runtime: Any) -> list[dict[str, Any]]:
    """`_reconcile_sprint_budget` as it was before secretary-1661: the whole audit, every tick."""
    writer = production.SprintWriter(runtime.reader.client)
    events = runtime.audit.events()
    committed = {str(event.get("request_id") or "") for event in events}
    outcomes: list[dict[str, Any]] = []
    sprint_cache: dict[str, str | None] = {}
    for event in events:
        reference = str(event.get("ref") or "")
        if not reference or reference.startswith("sprint:"):
            continue
        try:
            event_type = _budget_event_type(event)
        except TerminalTaxonomyValidationError as exc:
            outcomes.append({"status": "degraded", "action": "terminal-taxonomy-invalid", "reason": str(exc)})
            continue
        if event_type is None:
            continue
        identity = str(event.get("event_id") or event.get("request_id") or "")
        if not identity:
            continue
        request_id = "sprint-budget-" + identity
        if request_id in committed:
            continue
        sprint = _event_sprint(runtime, event, sprint_cache)
        if sprint is None:
            continue
        if not sprint:
            _record_unlinked_budget_event(runtime, event, request_id, identity, event_type)
            continue
        writer.record_budget(
            role="dispatcher",
            actor=runtime.owner,
            reference=sprint,
            event_type=event_type,
            request_id=request_id,
            source_event_id=identity,
        )
        outcomes.append({"status": "ok", "event_type": event_type})
    return outcomes


# fixtures -------------------------------------------------------------------------------------


def _every_shape() -> list[dict[str, Any]]:
    """The positive shapes by hand, then a seeded sample over every axis the classifier reads."""
    named = [
        _event("red-generic", shape="generic", kind="verdict", marker="review:red"),
        _event("red-typed", shape="typed", kind="card.verdict", marker="review:red"),
        _event("red-typed-verdict", shape="typed", kind="verdict", marker="review:red"),
        _event("blocked-generic", shape="generic", kind="moved", target="blocked"),
        _event("blocked-generic-infra", shape="generic", kind="moved", target="blocked")
        | {"request_id": "dispatcher-attempt-1-bringup-blocked"},
        _event("blocked-typed", shape="typed", kind="card.blocked", target="blocked", taxonomy="reslice"),
        _event(
            "blocked-typed-infra",
            shape="typed",
            kind="card.blocked",
            target="blocked",
            taxonomy="infrastructure",
        ),
        _event("blocked-typed-legacy", shape="typed", kind="card.blocked", target="blocked"),
        _event("blocked-typed-none", shape="typed", kind="card.blocked", target="blocked", taxonomy="drop"),
        _event(
            "blocked-typed-bad", shape="typed", kind="card.blocked", target="blocked", taxonomy="malformed"
        ),
        _event("blocked-generic-bad", shape="generic", kind="moved", target="blocked", taxonomy="malformed"),
        _event("preempt-generic", shape="generic", kind="moved", source="validate", target="ready"),
        _event("preempt-typed", shape="typed", kind="card.moved", source="assessment", target="ready"),
        _event("gate-red-generic", shape="generic", kind="moved", target="in_progress"),
        _event("x-gate-red-typed", shape="typed", kind="card.moved", source="validate", target="in_progress"),
        _event("hotfix-generic", shape="generic", kind="created", budget_event="hotfix"),
        _event("recreated-typed", shape="typed", kind="created", budget_event="recreated_task"),
    ]
    rng = random.Random(1661)
    axes = {
        "shape": ("generic", "typed"),
        "kind": (
            "verdict",
            "card.verdict",
            "moved",
            "card.moved",
            "card.blocked",
            "created",
            "card.created",
            "commented",
            "claimed",
            "reported",
        ),
        "ref": ("ummanu-1", "ummanu-2", "sprint:1", ""),
        "marker": (None, "review:red", "review:green"),
        "target": (None, "blocked", "ready", "in_progress", "done", "validate", "assessment"),
        "source": (None, "in_progress", "validate", "ci", "review", "assessment", "ready", "blocked"),
        "budget_event": (None, "hotfix", "recreated_task", "other"),
        "taxonomy": tuple(TAXONOMIES),
        "request": ("plain", "gate-red", "dispatcher-attempt-1-bringup-blocked"),
    }
    sampled = []
    for index in range(3000):
        choice = {name: rng.choice(values) for name, values in axes.items()}
        request = choice.pop("request")
        record = _event(f"{request}-{index}", **choice)
        sampled.append(record)
    return named + sampled


def _realistic() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """A sprint's life: linked and unlinked cards, green cycles, every budget event, noise."""
    links = {"ummanu-1": SPRINT, "ummanu-2": SPRINT, "ummanu-3": "sprint:2", "ummanu-9": ""}
    records: list[dict[str, Any]] = []
    serial = itertools.count()

    def add(**fields: Any) -> None:
        fields.setdefault("shape", "generic")
        records.append(_event(f"r{next(serial)}-{fields.pop('name', 'event')}", **fields))

    for card in links:
        add(kind="created", ref=card)
        add(kind="moved", ref=card, source="ready", target="in_progress")
        add(kind="commented", ref=card)
        add(kind="verdict", ref=card, marker="review:green")
        add(kind="verdict", ref=card, marker="review:red")
        add(shape="typed", kind="card.verdict", ref=card, marker="review:red")
        add(kind="moved", ref=card, source="validate", target="ready")
        add(name="gate-red", kind="moved", ref=card, source="validate", target="in_progress")
        add(shape="typed", kind="card.blocked", ref=card, target="blocked", taxonomy="reslice")
        add(shape="typed", kind="card.blocked", ref=card, target="blocked", taxonomy="drop")
        add(shape="typed", kind="card.blocked", ref=card, target="blocked", taxonomy="malformed")
        add(kind="moved", ref=card, source="in_progress", target="done")
    add(kind="created", ref="ummanu-4", budget_event="hotfix", sprint=SPRINT)
    add(shape="typed", kind="created", ref="ummanu-5", budget_event="recreated_task", sprint="")
    add(kind="moved", ref=SPRINT, target="blocked")
    return records, links


def _noise(count: int, *, offset: int = 0) -> list[dict[str, Any]]:
    kinds = ("commented", "claimed", "routing", "reported")
    return [
        _event(
            f"noise-{offset + index}",
            shape="generic",
            kind=kinds[index % len(kinds)],
            ref=f"other-{index % 53}",
        )
        for index in range(count)
    ]


# the proofs -----------------------------------------------------------------------------------


class CandidateCoverageTests(unittest.TestCase):
    """AC1: every event the classifier types is a candidate, on both backends' definitions."""

    def test_every_typed_event_is_in_the_sql_candidate_set(self) -> None:
        store = _Store(self)
        fixtures = _every_shape()
        store.seed(fixtures)
        found = {event["request_id"] for event in store.audit.uncharged_budget_candidates(limit=10_000)}

        typed = [
            event
            for event in fixtures
            if _typed(event) and event["ref"] and not event["ref"].startswith("sprint:")
        ]
        types: set[str] = set()
        for event in typed:
            with contextlib.suppress(TerminalTaxonomyValidationError):
                types.add(str(_budget_event_type(event)))
        self.assertEqual(
            types,
            {
                "red_review",
                "blocked",
                "infrastructure_blocked",
                "preempt",
                "red_ci",
                "recreated_task",
                "hotfix",
            },
        )
        self.assertEqual([event["request_id"] for event in typed if event["request_id"] not in found], [])
        # The file journal's predicate is the same set, record by record.
        self.assertEqual(
            [
                event["request_id"]
                for event in fixtures
                if budget_candidates.is_candidate(event) != (event["request_id"] in found)
            ],
            [],
        )
        # A superset, and a narrow one: some candidates classify to nothing, most records are not candidates.
        self.assertTrue(any(not _typed(event) for event in fixtures if event["request_id"] in found))
        self.assertLess(len(found), len(fixtures) // 2)

    def test_the_index_predicate_is_the_query_predicate(self) -> None:
        """The planner uses the partial index only while the query's predicate implies its own."""
        path = SCRIPT_LOCATION / "versions" / "0037_stage_budget_candidates.py"
        spec = importlib.util.spec_from_file_location("revision_stage_budget", path)
        assert spec is not None and spec.loader is not None
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        self.assertEqual(revision.CANDIDATE_PREDICATE, budget_candidates.CANDIDATE_PREDICATE)


class TickTests(unittest.TestCase):
    """AC2–AC4: the pass over the candidate set, tick by tick."""

    def test_an_event_committed_after_a_tick_is_charged_on_the_next(self) -> None:
        """A second connection's record claimed first and committed last is still charged."""
        store = _Store(self)
        late = store.connect(self)
        late_audit = SqlTaskAudit(late)
        runtime = _runtime(store.audit, {"ummanu-1": SPRINT}, store.root)
        with _writer(store.audit) as writer, contextlib.ExitStack() as held:
            held.enter_context(late.transaction())
            late_audit._claim_row(
                "late-red",
                _event("late-red", shape="generic", kind="verdict", marker="review:red"),
                status="committed",
            )
            store.audit.append(
                "on-time-red", _event("on-time-red", shape="typed", kind="card.verdict", marker="review:red")
            )
            self.assertEqual(len(_reconcile_sprint_budget(runtime)), 1)
            self.assertEqual([charge[0] for charge in writer.charges], ["evt_on-time-red"])
            held.close()  # the late record commits now, behind the one the tick charged

            order = [
                row[0]
                for row in store.client._query(
                    "SELECT request_id FROM requests WHERE request_id IN ('late-red', 'on-time-red') "
                    "ORDER BY settled_at, created_at, request_id"
                )
            ]
            self.assertEqual(order, ["late-red", "on-time-red"], "a cursor would have passed it")

            _reconcile_sprint_budget(runtime)
            _reconcile_sprint_budget(runtime)

        self.assertEqual([charge[0] for charge in writer.charges], ["evt_on-time-red", "evt_late-red"])

    def test_an_event_deferred_for_many_ticks_is_charged_once_when_its_card_recovers(self) -> None:
        store = _Store(self)
        store.seed(
            [
                _event("stuck-red", shape="generic", kind="verdict", ref="ummanu-2", marker="review:red"),
                _event("fine-red", shape="generic", kind="verdict", ref="ummanu-1", marker="review:red"),
            ]
        )
        runtime = _runtime(store.audit, {"ummanu-1": SPRINT, "ummanu-2": SPRINT}, store.root)
        runtime.reader.failing.add("ummanu-2")
        with _writer(store.audit) as writer:
            for tick in range(30):
                if tick % 7 == 3:
                    store.seed(_noise(20, offset=tick * 20))
                _reconcile_sprint_budget(runtime)
            self.assertEqual(writer.charges, [("evt_fine-red", SPRINT, "red_review")])
            self.assertEqual(
                [event["request_id"] for event in store.audit.uncharged_budget_candidates(limit=10)],
                ["stuck-red"],
            )

            runtime.reader.failing.clear()
            for _tick in range(5):
                _reconcile_sprint_budget(runtime)

        self.assertEqual(
            writer.charges, [("evt_fine-red", SPRINT, "red_review"), ("evt_stuck-red", SPRINT, "red_review")]
        )
        self.assertEqual(store.audit.uncharged_budget_candidates(limit=10), [])

    def test_the_charges_equal_the_full_scan_oracle_and_repeat_nothing(self) -> None:
        records, links = _realistic()
        interleaved = []
        noise = _noise(len(records) * 3)
        for index, record in enumerate(records):
            interleaved += noise[index * 3 : index * 3 + 3] + [record]

        oracle_store = _Store(self)
        oracle_store.seed(interleaved)
        with _writer(oracle_store.audit) as oracle:
            _oracle_reconcile(_runtime(oracle_store.audit, links, oracle_store.root))
            self.assertEqual(
                _oracle_reconcile(_runtime(oracle_store.audit, links, oracle_store.root))[-1:],
                [{"status": "degraded", "action": "terminal-taxonomy-invalid", "reason": mock.ANY}],
            )
        oracle_markers = oracle_store.charge_ids()

        store = _Store(self)
        store.seed(interleaved)
        runtime = _runtime(store.audit, links, store.root)
        ticks = 0
        with _writer(store.audit) as writer, mock.patch.object(production, "BUDGET_CANDIDATE_PAGE", 5):
            while store.audit.uncharged_budget_candidates(limit=1):
                _reconcile_sprint_budget(runtime)
                ticks += 1
                self.assertLess(ticks, 100)
            for _tick in range(3):
                self.assertEqual(_reconcile_sprint_budget(runtime), [])
            # The whole-audit oracle over what the new pass left finds nothing to charge.
            self.assertEqual(
                [row for row in _oracle_reconcile(runtime) if row["status"] == "ok"],
                [],
            )
        self.assertGreater(ticks, 3, "the backlog drains a page per tick")
        self.assertEqual(sorted(writer.charges), sorted(oracle.charges))
        self.assertEqual(len(set(writer.charges)), len(writer.charges))
        self.assertTrue(
            {"red_review", "preempt", "red_ci", "blocked", "hotfix"} <= {c[2] for c in writer.charges}
        )

        markers = store.charge_ids()
        unclassified = {rid for rid, kind in markers.items() if kind == "budget_unclassified"}
        self.assertEqual(
            {rid: kind for rid, kind in markers.items() if rid not in unclassified}, oracle_markers
        )
        self.assertIn("budget_unlinked", set(oracle_markers.values()))
        # The markers the oracle never wrote are exactly the candidates it re-read every tick.
        self.assertEqual(
            unclassified,
            {
                "sprint-budget-" + record["event_id"]
                for record in records
                if budget_candidates.is_candidate(record) and not _classified(record)
            },
        )


def _classified(event: dict[str, Any]) -> bool:
    try:
        return _budget_event_type(event) is not None
    except TerminalTaxonomyValidationError:
        return False


class CostTests(unittest.TestCase):
    """AC5: the rows read per tick do not follow unrelated history, and no plan scans `requests`."""

    N = 300

    def store(self, unrelated: int) -> _Store:
        store = _Store(self)
        records, _links = _realistic()
        noise = _noise(unrelated)
        per = unrelated // len(records)
        sequence: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            sequence += noise[index * per : (index + 1) * per] + [record]
        sequence += noise[len(records) * per :]
        # Half the candidates already charged, so the anti-join has both answers to give.
        for record in records[::2]:
            charge_id = "sprint-budget-" + record["event_id"]
            sequence.append(_event(charge_id, shape="generic", kind="budget_recorded", ref=SPRINT))
        store.seed(sequence)
        return store

    @staticmethod
    def candidate_query(store: _Store) -> tuple[str, tuple[Any, ...]]:
        issued: list[tuple[str, tuple[Any, ...]]] = []
        original = store.client._query

        def recording(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
            issued.append((sql, params))
            return original(sql, params)

        with mock.patch.object(store.client, "_query", recording):
            store.audit.uncharged_budget_candidates(limit=production.BUDGET_CANDIDATE_PAGE)
        (query,) = issued
        return query

    @staticmethod
    def plan(store: _Store, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        store.client._execute("SET enable_seqscan = off")
        (document,) = store.client._query("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)[0]
        store.client._execute("RESET enable_seqscan")
        plan = document if isinstance(document, list) else json.loads(document)
        return plan[0]["Plan"]

    @staticmethod
    def touched(node: dict[str, Any]) -> int:
        total = 0
        if node.get("Relation Name") == "requests":
            loops = int(node.get("Actual Loops") or 1)
            removed = int(node.get("Rows Removed by Filter") or 0) + int(
                node.get("Rows Removed by Index Recheck") or 0
            )
            total += (int(node.get("Actual Rows") or 0) + removed) * loops
        return total + sum(CostTests.touched(child) for child in node.get("Plans") or ())

    @staticmethod
    def nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
        return [node] + [item for child in node.get("Plans") or () for item in CostTests.nodes(child)]

    def test_ten_times_the_unrelated_history_reads_the_same_rows(self) -> None:
        small, large = self.store(self.N), self.store(10 * self.N)
        counts = [self.touched(self.plan(store, *self.candidate_query(store))) for store in (small, large)]
        self.assertEqual(counts[0], counts[1], f"rows touched grew with unrelated history: {counts}")
        self.assertEqual(
            small.audit.uncharged_budget_candidates(limit=1000),
            large.audit.uncharged_budget_candidates(limit=1000),
        )

    def test_the_read_walks_the_candidate_index_and_scans_nothing(self) -> None:
        store = self.store(10 * self.N)
        nodes = self.nodes(self.plan(store, *self.candidate_query(store)))
        self.assertEqual([node for node in nodes if node.get("Node Type") == "Seq Scan"], [])
        self.assertIn("requests_budget_candidates", {node.get("Index Name") for node in nodes})

    def test_the_answers_are_the_same_before_the_index_exists(self) -> None:
        """AC7: the dispatcher runs this code before the PO's upgrade adds `0013`'s index."""
        store = self.store(self.N)
        indexed = store.audit.uncharged_budget_candidates(limit=1000)
        paged = store.audit.uncharged_budget_candidates(limit=3)
        store.client._execute("DROP INDEX requests_budget_candidates")
        store.client._commit_unless_nested()
        self.assertEqual(store.audit.uncharged_budget_candidates(limit=1000), indexed)
        self.assertEqual(store.audit.uncharged_budget_candidates(limit=3), paged)
        self.assertEqual(paged, indexed[:3])
        self.assertGreater(len(indexed), 10)


if __name__ == "__main__":
    unittest.main()
