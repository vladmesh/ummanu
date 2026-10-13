"""Bring existing Product and Issue rows into the lane of their product.

The placement rule itself lives in :func:`ummanu.product_issues.product_swimlane_id` and applies
to every write.  Rows created before it, and rows a restore brought back from a checkpoint that
recorded their old lane, can still sit somewhere else, so the repair is a supported command rather
than a one-off script: it reads the board, reports what is out of place and moves only that, which
makes a second run on a board already in order a no-op.

Nothing here decides which lane a record belongs in.  The destination name comes from
``product_lane_name`` and the destination lane from ``product_swimlane_id`` - the same rule, the
same lane, as the writers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ummanu.kernel.errors import TaskError
from ummanu.product_issues import (
    ISSUE_TYPE,
    META_ISSUE_PRODUCT,
    META_PRODUCT_ID,
    META_RECORD_TYPE,
    PRODUCT_TYPE,
    product_lane_name,
    product_swimlane_id,
)
from ummanu.tasks import _nonnegative_int, _positive_int, all_project_cards

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient


def _active_lanes(client: SqlCardClient, board_id: int) -> dict[int, str]:
    lanes = client.call("getActiveSwimlanes", project_id=board_id) or []
    if not isinstance(lanes, list):
        raise TaskError("backend_error", "board store returned invalid swimlanes", 1)
    result: dict[int, str] = {}
    for lane in lanes:
        identifier = _positive_int(lane.get("id")) if isinstance(lane, dict) else None
        if identifier is not None:
            result[identifier] = str(lane.get("name") or "")
    return result


def _record_product(kind: str, metadata: dict[str, str]) -> str:
    """What the record itself says it belongs to, with no guess made for it.

    A Product answers with its own id, an Issue with `issue_product`.  Neither the title, the
    lane the row happens to sit in nor a project binding takes part, so a record that says
    nothing stays unresolved instead of being moved somewhere plausible.
    """
    raw = metadata.get(META_PRODUCT_ID if kind == PRODUCT_TYPE else META_ISSUE_PRODUCT, "")
    return str(raw).removeprefix("product:").strip()


def _plan(store: Any, board_id: int) -> dict[str, Any]:
    lanes = _active_lanes(store.client, board_id)
    cards = all_project_cards(store.client, board_id)
    records: list[tuple[dict[str, Any], str, str, bool]] = []
    registered: set[str] = set()
    for card in cards:
        metadata = store._metadata(card)
        kind = metadata.get(META_RECORD_TYPE)
        if kind not in {PRODUCT_TYPE, ISSUE_TYPE}:
            continue
        product = _record_product(kind, metadata)
        closed = int(card.get("is_active", 1) or 0) == 0
        if kind == PRODUCT_TYPE and product:
            registered.add(product)
        records.append((card, kind, product, closed))

    # Rows of the same (column, lane) are already numbered by the board; keeping that order as the
    # order of the moves is what makes the records that travel together arrive in the same order.
    records.sort(
        key=lambda item: (
            _nonnegative_int(item[0].get("swimlane_id")),
            _nonnegative_int(item[0].get("position")),
            _nonnegative_int(item[0].get("id")),
        )
    )

    # How many rows each (column, lane) holds - every card of the board, not only the typed
    # records, because a move is placed among all the siblings the board has there.
    occupancy: dict[tuple[int, int], int] = {}
    for card in cards:
        key = (_nonnegative_int(card.get("column_id")), _nonnegative_int(card.get("swimlane_id")))
        occupancy[key] = occupancy.get(key, 0) + 1

    moves: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    summary: dict[str, dict[str, Any]] = {}
    closed_count = 0
    in_place = 0
    for card, kind, product, closed in records:
        reference = str(card.get("reference") or "")
        if closed:
            # Counted before anything else is asked about the row.  Whether its product can be
            # resolved decides who has to look at it, not whether the board holds a closed typed
            # record, so a row that is both is counted here and listed as unresolved below.
            closed_count += 1
        if not product or product not in registered:
            unresolved.append(
                {
                    "ref": reference,
                    "record_type": kind,
                    "product": product,
                    "reason": "product is not stated on the record"
                    if not product
                    else "product is not a registered Product",
                }
            )
            continue
        lane_name = product_lane_name(product)
        entry = summary.setdefault(
            product,
            {"product": product, "lane": lane_name, "move": 0, "in_place": 0, "closed": 0},
        )
        if closed:
            entry["closed"] += 1
            continue
        current_id = _nonnegative_int(card.get("swimlane_id"))
        if lanes.get(current_id) == lane_name:
            in_place += 1
            entry["in_place"] += 1
            continue
        moves.append(
            {
                "ref": reference,
                "record_type": kind,
                "product": product,
                "task_id": _nonnegative_int(card.get("id")),
                "column_id": _nonnegative_int(card.get("column_id")),
                "from": {"swimlane_id": current_id, "swimlane": lanes.get(current_id, "")},
                "to": {"swimlane": lane_name, "swimlane_id": _lane_id(lanes, lane_name)},
            }
        )
        entry["move"] += 1
    missing = sorted({str(move["to"]["swimlane"]) for move in moves if move["to"]["swimlane_id"] is None})
    return {
        "moves": moves,
        "unresolved": unresolved,
        "occupancy": occupancy,
        "summary": [summary[key] for key in sorted(summary)],
        "totals": {
            "records": len(records),
            "move": len(moves),
            "in_place": in_place,
            "closed": closed_count,
            "unresolved": len(unresolved),
        },
        "lanes_to_create": missing,
    }


def _lane_id(lanes: dict[int, str], name: str) -> int | None:
    for identifier, lane_name in lanes.items():
        if lane_name == name:
            return identifier
    return None


def reconcile_product_lanes(store: Any, *, apply: bool = False) -> dict[str, Any]:
    """Report - and with ``apply``, perform - the moves that put every row in its product lane.

    Only the lane changes: the row keeps its reference, metadata, comments, column and open or
    closed state, because a move is a single ``moveTaskPosition`` into the same column of another
    lane and nothing else is written.  Closed rows are counted and left alone, and a row whose
    product is unstated or unregistered is listed rather than guessed at.
    """
    board_id, _ = store._board()
    plan = _plan(store, board_id)
    occupancy = plan.pop("occupancy")
    result = {
        "mode": "apply" if apply else "plan",
        "moves": plan["moves"],
        "unresolved": plan["unresolved"],
        "summary": plan["summary"],
        "totals": plan["totals"],
        "lanes_to_create": plan["lanes_to_create"],
        "moved": 0,
    }
    if not apply:
        return result
    resolved: dict[str, int] = {}
    for move in plan["moves"]:
        product = str(move["product"])
        if product not in resolved:
            resolved[product] = product_swimlane_id(store.client, board_id, product)
        swimlane_id = resolved[product]
        column_id = int(move["column_id"])
        # Each row is appended after whatever the destination lane already holds, in plan order,
        # so the records that travel together keep their order and the rows already in place keep
        # their positions.
        position = occupancy.get((column_id, swimlane_id), 0) + 1
        if not store.client.call(
            "moveTaskPosition",
            project_id=board_id,
            task_id=int(move["task_id"]),
            column_id=column_id,
            position=position,
            swimlane_id=swimlane_id,
        ):
            raise TaskError("backend_error", "board store rejected the Product/Issue lane move", 1)
        occupancy[(column_id, swimlane_id)] = position
        source = (column_id, int(move["from"]["swimlane_id"]))
        occupancy[source] = max(0, occupancy.get(source, 0) - 1)
        move["to"]["swimlane_id"] = swimlane_id
        result["moved"] += 1
    return result
