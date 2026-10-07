"""The restore's card initialization against an in-process model of the PostgreSQL card store.

The recovery drill of ummanu-41 stopped on production's `personal_site-198`: a closed card in the
`personal_site` lane, on a project id the registry has since retired for `personal-site`. Every
write the restore sent was taken; what failed was the proof that reads them back. The card's export
carries `"model": ""`, and the store, which clears a key it is handed empty, reads that key back
absent.

`StoreModel` answers the calls `restore_cards_batched` makes the way `SqlCardClient` does: the
virtual lane table is the sorted set of lane names, a lane id is a position in it, and
`saveTaskMetadata` sorts each key into the same column, counter, link or bag treatment, from the
store's own key tables. It refuses what the store refuses on these calls. The real store is
`tests/test_snapshot_recovery_postgres.py`'s, in CI.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.fakes.card_restore import BOARD, COLUMNS, CURRENT, RETIRED, StoreModel
from ummanu.board.sql_cards import SqlCardError
from ummanu.restore import _ensure_restore_swimlanes, _normalized_cards, _restore_card_order
from ummanu.task_restore import close_restored_cards_batched, restore_cards_batched
from ummanu.tasks import TaskError


class _Audit:
    """The restore's staged obligations, held the way `SqlTaskAudit` holds them."""

    def __init__(self) -> None:
        self.staged: dict[str, dict[str, Any]] = {}

    def committed_event(self, request_id: str) -> None:
        return None

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.staged.get(request_id))

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        self.staged[request_id] = copy.deepcopy(event)

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        self.staged.pop(request_id, None)

    @staticmethod
    def require_claim(existing: dict[str, Any], **_claim: Any) -> None:
        return None


def _exported(cards: list[dict[str, Any]], registered: set[str]) -> list[dict[str, Any]]:
    """The cards as restore reads them from the materialized export, against `registered`."""
    with tempfile.TemporaryDirectory() as raw:
        board = Path(raw) / "board"
        board.mkdir()
        (board / "cards.json").write_text(json.dumps({"version": 1, "cards": cards}), encoding="utf-8")
        return _normalized_cards(Path(raw), registered_project_ids=registered)


def _restore(store: StoreModel, cards: list[dict[str, Any]]) -> SimpleNamespace:
    writer = SimpleNamespace(client=store, audit=_Audit())
    ordered = sorted(cards, key=_restore_card_order)
    swimlanes = {lane["id"]: lane["name"] for lane in store.call("getActiveSwimlanes", project_id=BOARD)}
    columns, swimlanes = _ensure_restore_swimlanes(store, BOARD, COLUMNS, swimlanes, ordered)
    restore_cards_batched(
        writer,
        ordered,
        board_id=BOARD,
        columns=columns,
        swimlanes=swimlanes,
        existing={},
        request_prefix="restore:test:",
    )
    live = {row["reference"]: row for row in store.call("getAllTasks", project_id=BOARD, status_id=1)}
    close_restored_cards_batched(store, ordered, live, board_id=BOARD)
    return writer


def _placed(store: StoreModel, reference: str) -> dict[str, Any]:
    """Where the store holds a card, in the export's terms."""
    key, row = next((key, row) for key, row in store.rows.items() if row["reference"] == reference)
    return {
        "column": COLUMNS[row["column_id"]],
        "swimlane": row["lane"],
        "position": row["position"],
        "closed": row["archived"],
        "metadata": store.call("getTaskMetadata", task_id=key),
    }


class RetiredProjectCardTests(unittest.TestCase):
    def test_a_closed_card_on_a_retired_project_id_restores_as_exported(self) -> None:
        cards = _exported([copy.deepcopy(RETIRED), copy.deepcopy(CURRENT)], registered={"personal-site"})
        store = StoreModel()

        writer = _restore(store, cards)

        restored = _placed(store, "personal_site-198")
        # The placement is the export's: its own lane, not the current id's look-alike one.
        self.assertEqual(
            {key: restored[key] for key in ("column", "swimlane", "position", "closed")},
            {"column": "Done", "swimlane": "personal_site", "position": 1, "closed": True},
        )
        self.assertEqual(_placed(store, "personal-site-1")["swimlane"], "personal-site")
        self.assertEqual(sorted(store._lane_names()), ["personal-site", "personal_site"])
        # Every value the export carries is on the row; the empty `model` reads back as no value.
        exported = {key: value for key, value in RETIRED["metadata"].items() if value}
        self.assertEqual({k: v for k, v in restored["metadata"].items() if k in exported}, exported)
        self.assertNotIn("model", restored["metadata"])
        self.assertEqual(restored["metadata"]["project"], "personal_site")
        # Both obligations were proved initialized.
        revisions = {event["ref"]: event["backend"]["revision"] for event in writer.audit.staged.values()}
        self.assertEqual(revisions, {"personal_site-198": "initialized", "personal-site-1": "initialized"})

    def test_a_value_the_store_did_not_keep_is_still_incomplete(self) -> None:
        """Empty and absent are one value; a non-empty value the row lacks is not."""
        cards = _exported([copy.deepcopy(RETIRED)], registered={"personal-site"})
        store = StoreModel()
        save = store._rpc_saveTaskMetadata

        def losing_claim(*, task_id: int, values: dict[str, Any]) -> bool:
            return save(task_id=task_id, values={k: v for k, v in values.items() if k != "claim"})

        store._rpc_saveTaskMetadata = losing_claim  # type: ignore[method-assign]

        with self.assertRaises(TaskError) as raised:
            _restore(store, cards)

        self.assertEqual(
            raised.exception.message,
            "board parity check failed: restored-card initialization is incomplete for personal_site-198",
        )

    def test_the_model_refuses_what_the_store_refuses(self) -> None:
        store = StoreModel()
        key = store.call("createTask", project_id=BOARD, title="t", column_id=1, reference="ummanu-1")
        with self.assertRaises(SqlCardError) as raised:
            store.call("saveTaskMetadata", task_id=key, values={"record_type": "issue"})
        self.assertEqual(
            raised.exception.message, "card ummanu-1 is a task; it cannot carry record_type 'issue'"
        )
        with self.assertRaises(SqlCardError):
            store.call("createTask", project_id=BOARD, title="t", column_id=1)


if __name__ == "__main__":
    unittest.main()
