"""Board data across the rename (`docs/RENAME.md` §T3): dump, stop, restore, translate, count, and
afterwards archive one Product (`products`).

The product's recovery primitives are used by module, and every name is passed in: the old store
is read from the old `board-store.env` keys of the transition's own table, never through
`board.store.resolve`, whose keys the rename changes. The restore and the provisioning run in the
renamed tree against the rewritten `board-store.env`, which is exactly what `resolve` reads there.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from ..board import (
    backend as board_backend,
    migrate as board_migrate,
    postgres_recovery,
    provision as board_provision,
    store as board_store,
)
from .context import TransitionError
from .names import ADDED_ROWS, INSTANCE_PROJECT, UNCHANGED_TABLES, Names

STORE_FILE = "board-store.env"


def read_store_env(path: Path, names: Names) -> board_store.BoardStoreConfig:
    """The nine keys of `names`, all present and nothing else, as the store's own config."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TransitionError(f"cannot read {path}: {exc}") from None
    fields: dict[str, str] = {}
    for number, line in enumerate(lines, 1):
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in names.board_store_env or key in fields or not value:
            raise TransitionError(f"{path} line {number} is not one of the {names.env_prefix}DB_* keys")
        fields[key] = value
    missing = [key for key in names.board_store_env if key not in fields]
    if missing:
        raise TransitionError(f"{path} lacks {', '.join(missing)}")
    values = [fields[key] for key in names.board_store_env]
    if not values[1].isdigit():
        raise TransitionError(f"{path} names no TCP port")
    return board_store.BoardStoreConfig(
        host=values[0],
        port=int(values[1]),
        dbname=values[2],
        owner_user=values[3],
        owner_password=values[4],
        app_user=values[5],
        app_password=values[6],
        read_user=values[7],
        read_password=values[8],
    )


def store_env_text(config: board_store.BoardStoreConfig, names: Names) -> str:
    values = (
        config.host, str(config.port), config.dbname,
        config.owner_user, config.owner_password,
        config.app_user, config.app_password,
        config.read_user, config.read_password,
    )
    return "".join(f"{key}={value}\n" for key, value in zip(names.board_store_env, values, strict=True))


def renamed_config(config: board_store.BoardStoreConfig, new: Names) -> board_store.BoardStoreConfig:
    """Same endpoint and passwords; the new database and role names (§T2.4)."""
    return board_store.BoardStoreConfig(
        host=config.host,
        port=config.port,
        dbname=new.db_name,
        owner_user=new.db_owner,
        owner_password=config.owner_password,
        app_user=new.db_app,
        app_password=config.app_password,
        read_user=new.db_read,
        read_password=config.read_password,
    )


def write_store_env(path: Path, text: str) -> None:
    """Replace the file whole at mode 0600; never a moment with a broader mode or half a file."""
    temporary = path.with_name(f".{path.name}.transition")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)


# -- reads ---------------------------------------------------------------------------------------


def _connect(config: board_store.BoardStoreConfig, role: str) -> Any:
    import psycopg

    try:
        return psycopg.connect(config.for_role(role).conninfo(), connect_timeout=5)
    except psycopg.Error as exc:
        raise TransitionError(
            f"cannot reach the board store {config.dbname}@{config.host}:{config.port} as {role}: "
            + str(exc).strip().splitlines()[0]
        ) from None


def sprint_markers(config: board_store.BoardStoreConfig, sprint: str, markers: tuple[str, ...]) -> dict[str, int]:
    """The newest comment id of `sprint` whose body carries each marker; absent markers are left out."""
    found: dict[str, int] = {}
    with _connect(config, "read") as connection:
        for marker in markers:
            row = connection.execute(
                "SELECT max(comment_id) FROM sprint_comments WHERE sprint_ref = %s AND strpos(body, %s) > 0",
                (sprint, marker),
            ).fetchone()
            if row and row[0] is not None:
                found[marker] = int(row[0])
    return found


