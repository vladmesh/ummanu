"""A throwaway `postgres:16`, and the seeding that puts a starting board into it.

Cards have one implementation, the PostgreSQL one, so every test that needs a card board gets a
real store: `card_store()` hands a test its own migrated database, seeded from a `CardSeed`
(`tests/fakes/tasks.py`) through the same `SqlCardClient` the product uses, and removes it when the
test ends. Nothing here reaches a live installation: the container publishes on loopback, holds no
volume, is addressed only by the id `docker run` returned, and is removed when the process exits.

There is no skip.  `tests/test_board_store_schema.py` states the reason and it is the same one
here: a backend proof that quietly passes because it never reached a database is worth less than
no proof at all.  The suites that use it are the integration shards, where Docker and the core
dependencies are both present.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from tests.container_cleanup import remove_test_container
from ummanu.board import migrate, schema
from ummanu.board.backend import record_key, sprint_reference_number
from ummanu.board.sql_cards import SqlCardClient, _task_number_of
from ummanu.board.store import BoardStoreConfig
from ummanu.runtime.container_labels import TEST_BOARD_LABEL

IMAGE = "postgres:16"
OWNER = "ummanu_owner"
OWNER_PASSWORD = "throwaway-owner-password"
APP_PASSWORD = "throwaway@app/pass:word"
READ_PASSWORD = "throwaway-read-password"
READY_TIMEOUT_SECONDS = 90


def docker(*arguments: str) -> str:
    completed = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=180, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


class PostgresBoard:
    """One container, and one fresh database per test cloned from a migrated template."""

    _shared: ClassVar[PostgresBoard | None] = None
    TEMPLATE = "board_store_template"

    @classmethod
    def shared(cls) -> PostgresBoard:
        """The process's one container, started on first use and removed when the process exits.

        A container per test module cost a server start per module; the card suites are dozens of
        modules, and every one of them only needs a database of its own per test.
        """
        if cls._shared is None:
            for module in ("psycopg", "sqlalchemy", "alembic"):
                __import__(module)
            board = cls.__new__(cls)
            board.__init__()
            cls._shared = board
        return cls._shared

    def __init__(self) -> None:
        self.container = ""
        self._stopped = False
        self.container = docker(
            "run",
            "--rm",
            "-d",
            "--label",
            f"{TEST_BOARD_LABEL}={os.getpid()}",
            "-e",
            "POSTGRES_DB=postgres",
            "-e",
            f"POSTGRES_USER={OWNER}",
            "-e",
            f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=1g",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
            # A throwaway store holds nothing worth a flush: the suite issues tens of thousands of
            # small commits, and waiting on each is most of what a card test would otherwise cost.
            "-c",
            "fsync=off",
            "-c",
            "synchronous_commit=off",
            "-c",
            "full_page_writes=off",
        )
        # Register before inspecting the port or waiting for PostgreSQL: direct callers may
        # construct this fixture before they have a chance to register their own cleanup.
        atexit.register(self.stop)
        published = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Ports}}", self.container))
        self.host = "127.0.0.1"
        self.port = int(published["5432/tcp"][0]["HostPort"])
        self._serial = 0
        self._template_ready = False
        #: Released databases, emptied and ready to be handed out again (`release_database`).
        self._pool: list[str] = []
        self._await_server()
        self._prefer_the_bridge()

    def stop(self) -> None:
        if self._stopped:
            return
        if self.container:
            remove_test_container(self.container)
        self._stopped = True

    def _await_server(self) -> None:
        import psycopg

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        last = ""
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.config("postgres").for_role("owner").conninfo(), connect_timeout=3):
                    return
            except psycopg.Error as exc:
                last = str(exc).strip()
                time.sleep(0.5)
        raise RuntimeError(f"the throwaway {IMAGE} never accepted a connection: {last}")

    def _prefer_the_bridge(self) -> None:
        """Talk to the container on its own address when this host can reach it.

        The published loopback port goes through Docker's userland proxy, which roughly doubles
        the latency of every statement; a card test issues about a thousand of them. Where the
        bridge address answers (a Linux host, a CI runner), it is used; elsewhere the port stays.
        """
        import psycopg

        address = docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", self.container
        ).strip()
        if not address:
            return
        published = (self.host, self.port)
        self.host, self.port = address, 5432
        try:
            with psycopg.connect(self.config("postgres").for_role("owner").conninfo(), connect_timeout=3):
                return
        except psycopg.Error:
            self.host, self.port = published

    def config(self, dbname: str) -> BoardStoreConfig:
        return BoardStoreConfig(
            host=self.host,
            port=self.port,
            dbname=dbname,
            owner_user=OWNER,
            owner_password=OWNER_PASSWORD,
            app_user=schema.APP_ROLE,
            app_password=APP_PASSWORD,
            read_user=schema.READ_ROLE,
            read_password=READ_PASSWORD,
        )

    def _migrate_template(self) -> None:
        """Migrate one database once; every test database is a copy of it.

        `CREATE DATABASE ... TEMPLATE` copies a migrated store in tens of milliseconds, where
        migrating each one cost most of a second. The template is never connected to again, which is
        what PostgreSQL requires of a template.
        """
        import psycopg
        import sqlalchemy as sa

        with psycopg.connect(
            self.config("postgres").for_role("owner").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {self.TEMPLATE} WITH (FORCE)")
            maintenance.execute(f"CREATE DATABASE {self.TEMPLATE} OWNER {OWNER}")
        config = self.config(self.TEMPLATE)
        engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("owner")))
        try:
            with engine.connect() as connection:
                migrate.apply(connection, passwords=migrate.passwords_for(config))
        finally:
            engine.dispose()
        self._template_ready = True

    def fresh_database(self) -> BoardStoreConfig:
        """A migrated, empty store of its own, so no test can see another's rows."""
        import psycopg

        if self._pool:
            return self.config(self._pool.pop())
        if not self._template_ready:
            self._migrate_template()
        self._serial += 1
        name = f"board_store_case_{self._serial}"
        with psycopg.connect(
            self.config("postgres").for_role("owner").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            maintenance.execute(
                f"CREATE DATABASE {name} OWNER {OWNER} TEMPLATE {self.TEMPLATE} STRATEGY FILE_COPY"
            )
        return self.config(name)

    def release_database(self, name: str) -> None:
        """Empty a database a test is done with and keep it for the next one.

        Emptying a migrated store costs about half of cloning one and dropping it again, and a test
        suite asks for thousands. Every other session on it is ended first, so a connection the
        test left open cannot hold a lock the emptying waits on; a database that cannot be emptied
        is dropped instead of reused.
        """
        import psycopg

        try:
            with psycopg.connect(self.config(name).for_role("owner").conninfo(), autocommit=True) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
                tables = [
                    row[0]
                    for row in conn.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename <> 'alembic_version'"
                    )
                ]
                sequences = [
                    row[0]
                    for row in conn.execute(
                        "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
                    )
                ]
                if tables:
                    conn.execute(
                        "TRUNCATE "
                        + ", ".join(f'"{table}"' for table in tables)
                        + " RESTART IDENTITY CASCADE"
                    )
                for sequence in sequences:
                    conn.execute(f'ALTER SEQUENCE "{sequence}" RESTART')
        except psycopg.Error:
            self.drop_database(name)
            return
        self._pool.append(name)

    def drop_database(self, name: str) -> None:
        """Release one per-test database as soon as its last client is closed."""
        import psycopg

        with psycopg.connect(
            self.config("postgres").for_role("owner").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _epoch(value: Any) -> datetime:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = 0
    return datetime.fromtimestamp(max(seconds, 0), UTC)


def _seed_columns(seed: Any) -> dict[int, str]:
    columns = getattr(seed, "columns", None)
    if not isinstance(columns, list):
        columns = seed.call("getColumns", project_id=7)
    return {int(column["id"]): str(column["title"]) for column in columns}


def _seed_lanes(seed: Any) -> dict[int, str]:
    lanes = getattr(seed, "lanes", None)
    if not isinstance(lanes, list):
        lanes = seed.call("getActiveSwimlanes", project_id=7)
    return {int(lane["id"]): str(lane["name"]) for lane in lanes}


def _seed_comments(seed: Any, identifier: int) -> list[dict[str, Any]]:
    comments = getattr(seed, "comments", None)
    if isinstance(comments, dict):
        return list(comments.get(identifier) or [])
    return list(seed.call("getAllComments", task_id=identifier) or [])


def seed_client(
    config: BoardStoreConfig,
    seed: Any,
    instance_dir: Path | str,
    *,
    client_class: type[SqlCardClient] = SqlCardClient,
) -> SqlCardClient:
    """Write a starting board into a migrated store and return a client over it.

    `seed` carries rows in the legacy board shape the importer reads (`tests/fakes/tasks.py`
    `CardSeed`; the Sprint and Product/Issue fixtures of `tests/fakes/sprints.py` too): `tasks`
    with `column_id`, `swimlane_id` and `date_creation`, a `metadata` map and the comments. A card
    keeps its row id as its `board_key`, so `task_postgres_<id>` and every `task_id=<id>` call a test
    makes name the card it seeded, and the key sequence continues after the highest one (or at
    `seed.next_key`). Metadata keys the model names become columns, the rest become
    `tasks.extensions.extra`, and the swimlane stays in that same bag.  Everything but the card
    row itself is the product's own write path — `createTask`, `saveTaskMetadata` — rather than
    direct SQL, so a seeding that disagrees with the client is a failure of the client rather than
    a divergence nobody sees.
    """
    client = client_class(config.for_role("owner"), instance_dir)
    columns = _seed_columns(seed)
    lanes = _seed_lanes(seed)
    projects = {str(meta.get("project")) for meta in seed.metadata.values() if str(meta.get("project") or "")}
    with client.transaction():
        for project in sorted(projects):
            client._execute(
                "INSERT INTO projects (project_id, enabled, registry_present) VALUES (%s, true, true) "
                "ON CONFLICT (project_id) DO NOTHING",
                (project,),
            )
        for name in sorted(set(lanes.values())):
            client._lane_names().append(name) if name not in client._lane_names() else None
        client._lanes = sorted(set(client._lanes or []))
        public_numbers = {}
        transport_keys = {}
        for row in seed.tasks:
            reference = str(row["reference"])
            kind = reference.split(":", 1)[0] if ":" in reference else ""
            public_numbers[int(row["id"])] = (
                record_key(kind, reference.split(":", 1)[1])
                if kind in {"product", "issue"}
                else _task_number_of(reference)
            )
        for row in seed.tasks:
            number = public_numbers[int(row["id"])]
            reference = str(row["reference"])
            meta = dict(seed.metadata.get(int(row["id"]), {}))
            if meta.get("record_type") in {"product", "issue"}:
                transport_keys[int(row["id"])] = client.call(
                    "createTask",
                    project_id=1,
                    title=str(row.get("title") or reference),
                    description=str(row.get("description") or ""),
                    column_id=1,
                    swimlane_id=0,
                    reference=reference,
                )
                client.call("saveTaskMetadata", task_id=number, values=meta)
                if int(row.get("is_active", 1) or 0) == 0:
                    client.call("closeTask", task_id=number)
                continue
            transport_keys[int(row["id"])] = _seed_card_row(
                client, row, meta.get("project") or None, columns=columns, lanes=lanes
            )
        next_key = getattr(seed, "next_key", None)
        if next_key is not None and int(next_key) > 1:
            client._execute(
                "SELECT setval('card_board_key_seq', GREATEST(%s, (SELECT last_value FROM "
                "card_board_key_seq)), true)",
                (int(next_key) - 1,),
            )
        for identifier, meta in seed.metadata.items():
            if (
                meta
                and int(identifier) in transport_keys
                and meta.get("record_type") not in {"product", "issue"}
            ):
                ensure_sprint_row(client, meta.get("sprint_ref"))
                client.call("saveTaskMetadata", task_id=transport_keys[int(identifier)], values=dict(meta))
        for identifier in sorted(public_numbers):
            for comment in _seed_comments(seed, identifier):
                insert_comment_row(
                    client,
                    transport_keys[int(identifier)],
                    str(comment.get("comment") or ""),
                    _epoch(comment.get("date_creation")),
                )
    return client


def insert_card_row(
    client: SqlCardClient,
    *,
    key: int,
    reference: str,
    state: str,
    project: str | None,
    title: str = "",
    description: str = "",
    closed: bool = False,
    position: Any = 0,
    created: datetime,
    updated: datetime,
    moved: datetime | None = None,
    lane: str | None = None,
    bag: dict[str, str] | None = None,
) -> int:
    """One card row, under `key` as its `board_key`, and the project row it refers to.

    `bag` goes into the row's extension bag as it stands, an empty value included, as production's
    store holds values its PostgreSQL cutover took over; `saveTaskMetadata` would clear an empty one.
    """
    try:
        position_value = max(int(position), 0)
    except (TypeError, ValueError):
        position_value = 0
    extra = {**(bag or {}), **({"swimlane": lane} if lane else {})}
    if project:
        client._execute(
            "INSERT INTO projects (project_id, enabled, registry_present) VALUES (%s, true, true) "
            "ON CONFLICT (project_id) DO NOTHING",
            (project,),
        )
    inserted = client._query(
        "INSERT INTO tasks (board_key, task_ref, task_number, title, description, state, archived, "
        "position, project_id, created_at, updated_at, date_moved, extensions) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "RETURNING board_key",
        (
            int(key),
            reference,
            _task_number_of(reference),
            title or reference,
            description,
            state,
            closed,
            position_value,
            project,
            created,
            updated,
            moved,
            json.dumps({"extra": extra} if extra else {}),
        ),
    )
    client._execute(
        "SELECT setval('card_board_key_seq', GREATEST(%s, (SELECT last_value FROM card_board_key_seq)), true)",
        (int(key),),
    )
    return int(inserted[0][0])


def _seed_card_row(
    client: SqlCardClient,
    row: dict[str, Any],
    project: str | None,
    *,
    columns: dict[int, str],
    lanes: dict[int, str],
) -> int:
    """A seed row in the legacy board shape, written as the card row it describes."""
    from ummanu.tasks import _STATE_BY_COLUMN

    moved = row.get("date_moved")
    return insert_card_row(
        client,
        key=int(row["id"]),
        reference=str(row["reference"]),
        state=_STATE_BY_COLUMN[columns.get(int(row.get("column_id") or 0), "Issues")],
        project=project,
        title=str(row.get("title") or ""),
        description=str(row.get("description") or ""),
        closed=int(row.get("is_active", row.get("status", 1)) or 0) == 0,
        position=row.get("position"),
        created=_epoch(row.get("date_creation")),
        updated=_epoch(row.get("date_modification") or row.get("date_creation")),
        moved=_epoch(moved) if moved is not None else None,
        lane=lanes.get(int(row.get("swimlane_id") or 0)),
    )


def insert_comment_row(client: SqlCardClient, key: int, body: str, created: datetime) -> None:
    """A comment at the board's own creation time, not the seeding's.

    A comment's timestamp is part of what the reader returns, so a fixture that stamped `now()`
    would be asking about a different comment than the one it arranged.
    """
    first = body.splitlines()[0] if body else ""
    marker = first[1:-1] if first.startswith("[") and first.endswith("]") else None
    client._execute(
        "INSERT INTO task_comments (task_ref, marker, body, created_at) "
        "VALUES ((SELECT task_ref FROM tasks WHERE board_key = %s), %s, %s, %s)",
        (key, marker, body, created),
    )


def ensure_sprint_row(client: SqlCardClient, reference: Any, *, status: str = "open") -> None:
    """The row a card's `sprint_ref` refers to (§3.3), when the test has not created the sprint.

    A card linked to a sprint the store has never heard of cannot be written: `tasks.sprint_ref` is
    a foreign key. A test about the card rather than the sprint links it to this minimal row, and a
    test about the sprint creates the sprint through `SprintWriter` instead.
    """
    text = str(reference or "").strip()
    if not text:
        return
    now = datetime.now(UTC)
    client._execute(
        "INSERT INTO sprints (ref, board_key, sprint_number, goal, definition_of_done, status, "
        "created_at, updated_at, closed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (ref) DO NOTHING",
        (
            text,
            record_key("sprint", text),
            sprint_reference_number(text),
            "a goal",
            "a definition",
            status,
            now,
            now,
            None if status == "open" else now,
        ),
    )


class CardStoreClient(SqlCardClient):
    """The product's card client over a test's own store, and the arranging a test does on it.

    `calls` and `batch_calls` record what the code under test asked the board, in order; the
    arranging verbs below go through the same vocabulary without being recorded, so an assertion
    about the calls a read made is not about the fixture's own writes.
    """

    def __init__(self, credentials: Any, instance_dir: Path | str) -> None:
        super().__init__(credentials, instance_dir)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batch_calls: list[list[tuple[str, dict[str, Any]]]] = []

    def call(self, method: str, **params: Any) -> Any:
        self.calls.append((method, params))
        return super().call(method, **params)

    def call_batch(self, calls: Any) -> list[Any]:
        batch = list(calls)
        self.batch_calls.append(batch)
        return super().call_batch(batch)

    def _arrange(self, method: str, **params: Any) -> Any:
        return SqlCardClient.call(self, method, **params)

    # --- arranging -------------------------------------------------------------------

    def metadata(self, key: int) -> dict[str, str]:
        """The card's metadata as the board answers it."""
        return dict(self._arrange("getTaskMetadata", task_id=key))

    def save_metadata(self, key: int, values: dict[str, Any] | None = None, **named: Any) -> None:
        """Write metadata onto a card, linking any sprint it names to a row that exists."""
        document = {**(values or {}), **named}
        with self.transaction():
            ensure_sprint_row(self, document.get("sprint_ref"))
            self._arrange("saveTaskMetadata", task_id=key, values=document)

    def move(self, key: int, state: str, *, position: int = 1) -> None:
        """Put a card into another column, as a writer's move would."""
        from ummanu.board.sql_cards import _COLUMN_ID_BY_STATE

        self._arrange(
            "moveTaskPosition",
            project_id=1,
            task_id=key,
            column_id=_COLUMN_ID_BY_STATE[state],
            position=position,
            swimlane_id=0,
        )

    def update(self, key: int, **fields: Any) -> None:
        """Change a card's reference, title or description."""
        self._arrange("updateTask", id=key, **fields)

    def set_moved(self, key: int, moved: int | None) -> None:
        """When the card entered its column (`tasks.date_moved`), which no board verb sets."""
        with self.transaction():
            self._execute(
                "UPDATE tasks SET date_moved = %s WHERE board_key = %s",
                (_epoch(moved) if moved is not None else None, int(key)),
            )

    def comments(self, key: int) -> list[dict[str, Any]]:
        return list(self._arrange("getAllComments", task_id=key))

    def replace_comments(self, key: int, comments: list[dict[str, Any]]) -> None:
        """The card's comments are exactly these (`comment` and `date_creation`), in this order."""
        with self.transaction():
            self._execute(
                "DELETE FROM task_comments WHERE task_ref = (SELECT task_ref FROM tasks WHERE "
                "board_key = %s)",
                (int(key),),
            )
            for comment in comments:
                created = comment.get("date_creation")
                insert_comment_row(
                    self,
                    key,
                    str(comment.get("comment") or ""),
                    _epoch(created) if created is not None else datetime.now(UTC),
                )

    def remove_comments(self, key: int, containing: str) -> None:
        """Take the comments whose body contains `containing` off a card, as if never written."""
        with self.transaction():
            self._execute(
                "DELETE FROM task_comments WHERE task_ref = (SELECT task_ref FROM tasks WHERE "
                "board_key = %s) AND strpos(body, %s) > 0",
                (int(key), containing),
            )

    def add_comment(self, key: int, body: str, *, created: int | None = None) -> None:
        with self.transaction():
            insert_comment_row(self, key, body, _epoch(created) if created is not None else datetime.now(UTC))

    def row(self, key: int) -> dict[str, Any]:
        """The card row the board answers for this key, live or archived."""
        rows = self._rows("board_key = %s", (int(key),))
        if not rows:
            raise KeyError(key)
        return rows[0]

    def link_card(self, key: int, sprint: str) -> None:
        """Link a card to another sprint, taking it off as any sprint's current task first."""
        with self.transaction():
            self._execute(
                "UPDATE sprints SET current_task_ref = NULL WHERE current_task_ref = "
                "(SELECT task_ref FROM tasks WHERE board_key = %s)",
                (int(key),),
            )
            self._arrange("saveTaskMetadata", task_id=key, values={"sprint_ref": sprint})

    def next_key(self) -> int:
        """A card transport key no card of this store carries."""
        return int(self._query("SELECT coalesce(max(board_key), 0) + 1 FROM tasks")[0][0])

    def key_of(self, reference: str) -> int:
        """The transport key of the card this reference names."""
        rows = self._query("SELECT board_key FROM tasks WHERE task_ref = %s", (reference,))
        if not rows:
            raise KeyError(reference)
        return int(rows[0][0])

    def card_count(self) -> int:
        """How many cards the store holds, live and archived."""
        return int(self._query("SELECT count(*) FROM tasks")[0][0])

    def state(self, key: int) -> str:
        from ummanu.board.sql_cards import _STATE_BY_COLUMN_ID

        return _STATE_BY_COLUMN_ID[int(self.row(key)["column_id"])]

    def add_card(
        self,
        key: int,
        reference: str,
        *,
        project: str | None = "ummanu",
        state: str = "ready",
        title: str | None = None,
        description: str = "",
        position: int | None = None,
        metadata: dict[str, Any] | None = None,
        closed: bool = False,
        created: int = 1720000000,
        moved: int | None = None,
        lane: str | None = "Ummanu",
    ) -> None:
        """One card row, under `key`, with its metadata written through the client."""
        with self.transaction():
            insert_card_row(
                self,
                key=key,
                reference=reference,
                state=state,
                project=project,
                title=title or reference,
                description=description,
                closed=closed,
                position=key if position is None else position,
                created=_epoch(created),
                updated=_epoch(created),
                moved=_epoch(moved) if moved is not None else None,
                lane=lane,
            )
            self._lanes = None
            values = {**({"project": project} if project else {}), **(metadata or {})}
            if values:
                ensure_sprint_row(self, values.get("sprint_ref"))
                self._arrange("saveTaskMetadata", task_id=key, values=values)

    def add_sprint(self, reference: str, *, status: str = "open", **metadata: Any) -> dict[str, Any]:
        """A sprint row through the client's own Sprint vocabulary, or new values on an existing one.

        The defaults are the ones every sprint needs to exist at all; a test states the rest.
        """
        from ummanu.board.sql_cards import SPRINT_BOARD_ID

        key = record_key("sprint", reference)
        values = {key_: str(value) for key_, value in metadata.items()}
        with self.transaction():
            if self._query("SELECT 1 FROM sprints WHERE ref = %s", (reference,)):
                self._arrange("saveTaskMetadata", task_id=key, values={"sprint_status": status, **values})
            else:
                created = self._arrange(
                    "createTask",
                    project_id=SPRINT_BOARD_ID,
                    title=str(metadata.get("sprint_goal", "sprint")),
                    description="",
                    reference=reference,
                )
                self._arrange(
                    "saveTaskMetadata",
                    task_id=created,
                    values={
                        "sprint_goal": "ship the thing",
                        "sprint_definition_of_done": "the thing ships",
                        "sprint_repositories": '["ummanu"]',
                        "sprint_status": status,
                        **values,
                    },
                )
                # A resume is its own row (`sprint_resumes`), written only onto a sprint that exists.
                if "sprint_resume" in values:
                    self._arrange(
                        "saveTaskMetadata", task_id=created, values={"sprint_resume": values["sprint_resume"]}
                    )
        return {"id": key, "reference": reference}

    def sprint_metadata(self, reference: str) -> dict[str, str]:
        return dict(self._arrange("getTaskMetadata", task_id=record_key("sprint", reference)))

    def save_sprint_metadata(self, reference: str, **values: Any) -> None:
        self._arrange(
            "saveTaskMetadata",
            task_id=record_key("sprint", reference),
            values={key: str(value) for key, value in values.items()},
        )

    def add_record(
        self, reference: str, title: str, metadata: dict[str, Any], *, closed: bool = False
    ) -> int:
        """A Product or an Issue, through the same vocabulary a Product/Issue writer uses."""
        with self.transaction():
            created = self._arrange(
                "createTask",
                project_id=1,
                title=title,
                description="",
                column_id=1,
                swimlane_id=0,
                reference=reference,
            )
            self._arrange("saveTaskMetadata", task_id=created, values=dict(metadata))
            if closed:
                self._arrange("closeTask", task_id=created)
        return int(created)

    def clear_sprints(self) -> None:
        """No sprint at all: every sprint row goes, and no card is linked to one any more."""
        with self.transaction():
            self._execute("UPDATE tasks SET sprint_ref = NULL WHERE sprint_ref IS NOT NULL")
            self._execute("DELETE FROM sprints")

    def ensure_sprint(self, reference: str, *, status: str = "open") -> None:
        with self.transaction():
            ensure_sprint_row(self, reference, status=status)

    def remove_card(self, reference: str) -> None:
        """The one arranging verb the card protocol does not carry: a card that is gone."""
        with self.transaction():
            self._execute("DELETE FROM tasks WHERE task_ref = %s", (reference,))


def card_store(
    test: unittest.TestCase,
    seed: Any = None,
    *,
    instance_dir: Path | str | None = None,
    client_class: type[SqlCardClient] = CardStoreClient,
) -> Any:
    """A seeded card store of this test's own, closed and dropped when the test ends."""
    from tests.fakes.tasks import CardSeed

    board = PostgresBoard.shared()
    if instance_dir is None:
        scratch = tempfile.TemporaryDirectory()
        test.addCleanup(scratch.cleanup)
        instance_dir = scratch.name
    config = board.fresh_database()
    client = seed_client(
        config, seed if seed is not None else CardSeed(), instance_dir, client_class=client_class
    )
    # The seeding went through the client's own vocabulary; a test's call log starts after it.
    for log in ("calls", "batch_calls"):
        if isinstance(getattr(client, log, None), list):
            getattr(client, log).clear()

    def dispose() -> None:
        client.close()
        board.release_database(config.dbname)

    test.addCleanup(dispose)
    return client


def terminate_session(client: SqlCardClient) -> None:
    """End the client's server session from a second connection, as a board-store restart would.

    The shared test server keeps running; only this client's backend goes away.  The session is
    the connection this thread would use next: the pinned one inside a session or transaction,
    else the one the pool hands out next.  Returns once the backend has exited and its hang-up has
    reached the client's socket, so what the client does next does not race the server.
    """
    import select

    import psycopg

    connection = client.connection
    pid = connection.info.backend_pid
    with psycopg.connect(client.credentials.conninfo(), autocommit=True) as admin:
        row = admin.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,)).fetchone()
    if not row or not row[0]:
        raise AssertionError(f"backend {pid} was not terminated")
    poller = select.poll()
    poller.register(connection.fileno(), select.POLLIN)
    if not poller.poll(5000):
        raise AssertionError(f"the hang-up of backend {pid} never reached the client")


class CardStoreCase(unittest.TestCase):
    """A TestCase whose board is a real card store: `self.card_store(seed)`."""

    def card_store(self, seed: Any = None, **options: Any) -> Any:
        return card_store(self, seed, **options)


__all__ = [
    "IMAGE",
    "CardStoreCase",
    "CardStoreClient",
    "PostgresBoard",
    "card_store",
    "docker",
    "ensure_sprint_row",
    "seed_client",
    "terminate_session",
]
