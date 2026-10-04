"""Delegation, waits and what a sprint waits for, read from a real card store (secretary-1811).

The three reads DoD 11 of sprint:1469 renders: a card's `origin`, `wait` and `e2e` blocks in its task
snapshot; the cards a PO session delegated, from one board listing; and `work.waiting_on` of
`ummanu sprint status`, derived from the sprint's live cards and nothing else. The board is a
throwaway card store (`tests/sql_backend_fixtures.py`); the metadata is written as the writers write
it, into the extension bag.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests.fakes.dispatcher import dispatcher_seed
from tests.sql_backend_fixtures import card_store
from tests.webproto_sprint_fixtures import SprintProtocolFixture
from ummanu.board.owner_handover import WAITING_OWNER, WAITING_OWNER_BY, WAITING_OWNER_REASON
from ummanu.board.po_origin import PO_ORIGIN, PO_RETURN, origin_text
from ummanu.board.wait_card import WAIT_SPEC, WAIT_STATE, build_wait_spec
from ummanu.cli import main
from ummanu.config import validate
from ummanu.webproto.reads import ReadLayer

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
REPO = "vladmesh/ummanu"
RUN_URL = f"https://github.com/{REPO}/actions/runs/4242"
SESSION = "3f0a6c2e-origin-session"
SUCCESSOR = "9b7d1e44-successor-session"
OTHER = "5c5c5c5c-other-session"


def _instance(root: Path, data_dir: Path) -> Path:
    instance_dir = root / "instance"
    (instance_dir / "projects").mkdir(parents=True)
    (instance_dir / "instance.yaml").write_text(
        "version: 1\nname: test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
        encoding="utf-8",
    )
    (instance_dir / "projects" / "ummanu.yaml").write_text(
        "id: ummanu\nrepo: /projects/ummanu\nenabled: true\nadapter: ummanu\ndefault_branch: main\n",
        encoding="utf-8",
    )
    return instance_dir


def _wait_metadata(*, result: bool = False, sprint: str = "sprint:1") -> dict[str, str]:
    spec = build_wait_spec(
        run=REPO,
        run_id="4242",
        deadline="2h",
        returns=("observer", f"po-session:{SESSION}"),
        sprint=sprint,
        now=T0,
    )
    state = {"since": "2026-09-27T12:00:00Z"}
    if result:
        state["result"] = {
            "outcome": "target_reached",
            "summary": "the run concluded success",
            "evidence": RUN_URL,
            "fact": "success",
            "frozen_at": "2026-09-27T12:30:00Z",
            "key": "k-1",
        }
    return {"task_type": "wait", WAIT_SPEC: spec.text(), WAIT_STATE: json.dumps(state)}


def _e2e_metadata(*, state: str = "waiting", budget_wait: str = "") -> dict[str, str]:
    run = {
        "dispatch_id": "ummanu-520-e2e-1-00000001",
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "repo": REPO,
        "branch": "pipeline/ummanu-520",
        "workflow": "e2e.yml",
        "intent_at": "2026-09-27T12:00:00Z",
        "dispatch": "sent",
        "run_id": 4242,
        "run_url": RUN_URL,
        "head_sha": "0123456789abcdef0123456789abcdef01234567" if state != "identifying" else "",
        "wait_ref": "ummanu-530",
    }
    document: dict = {"runs": [run]}
    if budget_wait:
        document["budget_wait"] = {
            "decision": budget_wait,
            "generation": 3,
            "scope": "card",
            "since": "2026-09-27T13:00:00Z",
        }
    return {"task_type": "code", "e2e": json.dumps(document)}


AM_RUN_URL = f"https://github.com/{REPO}/actions/runs/5150"
AM_DISPATCH = "ummanu-551-e2e-am-1-00000001"


def _after_merge_run(**changes: object) -> dict:
    """The carrier's record of a coalesced after-merge run over secretary-550 and secretary-551."""
    run = {
        "dispatch_id": AM_DISPATCH,
        "sha": "c0ffee0000000000000000000000000000000000",
        "repo": REPO,
        "branch": "e2e/after-merge/ummanu-551",
        "workflow": "e2e.yml",
        "intent_at": "2026-09-28T10:00:00Z",
        "dispatch": "sent",
        "run_id": 5150,
        "run_url": AM_RUN_URL,
        "head_sha": "c0ffee0000000000000000000000000000000000",
        "wait_ref": "ummanu-559",
        "placement": "after_merge",
        "covered": [
            {"ref": "ummanu-550", "merge_sha": "a" * 40},
            {"ref": "ummanu-551", "merge_sha": "b" * 40},
        ],
    }
    if changes.get("resolution"):
        run["result"] = {"outcome": "target_reached", "conclusion": "success", "summary": "green", "key": "k"}
        run["acted"] = True
    run.update(changes)
    return run