def prepared_sprints(config: board_store.BoardStoreConfig, marker: str) -> list[str]:
    """Open sprints carrying the observer's baseline comment."""
    with _connect(config, "read") as connection:
        rows = connection.execute(
            "SELECT DISTINCT s.ref FROM sprints s JOIN sprint_comments c ON c.sprint_ref = s.ref "
            "WHERE s.status = 'open' AND strpos(c.body, %s) > 0 ORDER BY s.ref",
            (marker,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def table_counts(config: board_store.BoardStoreConfig, role: str = "read") -> dict[str, int]:
    """Rows per public table except `alembic_version`; `{}` for a store with no tables yet."""
    from psycopg import sql

    with _connect(config, role) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version' ORDER BY tablename"
            ).fetchall()
        ]
        return {
            name: int(
                connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0]
            )
            for name in names
        }


# -- dump ----------------------------------------------------------------------------------------


def inspect_source(config: board_store.BoardStoreConfig) -> dict[str, Any]:
    """`postgres_recovery.inspect_source` over an explicit config: the dump metadata minus counts."""
    import psycopg

    try:
        with _connect(config, "owner") as connection:
            server_num = int(connection.execute("SHOW server_version_num").fetchone()[0])
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except (psycopg.Error, TypeError, ValueError) as exc:
        raise TransitionError("old board store preflight failed: " + str(exc).strip().splitlines()[0]) from None
    server_major = server_num // 10000
    if server_major != board_provision.POSTGRES_MAJOR:
        raise TransitionError(
            f"old board store runs PostgreSQL {server_major}, the client is {board_provision.POSTGRES_MAJOR}"
        )
    head = board_migrate.head_revision()
    current = str(revision[0]) if revision else ""
    if current != head:
        raise TransitionError(f"old board store is at schema {current or '(none)'}; this code ships {head}")
    return {
        "engine": postgres_recovery.ENGINE,
        "format": postgres_recovery.DUMP_FORMAT,
        "dump_version": 1,
        "image": board_provision.IMAGE,
        "server_major": server_major,
        "source_schema": current,
        "alembic_head": head,
        "restore_purpose": postgres_recovery.DUMP_PURPOSE,
        "source_endpoint_id": postgres_recovery.endpoint_identity(config),
    }


def dump(config: board_store.BoardStoreConfig, directory: Path) -> dict[str, Any]:
    """Dump once; a rerun with a complete dump and its metadata on disk returns that metadata."""
    archive = directory / "postgres.dump"
    metadata_path = directory / "dump.json"
    if archive.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("bytes") == archive.stat().st_size and isinstance(metadata.get("table_counts"), dict):
            return dict(metadata)
    try:
        metadata = postgres_recovery.create_dump(config, archive, inspect_source(config))
    except postgres_recovery.PostgresRecoveryError as exc:
        raise TransitionError(str(exc)) from None
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metadata_path.chmod(0o600)
    return dict(metadata)


def compose_argv(compose_path: Path, project: str, env_file: Path, *arguments: str) -> list[str]:
    return [
        "docker", "compose",
        "--project-name", project,
        "--env-file", str(env_file),
        "--file", str(compose_path),
        *arguments,
    ]


# -- restore and translate -----------------------------------------------------------------------


def require_renamed_tree(new: Names, compose_path: Path) -> None:
    """Provisioning and restore run only in the renamed tree: its store constants are the new names."""
    if board_provision.PROJECT != new.compose_project or board_provision.DEFAULT_COMPOSE_PATH != compose_path:
        raise TransitionError(
            f"this code provisions {board_provision.PROJECT} at {board_provision.DEFAULT_COMPOSE_PATH}, not "
            f"{new.compose_project} at {compose_path}: run this step from the renamed checkout"
        )


def provision_new_store(instance: Path, compose_path: Path) -> tuple[str, ...]:
    try:
        outcome = board_provision.provision(
            instance,
            compose_path=compose_path,
            privileged_argv=lambda argv: ["sudo", "-n", *argv],
        )
    except (board_store.BoardStoreError, OSError, RuntimeError) as exc:
        raise TransitionError(f"provisioning the new board store failed: {exc}") from None
    if outcome is None:
        raise TransitionError("provisioning found no board-store.env")
    return tuple(outcome.actions)


