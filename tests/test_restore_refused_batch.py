"""A refused restore batch surfaces what the store said (issue:454e56ce7c1b57beb78b).

The ummanu-45 drill's board import failed as "batched comment write is uncertain; pending
occurrences were reconciled" while the store's log said why: `issue_comments` violated
`issue_comment_claims_its_request`. PostgreSQL aborts a transaction at its first refused statement,
so the proving read the ambiguity path makes inside the import's one transaction answers only
"current transaction is aborted".

`PoisoningStore` behaves that way: inside a transaction (`_depth`), the call it is told to refuse
raises the store's refusal (`sql_cards._driver_error`'s `backend_error`), and every call after it
raises the aborted-transaction refusal. A transport loss (`backend_unavailable`) poisons nothing, and
it still takes the ambiguity path, because there the outcome is unknown.
"""

from __future__ import annotations

import contextlib
import copy
import unittest
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

from ummanu.board import sql_cards
from ummanu.restore import _ensure_restore_swimlanes
from ummanu.task_restore import (
    RestoreCommentOccurrence,
    close_restored_cards_batched,
    restore_cards_batched,
    restore_comments_batched,
)
from ummanu.tasks import TaskError

BOARD = sql_cards.BOARD_ID
COLUMNS = dict(sql_cards.BOARD_COLUMNS)
REFUSAL = (
    'the board store refused to apply a write: insert or update on table "issue_comments" violates '
    'foreign key constraint "issue_comment_claims_its_request"'
)
ABORTED = (
    "the board store refused to answer a read: current transaction is aborted, "
    "commands ignored until end of transaction block"
)
LOST = "the board store is unreachable while it must apply a write: server closed the connection"


class PoisoningStore:
    """A card store holding one card and its comments, refusing one method the way PostgreSQL does."""

    def __init__(self, *, refuse: str, code: str = "backend_error", depth: int = 1) -> None:
        self._depth = depth
        self.refuse, self.code = refuse, code
        self.aborted = False
        self.calls: list[str] = []
        self.comments: list[str] = []
        self.metadata: dict[str, str] = {}
        self.closed = False

    def call(self, method: str, **params: Any) -> Any:
        self.calls.append(method)
        if self.aborted:
            raise TaskError("backend_error", ABORTED, 1)
        if method == self.refuse:
            if self.code == "backend_unavailable":
                raise TaskError("backend_unavailable", LOST, 1)
            # A refused statement aborts the enclosing transaction; outside one it is only refused.
            self.aborted = bool(self._depth)
            raise TaskError("backend_error", REFUSAL, 1)
        return getattr(self, f"_rpc_{method}")(**params)

    def call_batch(self, calls: Any) -> list[Any]:
        return [self.call(method, **dict(arguments)) for method, arguments in calls]

    def _rpc_getAllComments(self, *, task_id: int) -> list[dict[str, Any]]:
        return [{"id": index, "comment": body} for index, body in enumerate(self.comments, 1)]

    def _rpc_createComment(self, *, task_id: int, content: str, user_id: int = 0, **_: Any) -> int:
        self.comments.append(content)
        return len(self.comments)

    def _rpc_getAllTasks(self, *, project_id: int, status_id: int) -> list[dict[str, Any]]:
        row = {
            "id": 1,
            "reference": "issue:0123456789abcdef0123",
            "title": "Issue",
            "description": "",
            "column_id": 1,
            "position": 1,
            "swimlane_id": 0,
            "is_active": 0 if self.closed else 1,
        }
        return [row] if (status_id == 1) != self.closed else []

    def _rpc_getActiveSwimlanes(self, *, project_id: int) -> list[dict[str, Any]]:
        return []

    def _rpc_getTaskMetadata(self, *, task_id: int) -> dict[str, str]:
        return dict(self.metadata)


class _Audit:
    """The restore's staged and committed records, as `SqlTaskAudit` keeps them."""

    def __init__(self) -> None:
        self.staged: dict[str, dict[str, Any]] = {}
        self.committed: dict[str, dict[str, Any]] = {}

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.committed.get(request_id))

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.staged.get(request_id))

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        self.staged[request_id] = copy.deepcopy(event)

    def append(self, request_id: str, event: dict[str, Any]) -> str:
        self.staged.pop(request_id, None)
        self.committed[request_id] = copy.deepcopy(event)
        return str(event["event_id"])

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        self.staged.pop(request_id, None)

    @staticmethod
    def require_claim(existing: dict[str, Any], **_claim: Any) -> None:
        return None

    @contextlib.contextmanager
    def marker_comment_lock(self, reference: str) -> Iterator[None]:
        yield

    @staticmethod
    def pending_marker_owners(_items: Any) -> dict[str, str]:
        return {}


ISSUE = "issue:0123456789abcdef0123"
BODY = "[issue:priority]\nraised for recovery\n[request-id:raise-priority]"


def _comment(store: PoisoningStore) -> SimpleNamespace:
    writer = SimpleNamespace(client=store, audit=_Audit())
    restore_comments = [RestoreCommentOccurrence(ISSUE, 1, BODY, 0, "restore:t:comment:" + ISSUE + ":0")]
    with_error = None
    try:
        restore_comments_batched(writer, restore_comments)
    except TaskError as exc:
        with_error = exc
    return SimpleNamespace(writer=writer, error=with_error)