def _handed() -> dict[str, str]:
    return {
        WAITING_OWNER: "2026-09-27T12:00:00+00:00",
        WAITING_OWNER_REASON: "The owner holds the payment card.",
        WAITING_OWNER_BY: "po",
    }


class DelegationReadFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data_dir = self.tmp / "data"
        (self.data_dir / "dispatcher").mkdir(parents=True)
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps({"phase": "production", "records": {}, "observers": {}}), encoding="utf-8"
        )
        self.instance = _instance(self.tmp, self.data_dir)
        self.board = card_store(self, dispatcher_seed(), instance_dir=self.data_dir)

    def layer(self) -> ReadLayer:
        return ReadLayer(
            self.instance, board_client=self.board, status_reader=lambda: {}, clock=lambda: 1788652800.0
        )

    def delegated(
        self, key: int, ref: str, *, session: str = SESSION, successor: str = "", **metadata
    ) -> None:
        values = {
            "project": "ummanu",
            "task_type": "decision",
            PO_ORIGIN: origin_text(session, "req-po-1"),
        }
        if successor:
            values[PO_RETURN] = json.dumps(
                {
                    "executor": "",
                    "successors": {session: {"replaces": session, "via": "closed", "session": successor}},
                }
            )
        values.update(metadata)
        self.board.add_card(key, ref, state="in_progress", title=f"card {ref}", metadata=values)

    def returned(self, ref: str, event: str, state: str, *, session: str | None, status: str | None) -> None:
        with self.board.transaction():
            self.board._execute(
                "INSERT INTO origin_returns (task_ref, event_id, request_id, target_state, created_at, "
                "delivered_at, status, notice, session, po_request_id) "
                "VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s, %s)",
                (ref, event, f"req-{event}", state, T0 if status else None, status, "", session, "req-po-1"),
            )


class TaskSnapshotBlockTests(DelegationReadFixture):
    def test_the_snapshot_carries_origin_with_returns_wait_and_e2e(self) -> None:
        self.delegated(20, "ummanu-520", successor=SUCCESSOR, **_e2e_metadata())
        self.returned("ummanu-520", "evt-1", "blocked", session=SESSION, status="delivered")
        self.board.add_card(
            21, "ummanu-530", state="in_progress", metadata={"project": "ummanu", **_wait_metadata()}
        )

        card = self.layer().task_snapshot("ummanu-520")["card"]["value"]
        self.assertEqual(card["origin"]["po_session"], SESSION)
        self.assertEqual(card["origin"]["current_session"], SUCCESSOR)
        self.assertEqual(
            [(row["state"], row["status"], row["session"]) for row in card["origin"]["returns"]],
            [("blocked", "delivered", SESSION)],
        )
        self.assertEqual(card["e2e"]["runs"][0]["run"], RUN_URL)
        self.assertEqual(card["e2e"]["runs"][0]["wait_card"], "ummanu-530")
        self.assertIsNone(card["wait"])

        waiting = self.layer().task_snapshot("ummanu-530")["card"]["value"]
        self.assertEqual(waiting["wait"]["state"], "waiting")
        self.assertEqual(waiting["wait"]["target"]["link"], RUN_URL)
        self.assertIsNone(waiting["origin"])
        self.assertIsNone(waiting["e2e"])

    def test_a_card_with_none_of_the_blocks_carries_nulls(self) -> None:
        card = self.layer().task_snapshot("ummanu-510")["card"]["value"]
        self.assertEqual((card["origin"], card["wait"], card["e2e"]), (None, None, None))