def restore(archive: Path, instance: Path, metadata: dict[str, Any], config: board_store.BoardStoreConfig) -> str:
    """Restore into the empty new store; a store already holding the dump (or the translated dump)
    is the finished state of an earlier run and is left alone."""
    expected = {str(name): int(count) for name, count in metadata["table_counts"].items()}
    current = table_counts(config, "owner")
    if current and any(current.values()):
        if current == expected or current == with_added_rows(expected):
            return "already restored"
        raise TransitionError("the new board store holds data that is not this dump; refusing to restore over it")
    try:
        postgres_recovery.restore_dump(archive, instance, metadata)
    except postgres_recovery.PostgresRecoveryError as exc:
        raise TransitionError(str(exc)) from None
    return "restored"


def with_added_rows(counts: dict[str, int]) -> dict[str, int]:
    return {name: count + ADDED_ROWS.get(name, 0) for name, count in counts.items()}


def translate(config: board_store.BoardStoreConfig, old: Names, new: Names, *,
              old_root: Path, new_root: Path, old_po: Path, new_po: Path) -> dict[str, Any]:
    """§T2.6 in one owner transaction. Only open sprints, open issues and open PO sessions move;
    closed records, every card, events, requests and comments keep the old identity (class R)."""
    with _connect(config, "owner") as connection, connection.transaction():
        if connection.execute("SELECT 1 FROM products WHERE product_id = %s", (new.product_id,)).fetchone():
            return {"translated": False, "reason": f"product {new.product_id} already exists"}
        if not connection.execute("SELECT 1 FROM products WHERE product_id = %s", (old.product_id,)).fetchone():
            raise TransitionError(f"the restored board has no product {old.product_id}")
        connection.execute(
            "INSERT INTO products (product_id, board_key, title, description, state, extensions, created_at, "
            "updated_at) SELECT %s, %s, %s, description, 'active', extensions, now(), now() "
            "FROM products WHERE product_id = %s",
            (new.product_id, board_backend.record_key("product", new.product_id), new.product_title,
             old.product_id),
        )
        connection.execute("UPDATE products SET state = 'archived' WHERE product_id = %s", (old.product_id,))
        connection.execute(
            "INSERT INTO projects (project_id, enabled, plane, adapter, orca_binding, registry_present) "
            "SELECT %s, true, plane, %s, %s, true FROM projects WHERE project_id = %s",
            (new.project_id, new.project_id, new.project_id, old.project_id),
        )
        connection.execute(
            "UPDATE projects SET enabled = false, registry_present = false WHERE project_id = %s",
            (old.project_id,),
        )
        new_repository = connection.execute(
            "INSERT INTO repositories (project_id, path, remote, default_branch, role) "
            "VALUES (%s, %s, %s, 'main', 'primary') RETURNING repository_id",
            (new.project_id, str(new_root), new.remote),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO product_projects (product_id, project_id) VALUES (%s, %s), (%s, %s)",
            (new.product_id, new.project_id, new.product_id, INSTANCE_PROJECT),
        )
        sprints = [
            str(row[0])
            for row in connection.execute(
                "SELECT ref FROM sprints WHERE status = 'open' AND (product_id = %s OR %s = ANY(allowed_productions) "
                "OR ref IN (SELECT sprint_ref FROM sprint_projects WHERE project_id = %s)) ORDER BY ref",
                (old.product_id, old.project_id, old.project_id),
            ).fetchall()
        ]
        connection.execute(
            "UPDATE sprints SET product_id = CASE WHEN product_id = %s THEN %s ELSE product_id END, "
            "allowed_productions = array_replace(allowed_productions, %s, %s) WHERE ref = ANY(%s)",
            (old.product_id, new.product_id, old.project_id, new.project_id, sprints),
        )
        connection.execute(
            "UPDATE sprint_projects SET project_id = %s WHERE project_id = %s AND sprint_ref = ANY(%s)",
            (new.project_id, old.project_id, sprints),
        )
        connection.execute(
            "UPDATE sprint_repositories SET repository_id = %s WHERE sprint_ref = ANY(%s) AND repository_id IN "
            "(SELECT repository_id FROM repositories WHERE path = %s)",
            (new_repository, sprints, str(old_root)),
        )
        issues = connection.execute(
            "UPDATE issues SET product_id = %s WHERE product_id = %s AND state = 'open'",
            (new.product_id, old.product_id),
        ).rowcount
        sessions = connection.execute(
            "UPDATE po_sessions SET cwd = %s WHERE state = 'open' AND cwd = %s",
            (str(new_po), str(old_po)),
        ).rowcount
    return {
        "translated": True,
        "sprints": sprints,
        "issues": int(issues),
        "po_sessions": int(sessions),
        "repository_id": int(new_repository),
    }


