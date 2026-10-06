"""The Product and Issue half of the board vocabulary, answered from PostgreSQL.

`board/sql_cards.py` answers the same eleven-method vocabulary for a card over `tasks`.  This
module answers it for the two dictionary records that share the Pipeline board with cards —
`products` and `issues` (`docs/BOARD_STORE.md` §3.1, §3.2) — together with their satellites
`product_projects` (§3.1), `issue_comments` and `product_comments` (§3.7).  It is one object
because the routing is one decision: on this board every record is addressed by an integer, and
the integer says which of the three tables it lives in (`board/backend.py:record_key`).

Three things are decided here and each is a fact about the *schema*, not a preference:

* **`record_type` is the table.**  §8.1 says the three record types become three tables, so a
  Product is a row of `products` and an Issue a row of `issues`, and nothing carries a
  `record_type` column.  The client answers the key from whichever table it read.
* **A create is spread over three calls and a row is not.**  `SqlBoardHost.create` issues
  `createTask` with the title, the reference and a marker description, and supplies
  `record_type`, the product and, for an Issue, its kind and priority only afterwards through
  `saveTaskMetadata`.  As a card the intermediate row would be legal; here it is not a legal
  anything — `issues.product_id`, `issues.issue_kind` and `issues.priority` are `NOT NULL` with
  closed vocabularies (§3.2, §3.12).  So `createTask` **stages** the row inside the mutation's
  transaction and `saveTaskMetadata` inserts it, and the two reads in between —
  `getTaskByReference` and `getAllTasks` — see the staged row.  Nothing can commit while a stage
  is unfinished: `SqlCardClient.transaction` refuses, because a create that vanished silently is
  the one failure this adapter must not have.  Inventing a placeholder `issue_kind` was the
  alternative and it is exactly the "store a fact the board never stated" §8.6 refuses.
* **A close reason and a closed state are one fact.**  `issue_close_reason_matches_state` (§3.2)
  makes `(state = 'closed') = (close_reason IS NOT NULL)` an invariant of the row, while the
  board writes the reason first and the closure second.  Writing `issue_closed_reason` therefore
  closes the Issue, and the `closeTask` that follows finds it closed and is a no-op — the same
  end state, reached in one statement instead of two, because the schema does not admit the
  intermediate one.

What this module deliberately does not have is a lane.  §8.6 keeps the swimlane derived from the
product; `SqlCardClient._lane_names` builds the virtual lane table out of the products the store
holds, so a Product/Issue row's `swimlane_id` is its own product's lane by construction and the
`swimlane_id` a caller passes to `createTask` is not consulted for these two record types.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ummanu.board.backend import record_key, record_key_kind
from ummanu.board.extension_bag import EXTENSION_BAG

#: §8.1's Product keys, and the column or table each one is.
PRODUCT_KEYS = ("record_type", "product_id", "product_projects")

#: §8.1's Issue keys.  `issue_product` is `issues.product_id`, spelled as the bare product id the
#: way the board's metadata spells it.
ISSUE_KEYS = (
    "record_type",
    "issue_product",
    "issue_kind",
    "issue_priority",
    "issue_closed_reason",
)

_REQUEST_STAMP = re.compile(r"^\[request-id:([^\]\r\n]+)\]$")


class ProductIssueRecords:
    """The Product/Issue vocabulary over one `SqlCardClient`'s connection and transaction.

    It is not a second client: it holds the card client and issues its statements through
    `_query`/`_execute`, so a Product write and a card write inside one mutation are statements of
    one transaction (§7.1).
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    @property
    def staged(self) -> dict[int, dict[str, Any]]:
        """References staged by `createTask` and not yet inserted by `saveTaskMetadata`, keyed by
        the synthetic board key: this thread's open transaction's own (`SqlCardClient._staged`).
        Emptied by the insert, by a rollback, and checked at commit."""
        return self.client._staged("records")

    # --- identity --------------------------------------------------------------------

    @staticmethod
    def kind_of_reference(reference: str) -> str | None:
        text = str(reference or "")
        if text.startswith("product:"):
            return "product"
        if text.startswith("issue:"):
            return "issue"
        return None

    @staticmethod
    def identifier_of(reference: str) -> str:
        text = str(reference or "")
        return text.split(":", 1)[1] if ":" in text else text

    def _error(self, message: str) -> Exception:
        from ummanu.board.sql_cards import SqlCardError

        return SqlCardError(message)

    def _issues_column_id(self) -> int:
        """Read the Product/Issue column from the SQL client's one board vocabulary."""
        board = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(board, dict):  # pragma: no cover - the SQL vocabulary declares it
            raise self._error("the SQL board vocabulary has no Pipeline board")
        columns = self.client.call("getColumns", project_id=int(board["id"]))
        matches = [int(column["id"]) for column in columns if column.get("title") == "Issues"]
        if len(matches) != 1:
            raise self._error("the SQL board vocabulary must contain exactly one Issues column")
        return matches[0]

    def identifier_for(self, kind: str, task_id: int) -> str:
        """Resolve through the indexed stored key and verify its deterministic identity."""
        column, table = ("product_id", "products") if kind == "product" else ("issue_id", "issues")
        matches = self.client._query(
            f"SELECT {column} FROM {table} WHERE board_key = %s", (int(task_id),)
        )
        if not matches:
            raise self._error(f"no {kind} carries the board key {task_id}")
        identifier = str(matches[0][0])
        if record_key(kind, identifier) != int(task_id):
            raise self._error(f"{kind} {identifier!r} carries an invalid board key")
        return identifier

    # --- rows ------------------------------------------------------------------------

    def _staged_row(self, task_id: int) -> dict[str, Any]:
        staged = self.staged[int(task_id)]
        return {
            "id": int(task_id),
            "reference": staged["reference"],
            "title": staged["title"],
            "description": staged["description"],
            "column_id": self._issues_column_id(),
            "position": 0,
            "swimlane_id": self.client._lane_id(staged["lane"]),
            "date_creation": _epoch(staged["created_at"]),
            "date_modification": _epoch(staged["created_at"]),
            "is_active": 1,
        }

    def rows(self) -> list[dict[str, Any]]:
        """Every Product and Issue row the store holds, plus the staged creates of this
        transaction, in reference order."""
        result = [self._product_row(values) for values in self.client._query(
            "SELECT product_id, board_key, title, description, state, created_at, updated_at "
            "FROM products ORDER BY product_id"
        )]
        result += [self._issue_row(values) for values in self.client._query(
            "SELECT issue_id, board_key, product_id, title, description, state, created_at, updated_at "
            "FROM issues ORDER BY issue_id"
        )]
        result += [self._staged_row(key) for key in sorted(self.staged)]
        return result

    def _product_row(self, values: tuple[Any, ...]) -> dict[str, Any]:
        product_id, board_key_value, title, description, state, created, updated = values
        return {
            "id": int(board_key_value),
            "reference": f"product:{product_id}",
            "title": _text(title),
            "description": _text(description),
            "column_id": self._issues_column_id(),
            "position": 0,
            "swimlane_id": self.client._lane_id(product_id),
            "date_creation": _epoch(created),
            "date_modification": _epoch(updated),
            "is_active": 0 if state == "archived" else 1,
        }

    def _issue_row(self, values: tuple[Any, ...]) -> dict[str, Any]:
        issue_id, board_key_value, product_id, title, description, state, created, updated = values
        return {
            "id": int(board_key_value),
            "reference": f"issue:{issue_id}",
            "title": _text(title),
            "description": _text(description),
            "column_id": self._issues_column_id(),
            "position": 0,
            "swimlane_id": self.client._lane_id(product_id),
            "date_creation": _epoch(created),
            "date_modification": _epoch(updated),
            "is_active": 0 if state == "closed" else 1,
        }

    def row_by_reference(self, reference: str) -> dict[str, Any] | None:
        kind = self.kind_of_reference(reference)
        identifier = self.identifier_of(reference)
        key = record_key(kind, identifier) if kind else None
        if key is not None and key in self.staged:
            return self._staged_row(key)
        if kind == "product":
            rows = self.client._query(
                "SELECT product_id, board_key, title, description, state, created_at, updated_at "
                "FROM products WHERE product_id = %s",
                (identifier,),
            )
            return self._product_row(rows[0]) if rows else None
        rows = self.client._query(
            "SELECT issue_id, board_key, product_id, title, description, state, created_at, updated_at "
            "FROM issues WHERE issue_id = %s",
            (identifier,),
        )
        return self._issue_row(rows[0]) if rows else None

    def row(self, kind: str, task_id: int) -> dict[str, Any]:
        if int(task_id) in self.staged:
            return self._staged_row(int(task_id))
        identifier = self.identifier_for(kind, task_id)
        prefix = "product:" if kind == "product" else "issue:"
        row = self.row_by_reference(prefix + identifier)
        if row is None:  # pragma: no cover - `identifier_for` already proved the row is there
            raise self._error(f"no {kind} carries the board key {task_id}")
        return row

    # --- the vocabulary --------------------------------------------------------------

    def create(self, *, title: str, description: str, reference: str) -> int:
        """Stage the row this create names, and answer the board key it will carry.

        The insert waits for `saveTaskMetadata`, which is where the values the two tables require
        arrive; see this module's header for why an intermediate row is not written.
        """
        kind = self.kind_of_reference(reference)
        if kind is None:
            raise self._error(f"{reference!r} is not a Product or Issue reference")
        identifier = self.identifier_of(reference)
        if not identifier:
            raise self._error(f"{reference!r} names no {kind}")
        if self.row_by_reference(reference) is not None:
            raise self._error(f"{reference} already exists")
        key = record_key(kind, identifier)
        column, table = ("product_id", "products") if kind == "product" else ("issue_id", "issues")
        collision = self.client._query(
            f"SELECT {column} FROM {table} WHERE board_key = %s", (key,)
        )
        if collision:
            raise self._error(
                f"{kind} board-key collision between {identifier!r} and {collision[0][0]!r}"
            )
        lane = identifier if kind == "product" else None
        self.client._staged("records")[key] = {
            "reference": reference,
            "title": title,
            "description": description or "",
            "lane": lane,
            "created_at": _now(),
        }
        return key

    def update(self, task_id: int, fields: dict[str, Any]) -> bool:
        """`updateTask`: the reference, title and description a create finishes with."""
        key = int(task_id)
        if key in self.staged:
            staged = self.staged[key]
            if "reference" in fields and str(fields["reference"]) != staged["reference"]:
                raise self._error(
                    "a staged Product/Issue create cannot change its reference: the board key is "
                    "minted from it"
                )
            for name in ("title", "description"):
                if name in fields:
                    staged[name] = _text(fields[name])
            return True
        kind = record_key_kind(key)
        identifier = self.identifier_for(kind, key)
        if "reference" in fields and str(fields["reference"]) != (
            f"product:{identifier}" if kind == "product" else f"issue:{identifier}"
        ):
            raise self._error(
                "a Product/Issue reference is its identity (§9) and the store has no rename"
            )
        assignments, params = [], []
        for name in ("title", "description"):
            if name in fields:
                assignments.append(f"{name} = %s")
                params.append(_text(fields[name]))
        if not assignments:
            return True
        assignments.append("updated_at = %s")
        params.append(_now())
        params.append(identifier)
        table, column = ("products", "product_id") if kind == "product" else ("issues", "issue_id")
        self.client._execute(
            f"UPDATE {table} SET {', '.join(assignments)} WHERE {column} = %s", tuple(params)
        )
        return True

    def close(self, task_id: int) -> bool:
        """`closeTask`: a Product is archived, an Issue is closed (§3.1, §3.2)."""
        key = int(task_id)
        if key in self.staged:
            raise self._error("a Product/Issue create that is not finished cannot be closed")
        kind = record_key_kind(key)
        identifier = self.identifier_for(kind, key)
        if kind == "product":
            self.client._execute(
                "UPDATE products SET state = 'archived', updated_at = %s WHERE product_id = %s",
                (_now(), identifier),
            )
            return True
        reason = self.client._query(
            "SELECT close_reason FROM issues WHERE issue_id = %s", (identifier,)
        )[0][0]
        if reason is None:
            raise self._error(
                "an Issue is closed with its reason: issue_close_reason_matches_state (§3.2) "
                "admits no closed Issue without one, so write issue_closed_reason first"
            )
        self.client._execute(
            "UPDATE issues SET state = 'closed', updated_at = %s WHERE issue_id = %s",
            (_now(), identifier),
        )
        return True

    def _stored(self, kind: str, keys: list[int], columns: str) -> dict[int, tuple[Any, ...]]:
        """`identifier_for` for many keys: each key's row, its identity checked against the key."""
        column, table = ("product_id", "products") if kind == "product" else ("issue_id", "issues")
        rows = {
            int(values[0]): values[1:]
            for values in self.client._query(
                f"SELECT board_key, {column}, {columns} FROM {table} "
                "WHERE board_key = ANY(%s::bigint[])",
                (keys,),
            )
        }
        for key in keys:
            if key not in rows:
                raise self._error(f"no {kind} carries the board key {key}")
            if record_key(kind, str(rows[key][0])) != key:
                raise self._error(f"{kind} {str(rows[key][0])!r} carries an invalid board key")
        return rows

    def _by_kind(self, task_ids: list[int]) -> tuple[list[int], dict[str, list[int]]]:
        """Split keys into the staged creates and the stored rows of each table."""
        staged: list[int] = []
        stored: dict[str, list[int]] = {"product": [], "issue": []}
        for key in map(int, task_ids):
            if key in self.staged:
                staged.append(key)
            else:
                stored[str(record_key_kind(key))].append(key)
        return staged, stored

    def metadata_of(self, task_ids: list[int]) -> dict[int, dict[str, str]]:
        """Product and Issue metadata for every key, in one read per table."""
        staged, stored = self._by_kind(task_ids)
        # A staged create carries no typed values yet, and says so rather than guessing: the host
        # reads this row with `allow_incomplete` while it finishes the create.
        result: dict[int, dict[str, str]] = {key: {} for key in staged}
        if stored["product"]:
            products = self._stored("product", stored["product"], "extensions")
            projects = _grouped(self.client._query(
                "SELECT product_id, project_id FROM product_projects "
                "WHERE product_id = ANY(%s::text[]) ORDER BY product_id, project_id",
                ([str(products[key][0]) for key in stored["product"]],),
            ))
            for key in stored["product"]:
                identifier, extensions = products[key]
                identifier = str(identifier)
                meta = {
                    "record_type": "product",
                    "product_id": identifier,
                    "product_projects": json.dumps(
                        projects.get(identifier, []), separators=(",", ":")
                    ),
                }
                bag = extensions if isinstance(extensions, dict) else json.loads(extensions or "{}")
                for name, value in (bag.get(EXTENSION_BAG) or {}).items():
                    if name not in PRODUCT_KEYS:
                        meta[name] = _text(value)
                result[key] = meta
        if stored["issue"]:
            issues = self._stored(
                "issue", stored["issue"], "product_id, issue_kind, priority, close_reason, extensions"
            )
            for key in stored["issue"]:
                _identifier, product_id, issue_kind, priority, close_reason, extensions = issues[key]
                meta = {
                    "record_type": "issue",
                    "issue_product": _text(product_id),
                    "issue_kind": _text(issue_kind),
                    "issue_priority": _text(priority),
                }
                if close_reason:
                    meta["issue_closed_reason"] = _text(close_reason)
                bag = extensions if isinstance(extensions, dict) else json.loads(extensions or "{}")
                for name, value in (bag.get(EXTENSION_BAG) or {}).items():
                    if name not in ISSUE_KEYS:
                        meta[name] = _text(value)
                result[key] = meta
        return result

    def save_metadata(self, task_id: int, values: dict[str, Any]) -> bool:
        key = int(task_id)
        if key in self.staged:
            return self._insert(key, values)
        kind = record_key_kind(key)
        identifier = self.identifier_for(kind, key)
        if kind == "product":
            return self._update_product(identifier, values)
        return self._update_issue(identifier, values)

    def _insert(self, key: int, values: dict[str, Any]) -> bool:
        """Finish a staged create: the row the two tables can actually hold."""
        staged = self.staged[key]
        reference = str(staged["reference"])
        kind = self.kind_of_reference(reference)
        identifier = self.identifier_of(reference)
        declared = _text(values.get("record_type"))
        if declared != kind:
            raise self._error(
                f"a create of {reference} is finished by its own record type: the metadata says "
                f"{declared or 'nothing'}, the reference says {kind}"
            )
        now = _now()
        if kind == "product":
            product_id = _text(values.get("product_id"))
            if product_id != identifier:
                raise self._error(
                    f"a Product's reference and its product_id must be the same record: "
                    f"{reference} and {product_id!r}"
                )
            self.client._execute(
                "INSERT INTO products (product_id, board_key, title, description, state, extensions, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, 'active', %s::jsonb, %s, %s)",
                (product_id, key, staged["title"], staged["description"],
                 json.dumps({EXTENSION_BAG: self._extension_bag(values, PRODUCT_KEYS)}),
                 staged["created_at"], now),
            )
            self._write_projects(product_id, values.get("product_projects"))
            self.client._lane_added(product_id)
        else:
            product_id = _text(values.get("issue_product"))
            if not product_id:
                raise self._error(f"an Issue names its Product: {reference} named none")
            self.client._execute(
                "INSERT INTO issues (issue_id, board_key, product_id, title, description, issue_kind, "
                "priority, state, extensions, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s::jsonb, %s, %s)",
                (
                    identifier,
                    key,
                    product_id,
                    staged["title"],
                    staged["description"],
                    _text(values.get("issue_kind")),
                    _text(values.get("issue_priority")),
                    json.dumps({EXTENSION_BAG: self._extension_bag(values, ISSUE_KEYS)}),
                    staged["created_at"],
                    now,
                ),
            )
            reason = _text(values.get("issue_closed_reason"))
            if reason:
                self._close_issue_with_reason(identifier, reason)
        del self.staged[key]
        return True

    def _write_projects(self, product_id: str, raw: Any) -> None:
        """`product_projects`, and the `projects` rows §3.1 says a mutation projects for itself."""
        try:
            projects = json.loads(_text(raw) or "[]")
        except json.JSONDecodeError:
            raise self._error("a Product's project set must be a JSON array") from None
        if not isinstance(projects, list) or any(not isinstance(item, str) for item in projects):
            raise self._error("a Product's project set must be a JSON array of ids")
        self.client._execute("DELETE FROM product_projects WHERE product_id = %s", (product_id,))
        for project in sorted(set(projects)):
            # §3.1: `projects` is derived from the file registry, which stays canonical.  The
            # mutation that references an id inserts a missing row inside its own transaction,
            # with the id only: everything else in the row is the registry's.
            self.client._execute(
                "INSERT INTO projects (project_id) VALUES (%s) ON CONFLICT (project_id) DO NOTHING",
                (project,),
            )
            self.client._execute(
                "INSERT INTO product_projects (product_id, project_id) VALUES (%s, %s)",
                (product_id, project),
            )

    def _update_product(self, product_id: str, values: dict[str, Any]) -> bool:
        if _text(values.get("record_type")) not in {"", "product"}:
            raise self._error("a Product's record type is its table and cannot be rewritten")
        declared = _text(values.get("product_id"))
        if declared and declared != product_id:
            raise self._error("a Product's id is its identity (§9) and the store has no rename")
        if "product_projects" in values:
            self._write_projects(product_id, values.get("product_projects"))
        bag = self._extension_bag(values, PRODUCT_KEYS)
        assignments: list[str] = []
        params: list[Any] = []
        if bag:
            assignments.append(
                f"extensions = jsonb_set(coalesce(extensions, '{{}}'::jsonb), '{{{EXTENSION_BAG}}}', "
                f"coalesce(extensions->'{EXTENSION_BAG}', '{{}}'::jsonb) || %s::jsonb, true)"
            )
            params.append(json.dumps(bag))
        assignments.append("updated_at = %s")
        params.extend((_now(), product_id))
        self.client._execute(
            f"UPDATE products SET {', '.join(assignments)} WHERE product_id = %s", tuple(params)
        )
        return True

    def _update_issue(self, issue_id: str, values: dict[str, Any]) -> bool:
        if _text(values.get("record_type")) not in {"", "issue"}:
            raise self._error("an Issue's record type is its table and cannot be rewritten")
        assignments, params = [], []
        for key, column in (
            ("issue_product", "product_id"),
            ("issue_kind", "issue_kind"),
            ("issue_priority", "priority"),
        ):
            if key in values:
                assignments.append(f"{column} = %s")
                params.append(_text(values[key]))
        bag = self._extension_bag(values, ISSUE_KEYS)
        if bag:
            assignments.append(
                f"extensions = jsonb_set(coalesce(extensions, '{{}}'::jsonb), '{{{EXTENSION_BAG}}}', "
                f"coalesce(extensions->'{EXTENSION_BAG}', '{{}}'::jsonb) || %s::jsonb, true)"
            )
            params.append(json.dumps(bag))
        if assignments:
            assignments.append("updated_at = %s")
            params.append(_now())
            params.append(issue_id)
            self.client._execute(
                f"UPDATE issues SET {', '.join(assignments)} WHERE issue_id = %s", tuple(params)
            )
        if "issue_closed_reason" in values:
            reason = _text(values["issue_closed_reason"])
            if reason:
                self._close_issue_with_reason(issue_id, reason)
        return True

    def _close_issue_with_reason(self, issue_id: str, reason: str) -> None:
        """The reason and the closed state, written together because §3.2 makes them one fact."""
        self.client._execute(
            "UPDATE issues SET close_reason = %s, state = 'closed', updated_at = %s "
            "WHERE issue_id = %s",
            (reason, _now(), issue_id),
        )

    def _extension_bag(self, values: dict[str, Any], modelled: tuple[str, ...]) -> dict[str, str]:
        """§8.2: the keys the model does not name, kept rather than dropped."""
        return {
            key: _text(value)
            for key, value in values.items()
            if key not in modelled
        }

    # --- comments --------------------------------------------------------------------

    def _comment_table(self, kind: str) -> tuple[str, str]:
        return (
            ("product_comments", "product_id")
            if kind == "product"
            else ("issue_comments", "issue_id")
        )

    @staticmethod
    def _comment_request_id(content: str) -> str | None:
        """Return the canonical request stamp when the comment carries one."""
        for line in reversed(content.splitlines()):
            match = _REQUEST_STAMP.fullmatch(line)
            if match is not None:
                return match.group(1)
        return None

    def comments_of(self, task_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Product and Issue comments for every key, in one read per table."""
        staged, stored = self._by_kind(task_ids)
        result: dict[int, list[dict[str, Any]]] = {key: [] for key in staged}
        for kind, keys in stored.items():
            if not keys:
                continue
            identifiers = {key: str(values[0]) for key, values in self._stored(kind, keys, "1").items()}
            table, column = self._comment_table(kind)
            comments = _grouped(self.client._query(
                f"SELECT {column}, comment_id, body, created_at FROM {table} "
                f"WHERE {column} = ANY(%s::text[]) ORDER BY {column}, created_at, comment_id",
                ([identifiers[key] for key in keys],),
            ))
            for key in keys:
                result[key] = [
                    {"id": identifier_value, "date_creation": _epoch(created), "comment": body}
                    for identifier_value, body, created in comments.get(identifiers[key], [])
                ]
        return result

    def create_comment(self, task_id: int, content: str) -> int:
        key = int(task_id)
        if key in self.staged:
            raise self._error("a Product/Issue create that is not finished carries no comment")
        kind = record_key_kind(key)
        identifier = self.identifier_for(kind, key)
        table, column = self._comment_table(kind)
        first = content.splitlines()[0] if content else ""
        marker = first[1:-1] if first.startswith("[") and first.endswith("]") else None
        request_id = self._comment_request_id(content)
        if request_id is not None:
            existing = self.client._query(
                f"SELECT comment_id, {column}, body FROM {table} WHERE request_id = %s",
                (request_id,),
            )
            if existing:
                comment_id, owner, body = existing[0]
                if owner != identifier or body != content:
                    raise self._error("request id belongs to another comment or entity")
                return int(comment_id)
        rows = self.client._query(
            f"INSERT INTO {table} ({column}, marker, body, request_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING comment_id",
            (identifier, marker, content, request_id, _now()),
        )
        return int(rows[0][0])


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _epoch(value: Any) -> str:
    from ummanu.board.sql_cards import _epoch as card_epoch

    return card_epoch(value)


def _grouped(rows: Any) -> dict[Any, list[Any]]:
    from ummanu.board.sql_cards import _grouped as card_grouped

    return card_grouped(rows)


def _now() -> Any:
    from ummanu.board.sql_cards import _now as card_now

    return card_now()


__all__ = ["ISSUE_KEYS", "PRODUCT_KEYS", "ProductIssueRecords"]