class PoDelegatedTests(DelegationReadFixture):
    def test_the_session_lists_what_it_delegated_and_inherited_from_one_board_listing(self) -> None:
        self.delegated(20, "ummanu-520")
        self.returned("ummanu-520", "evt-1", "blocked", session=SESSION, status="delivered")
        self.returned("ummanu-520", "evt-2", "done", session=None, status=None)
        self.delegated(21, "ummanu-521", session=OTHER, successor=SESSION)
        self.delegated(22, "ummanu-522", session=OTHER)
        self.board.calls.clear()
        self.board.batch_calls.clear()

        document = self.layer().po_delegated(SESSION)

        self.assertEqual(document["source"]["state"], "available")
        self.assertEqual(
            [(item["ref"], item["relation"], item["type"], item["state"]) for item in document["items"]],
            [
                ("ummanu-520", "delegated", "decision", "in_progress"),
                ("ummanu-521", "inherited", "decision", "in_progress"),
            ],
        )
        last = document["items"][0]["last_return"]
        self.assertEqual((last["state"], last["status"], last["session"]), ("done", None, None))
        self.assertIsNone(document["items"][1]["last_return"])
        # One listing: one `getAllTasks`, the metadata in one batch, and no per-card read.
        methods = [method for method, _params in self.board.calls]
        self.assertEqual(methods.count("getAllTasks"), 1, methods)
        self.assertEqual(len(self.board.batch_calls), 1)
        self.assertFalse(
            {"getTask", "getTaskByReference", "getAllComments", "getTaskMetadata"} & set(methods), methods
        )

    def test_a_session_that_delegated_nothing_is_an_empty_list(self) -> None:
        self.delegated(20, "ummanu-520", session=OTHER)
        self.assertEqual(self.layer().po_delegated(SESSION)["items"], [])
        self.assertEqual(self.layer().po_delegated("")["items"], [])

    def test_a_board_that_does_not_answer_is_null_items_and_a_reason(self) -> None:
        class Broken:
            def call(self, *_args, **_kwargs):
                raise OSError("the board store went away")

        layer = ReadLayer(self.instance, board_client=Broken(), status_reader=lambda: {}, clock=lambda: 1.0)
        document = layer.po_delegated(SESSION)
        self.assertIsNone(document["items"])
        self.assertEqual(document["source"]["state"], "unavailable")
        self.assertIn("went away", document["source"]["reason"])