class RefusedCommentBatchTests(unittest.TestCase):
    def test_a_refusal_inside_the_import_transaction_carries_the_store_message(self) -> None:
        store = PoisoningStore(refuse="createComment")
        outcome = _comment(store)
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.error.code, "backend_error")
        self.assertIn('violates foreign key constraint "issue_comment_claims_its_request"', outcome.error.message)
        self.assertNotIn("uncertain", outcome.error.message)
        self.assertNotIn("aborted", outcome.error.message)
        # Nothing was read inside the aborted transaction after the refusal.
        self.assertEqual(store.calls[-1], "createComment")

    def test_transport_loss_still_takes_the_ambiguity_path(self) -> None:
        store = PoisoningStore(refuse="createComment", code="backend_unavailable")
        outcome = _comment(store)
        self.assertEqual(outcome.error.code, "audit_pending")
        self.assertEqual(
            outcome.error.message, "batched comment write is uncertain; pending occurrences were reconciled"
        )
        # The evidence read ran, and the occurrence stays staged for the retry.
        self.assertEqual(store.calls[-1], "getAllComments")
        self.assertIn("restore:t:comment:" + ISSUE + ":0", outcome.writer.audit.staged)

    def test_a_refusal_outside_a_transaction_is_reconciled_as_before(self) -> None:
        store = PoisoningStore(refuse="createComment", depth=0)
        outcome = _comment(store)
        self.assertEqual(outcome.error.code, "audit_pending")
        self.assertEqual(store.calls[-1], "getAllComments")


def _card(reference: str, *, closed: bool = False) -> dict[str, Any]:
    return {
        "reference": reference,
        "title": "Issue",
        "description": "",
        "column": COLUMNS[1],
        "swimlane": "",
        "position": 1,
        "closed": closed,
        "metadata": {"record_type": "issue", "issue_product": "ummanu"},
        "fields": {},
        "comments": [],
    }


class RefusedCardBatchTests(unittest.TestCase):
    def test_existing_ownership_metadata_conflict_refuses_before_initialization(self) -> None:
        store = PoisoningStore(refuse="saveTaskMetadata")
        card = _card(ISSUE)
        card["metadata"]["issue_priority"] = "P1"
        store.metadata = {**card["metadata"], "issue_priority": "P2"}
        existing = {ISSUE: store.call("getAllTasks", project_id=BOARD, status_id=1)[0]}
        writer = SimpleNamespace(client=store, audit=_Audit())
        with self.assertRaisesRegex(TaskError, "different metadata"):
            restore_cards_batched(
                writer, [card], board_id=BOARD, columns=COLUMNS, swimlanes={},
                existing=existing, request_prefix="restore:t:",
            )
        self.assertEqual(store.metadata["issue_priority"], "P2")
        self.assertEqual(store.calls[-1], "getTaskMetadata")

    def _create(self, store: PoisoningStore) -> TaskError:
        writer = SimpleNamespace(client=store, audit=_Audit())
        with self.assertRaises(TaskError) as raised:
            restore_cards_batched(
                writer,
                [_card("issue:fedcba9876543210fedc")],
                board_id=BOARD,
                columns=COLUMNS,
                swimlanes={},
                existing={},
                request_prefix="restore:t:",
            )
        return raised.exception

    def test_a_refused_create_carries_the_store_message(self) -> None:
        store = PoisoningStore(refuse="createTask")
        error = self._create(store)
        self.assertEqual(error.code, "backend_error")
        self.assertIn("card create batch", error.message)
        self.assertIn("issue_comment_claims_its_request", error.message)
        self.assertEqual(store.calls[-1], "createTask")

    def test_a_lost_create_is_still_uncertain(self) -> None:
        error = self._create(PoisoningStore(refuse="createTask", code="backend_unavailable"))
        self.assertEqual(error.code, "audit_pending")
        self.assertIn("uncertain", error.message)

    def test_a_refused_closure_carries_the_store_message(self) -> None:
        store = PoisoningStore(refuse="closeTask")
        live = {ISSUE: {"id": "1", "reference": ISSUE}}
        with self.assertRaises(TaskError) as raised:
            close_restored_cards_batched(store, [_card(ISSUE, closed=True)], live, board_id=BOARD)
        self.assertIn("card closure batch", raised.exception.message)
        self.assertIn("issue_comment_claims_its_request", raised.exception.message)
        self.assertEqual(store.calls[-1], "closeTask")

    def test_a_lost_closure_is_still_uncertain(self) -> None:
        store = PoisoningStore(refuse="closeTask", code="backend_unavailable")
        live = {ISSUE: {"id": "1", "reference": ISSUE}}
        with self.assertRaises(TaskError) as raised:
            close_restored_cards_batched(store, [_card(ISSUE, closed=True)], live, board_id=BOARD)
        self.assertEqual(raised.exception.message, "restored-card closure is uncertain; retry is required")

    def test_a_refused_swimlane_batch_carries_the_store_message(self) -> None:
        store = PoisoningStore(refuse="addSwimlane")
        card = {**_card("ummanu-1"), "swimlane": "ummanu"}
        with self.assertRaises(TaskError) as raised:
            _ensure_restore_swimlanes(store, BOARD, COLUMNS, {}, [card])
        self.assertIn("swimlane batch", raised.exception.message)
        self.assertEqual(store.calls[-1], "addSwimlane")


if __name__ == "__main__":
    unittest.main()
