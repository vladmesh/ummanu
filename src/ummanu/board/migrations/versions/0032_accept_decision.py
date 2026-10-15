"""The observer's Assessment decision value `release` is stored as `accept`.

Every persisted spelling of the decision is rewritten in place: the `decision:<value>` marker of
`task_comments`, and inside `board_events.data` and `requests.intent` every object key `decision` or
`disposition` holding the value and every `marker` holding `decision:<value>`, at any depth. The
downgrade writes the old value back the same way.
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0032_accept_decision"
down_revision = "0031_sprint_dod_items"
branch_labels = None
depends_on = None
# The released code refuses the new value, so it cannot keep reading a migrated store.
release_safety = "destructive"

OLD = "release"
NEW = "accept"
_VALUE_KEYS = ("decision", "disposition")
#: Each JSONB document column with the key that names its row.
_DOCUMENTS = (("board_events", "data", "event_id"), ("requests", "intent", "request_id"))


def _renamed(value: Any, old: str, new: str) -> Any:
    if isinstance(value, list):
        return [_renamed(item, old, new) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in _VALUE_KEYS and item == old:
            out[key] = new
        elif key == "marker" and item == f"decision:{old}":
            out[key] = f"decision:{new}"
        else:
            out[key] = _renamed(item, old, new)
    return out


def _rewrite(old: str, new: str) -> None:
    connection = op.get_bind()
    connection.exec_driver_sql(
        "UPDATE task_comments SET marker = %(new)s WHERE marker = %(old)s",
        {"old": f"decision:{old}", "new": f"decision:{new}"},
    )
    for table, column, key in _DOCUMENTS:
        rows = connection.exec_driver_sql(
            f"SELECT {key}, {column} FROM {table} WHERE {column}::text LIKE %(value)s "
            f"OR {column}::text LIKE %(marker)s",
            {"value": f'%"{old}"%', "marker": f'%"decision:{old}"%'},
        ).fetchall()
        for identity, document in rows:
            changed = _renamed(document, old, new)
            if changed != document:
                connection.exec_driver_sql(
                    f"UPDATE {table} SET {column} = %(document)s::jsonb WHERE {key} = %(identity)s",
                    {"document": json.dumps(changed), "identity": identity},
                )


def upgrade() -> None:
    _rewrite(OLD, NEW)


def downgrade() -> None:
    _rewrite(NEW, OLD)