class SprintStatusWaitingOnTests(SprintProtocolFixture):
    """`ummanu sprint status` prints `work.waiting_on`: every kind, from the sprint's cards alone."""

    def _run(self, reference: str) -> dict:
        output, errors = io.StringIO(), io.StringIO()
        with self.board_injected(), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(
                [
                    "sprint",
                    "status",
                    "--ref",
                    reference,
                    "--instance",
                    str(self.instance),
                    "--data-dir",
                    str(self.data_dir),
                ]
            )
        self.assertEqual(code, 0, errors.getvalue())
        return json.loads(output.getvalue())

    def _card(self, key: int, ref: str, sprint: str, state: str, **metadata: str) -> None:
        self.board.add_card(
            key, ref, state=state, metadata={"project": "ummanu", "sprint_ref": sprint, **metadata}
        )

    def test_waiting_on_names_each_run_owner_and_po_card(self) -> None:
        reference = self.reference_of(self.create())
        self._card(40, "ummanu-540", reference, "in_progress", **_wait_metadata(sprint=reference))
        self._card(41, "ummanu-541", reference, "validate", **_e2e_metadata())
        self._card(42, "ummanu-542", reference, "in_progress", task_type="decision", **_handed())
        self._card(
            43,
            "ummanu-543",
            reference,
            "validate",
            **_e2e_metadata(state="waiting", budget_wait="ummanu-599"),
        )
        self._card(44, "ummanu-544", reference, "in_progress", task_type="operation")
        # Nothing to wait for: a finished wait, a code card in Ready, a decision already done.
        self._card(45, "ummanu-545", reference, "done", **_wait_metadata(result=True, sprint=reference))
        self._card(46, "ummanu-546", reference, "ready", task_type="code")
        self._card(47, "ummanu-547", reference, "done", task_type="decision", **_handed())
        # Another sprint's waiting card is not this sprint's.
        self._card(48, "ummanu-548", "", "in_progress", task_type="decision")

        document = self._run(reference)
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
        waiting_on = document["work"]["waiting_on"]

        found = {(entry["kind"], entry["card"]) for entry in waiting_on}
        self.assertEqual(
            found,
            {
                ("run", "ummanu-540"),
                ("run", "ummanu-541"),
                ("owner", "ummanu-542"),
                ("run", "ummanu-543"),
                ("dependency", "ummanu-543"),
                ("po", "ummanu-544"),
            },
        )
        for entry in waiting_on:
            expected = {"kind", "card", "detail"}
            if entry["kind"] == "dependency":
                expected.add("holder")
                self.assertEqual(entry["holder"], "ummanu-599")
            self.assertEqual(set(entry), expected)
            self.assertTrue(entry["detail"] and "\n" not in entry["detail"])
        detail = {(entry["kind"], entry["card"]): entry["detail"] for entry in waiting_on}
        self.assertIn(RUN_URL, detail[("run", "ummanu-540")])
        self.assertIn("deadline", detail[("run", "ummanu-540")])
        self.assertIn(RUN_URL, detail[("run", "ummanu-541")])
        self.assertIn("payment card", detail[("owner", "ummanu-542")])
        self.assertIn("ummanu-599", detail[("dependency", "ummanu-543")])
        self.assertIn("with the PO", detail[("po", "ummanu-544")])

    def _merged(self, key: int, ref: str, sprint: str, mark: dict, carried: list | None = None) -> None:
        document: dict = {"runs": [], "after_merge": mark}
        if carried:
            document["after_merge_runs"] = carried
        self._card(key, ref, sprint, "done", task_type="code", e2e=json.dumps(document))

    def test_a_coalesced_after_merge_run_is_waited_on_by_every_card_it_covers(self) -> None:
        """secretary-1811 rework: the carrier and the card it covers, once each, with the same run URL."""
        reference = self.reference_of(self.create())
        covered = {
            "merge_sha": "a" * 40,
            "state": "covered",
            "dispatch_id": AM_DISPATCH,
            "run_url": AM_RUN_URL,
            "carrier": "ummanu-551",
        }
        self._merged(50, "ummanu-550", reference, covered)
        self._merged(51, "ummanu-551", reference, {**covered, "merge_sha": "b" * 40}, [_after_merge_run()])
        self._merged(52, "ummanu-552", reference, {"merge_sha": "c" * 40, "state": "pending"})

        document = self._run(reference)
        self.assertEqual(validate(document, "web-sprint", document["kind"]), [])
        waiting_on = document["work"]["waiting_on"]
        self.assertEqual(
            sorted((entry["kind"], entry["card"]) for entry in waiting_on),
            [("run", "ummanu-550"), ("run", "ummanu-551"), ("run", "ummanu-552")],
        )
        detail = {entry["card"]: entry["detail"] for entry in waiting_on}
        self.assertIn(AM_RUN_URL, detail["ummanu-550"])
        self.assertIn(AM_RUN_URL, detail["ummanu-551"])
        self.assertEqual(detail["ummanu-552"], "queued for the next after-merge run")

    def test_after_a_green_after_merge_run_neither_card_waits(self) -> None:
        reference = self.reference_of(self.create())
        green = {
            "merge_sha": "a" * 40,
            "state": "green",
            "dispatch_id": AM_DISPATCH,
            "run_url": AM_RUN_URL,
            "carrier": "ummanu-551",
        }
        self._merged(50, "ummanu-550", reference, green)
        self._merged(
            51,
            "ummanu-551",
            reference,
            {**green, "merge_sha": "b" * 40},
            [_after_merge_run(resolution="green")],
        )
        self.assertEqual(self._run(reference)["work"]["waiting_on"], [])

    def test_a_sprint_waiting_on_nothing_prints_an_empty_list(self) -> None:
        reference = self.reference_of(self.create())
        self._card(46, "ummanu-546", reference, "ready", task_type="code")
        self.assertEqual(self._run(reference)["work"]["waiting_on"], [])

    def test_the_listing_carries_the_same_list_per_sprint(self) -> None:
        reference = self.reference_of(self.create())
        self._card(44, "ummanu-544", reference, "in_progress", task_type="operation")
        item = next(
            entry for entry in self.reads().sprint_list()["sprints"]["items"] if entry["ref"] == reference
        )
        self.assertEqual([entry["card"] for entry in item["waiting_on"]], ["ummanu-544"])


if __name__ == "__main__":
    unittest.main()
