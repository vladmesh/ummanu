"""In-process card store and exported records for hermetic restore checks."""

from __future__ import annotations

from typing import Any

from ummanu.board import sql_cards
from ummanu.board.sql_cards import SqlCardError

BOARD = sql_cards.BOARD_ID
COLUMNS = dict(sql_cards.BOARD_COLUMNS)

#: `cards/0001/00001509.json` of production's checkpoint, less its comments.
RETIRED = {
    "closed": True,
    "column": "Done",
    "comments": [],
    "date_moved": 1785832418,
    "description": "## Цель\n\nПривести docker-сборку сервисов в порядок.\n",
    "fields": {
        "base_branch": "",
        "blocked_by": "",
        "claim": "personal_site-198-1782988235",
        "effective_head": "claude-sonnet",
        "effective_review_head": "codex-terra-high",
        "head": "claude-sonnet",
        "project": "personal_site",
        "review_head": "",
        "slug": "",
        "task_type": "code",
    },
    "id": 360,
    "metadata": {
        "claim": "personal_site-198-1782988235",
        "complexity": "standard",
        "family_preference": "auto",
        "head": "claude-sonnet",
        "model": "",
        "project": "personal_site",
        "record_type": "task",
        "task_type": "code",
    },
    "position": 1,
    "reference": "personal_site-198",
    "swimlane": "personal_site",
    "title": "Docker-гигиена: .dockerignore + непривилегированная dev-стадия бэкенда",
}

#: A live card on the id that replaced it: its lane is the one `_matching_swimlane` would also
#: accept for `personal_site` if the exact name were not there.
CURRENT = {
    "column": "Ready",
    "comments": [],
    "description": "",
    "fields": {"project": "personal-site", "task_type": "code"},
    "metadata": {"project": "personal-site", "record_type": "task", "task_type": "code"},
    "position": 1,
    "reference": "personal-site-1",
    "swimlane": "personal-site",
    "title": "Current site card",
}


class StoreModel:
    """`SqlCardClient`'s answers to the restore's card calls, without PostgreSQL."""

    instance_dir = "."

    def __init__(self) -> None:
        self.lanes: list[str] = []
        self.rows: dict[int, dict[str, Any]] = {}
        self.next_key = 1

    # The client's two entry points: one call, and a batch that runs call by call.
    def call(self, method: str, **params: Any) -> Any:
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            raise SqlCardError(f"the board store does not serve {method}")
        return handler(**params)

    def call_batch(self, calls: Any) -> list[Any]:
        return [self.call(method, **dict(arguments)) for method, arguments in calls]

    # --- lanes: a sorted virtual table, an id is a position in it ----------------------------------
    def _lane_names(self) -> list[str]:
        named = {row["lane"] for row in self.rows.values() if row["lane"]}
        return sorted(named | set(self.lanes))

    def _lane_id(self, name: str | None) -> int:
        lanes = self._lane_names()
        return lanes.index(name) + 1 if name in lanes else 0

    def _lane_name(self, identifier: Any) -> str | None:
        lanes = self._lane_names()
        index = int(identifier or 0)
        return lanes[index - 1] if 1 <= index <= len(lanes) else None

    def _rpc_getActiveSwimlanes(self, *, project_id: int) -> list[dict[str, Any]]:
        return [{"id": index, "name": name} for index, name in enumerate(self._lane_names(), start=1)]

    def _rpc_addSwimlane(self, *, project_id: int, name: str) -> Any:
        if name in self._lane_names():
            return False
        self.lanes.append(name)
        return self._lane_id(name)

    # --- cards ---------------------------------------------------------------------------------
    def _row(self, task_id: Any) -> dict[str, Any]:
        row = self.rows.get(int(task_id))
        if row is None:
            raise SqlCardError(f"no card carries transport key {task_id}")
        return row

    def _rpc_createTask(
        self,
        *,
        project_id: int,
        title: str,
        description: str = "",
        column_id: int = 1,
        swimlane_id: int = 0,
        reference: str = "",
    ) -> int:
        if not reference:
            raise SqlCardError("the board store identifies a card by its reference (§9)")
        key, self.next_key = self.next_key, self.next_key + 1
        self.rows[key] = {
            "reference": reference,
            "title": title,
            "description": description or "",
            "column_id": int(column_id),
            "position": 1,
            "lane": self._lane_name(swimlane_id),
            "archived": False,
            "columns": {},
            "bag": {},
        }
        return key

    def _rpc_getAllTasks(self, *, project_id: int, status_id: int) -> list[dict[str, Any]]:
        return [
            {
                "id": key,
                "reference": row["reference"],
                "title": row["title"],
                "description": row["description"],
                "column_id": row["column_id"],
                "position": row["position"],
                "swimlane_id": self._lane_id(row["lane"]),
                "is_active": 0 if row["archived"] else 1,
            }
            for key, row in sorted(self.rows.items())
            if (status_id == 1) != row["archived"]
        ]

    def _rpc_moveTaskPosition(
        self, *, project_id: int, task_id: int, column_id: int, position: int, swimlane_id: int = 0
    ) -> bool:
        row = self._row(task_id)
        row["column_id"] = int(column_id)
        row["position"] = max(1, int(position))
        lane = self._lane_name(swimlane_id)
        if lane is not None:
            row["lane"] = lane
        return True

    def _rpc_closeTask(self, *, task_id: int) -> bool:
        self._row(task_id)["archived"] = True
        return True

    # --- metadata: `_rpc_saveTaskMetadata` and `_card_metadata`, key by key ------------------------
    def _rpc_saveTaskMetadata(self, *, task_id: int, values: dict[str, Any]) -> bool:
        row = self._row(task_id)
        declared = sql_cards._text(values.get("record_type"))
        if declared not in {"", "task"}:
            raise SqlCardError(f"card {row['reference']} is a task; it cannot carry record_type {declared!r}")
        columns, bag = row["columns"], row["bag"]
        for key, raw in values.items():
            text = sql_cards._text(raw)
            if (
                key in sql_cards._METADATA_COLUMNS
                or key in sql_cards._METADATA_LINKS
                or (key == sql_cards._METADATA_TIMESTAMP[0])
            ):
                # The column (or satellite rows) holds a value; an empty one is remembered in the bag.
                columns[key] = text
                if text:
                    bag.pop(key, None)
                else:
                    bag[key] = ""
            elif key == sql_cards._METADATA_FLAG[0]:
                columns[key] = "1" if sql_cards.live_impact_flag(text) else ""
                bag.pop(key, None)
            elif key in sql_cards._METADATA_COUNTERS:
                columns[key] = text if text.isdigit() and int(text) else ""
            elif text:
                bag[key] = text
            else:
                # An empty value for a key only the bag carries clears it.
                bag.pop(key, None)
        return True

    def _rpc_getTaskMetadata(self, *, task_id: int) -> dict[str, str]:
        row = self._row(task_id)
        meta = {key: value for key, value in row["columns"].items() if value}
        meta.update(row["bag"])
        meta["record_type"] = "task"
        return meta