def product_state(connection: Any, product_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    """One Product's row, its projects, and its issues and sprints by state; None when it is absent."""
    row = connection.execute(
        "SELECT state, title FROM products WHERE product_id = %s" + (" FOR UPDATE" if lock else ""), (product_id,)
    ).fetchone()
    if row is None:
        return None
    projects = connection.execute(
        "SELECT project_id FROM product_projects WHERE product_id = %s ORDER BY project_id", (product_id,)
    ).fetchall()
    issues = connection.execute(
        "SELECT state, count(*) FROM issues WHERE product_id = %s GROUP BY state ORDER BY state", (product_id,)
    ).fetchall()
    sprints = connection.execute(
        "SELECT ref, status FROM sprints WHERE product_id = %s ORDER BY ref", (product_id,)
    ).fetchall()
    return {
        "state": str(row[0]),
        "title": str(row[1]),
        "projects": [str(project[0]) for project in projects],
        "issues": {str(state): int(count) for state, count in issues},
        "sprints": [f"{ref} ({status})" for ref, status in sprints],
    }


def archive_product(
    config: board_store.BoardStoreConfig, product_id: str, check: Any, *, apply: bool
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read one Product and, with `apply`, archive it in the same owner transaction (the §T2.6 SQL).

    `check(before)` decides on the locked row and raises `TransitionError` to refuse, or returns
    whether the archive is still to be written. Returns the row before and after (`None` when
    nothing was written). Only `products.state` changes: not `updated_at`, not its projects.
    """
    with _connect(config, "owner" if apply else "read") as connection, connection.transaction():
        before = product_state(connection, product_id, lock=apply)
        if not check(before) or not apply:
            return before, None
        changed = connection.execute(
            "UPDATE products SET state = 'archived' WHERE product_id = %s AND state = 'active'", (product_id,)
        ).rowcount
        if changed != 1:
            raise TransitionError(f"product {product_id} changed while it was archived; nothing written")
        return before, product_state(connection, product_id)


def verify_counts(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Problems with `after` against the dump: every table equal except the §T2.7 added rows."""
    problems = []
    expected = with_added_rows(before)
    for table in sorted(set(expected) | set(after)):
        if expected.get(table) != after.get(table):
            problems.append(f"{table}: dump {before.get(table)}, expected {expected.get(table)}, now {after.get(table)}")
    for table in UNCHANGED_TABLES:
        if before.get(table) is None:
            problems.append(f"{table}: missing from the dump counts")
    return problems


__all__ = [
    "STORE_FILE",
    "archive_product",
    "compose_argv",
    "dump",
    "inspect_source",
    "prepared_sprints",
    "product_state",
    "provision_new_store",
    "read_store_env",
    "renamed_config",
    "require_renamed_tree",
    "restore",
    "sprint_markers",
    "store_env_text",
    "table_counts",
    "translate",
    "verify_counts",
    "with_added_rows",
    "write_store_env",
]
