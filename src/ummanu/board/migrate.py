"""Applying the board store's schema: Alembic, under the §7.4 advisory lock.

The schema is SQLAlchemy models and the migrations are Alembic's (``docs/BOARD_STORE.md`` §7.4).
This module adds what Alembic does not do by itself:

* **the lock.**  ``pg_advisory_lock`` on a fixed key, taken on the *same session* the migrations
  run on and released once at the end, so two upgrades racing on one installation serialize
  instead of both migrating.  Alembic has no opinion about concurrent runners.
* **the connection.**  §5.4's ``board-store.env``, resolved per §5.5 role, never an
  ``sqlalchemy.url`` literal in an ini file — and therefore behind the git-exclusion enforcement
  `board_store.resolve` performs.
* **the two generated passwords.**  §5.5's ``CREATE ROLE`` statements take them as parameters of
  the run, handed to the revision through ``config.attributes``; they are not bytes of any
  revision file.
* **the release's target bundle.**  The dispatcher of the running build migrates the board to the
  graph of the commit it is about to activate (`board.release_migrations`), so `apply` takes the
  script directory to read instead of assuming the package's own, and an ``admit`` hook that refuses
  a pending revision before any of them runs.
* **`upgrade.py`'s three outcomes.**  Unconfigured is a no-op, configured and current is
  unchanged, configured and broken is a failure carrying its reason — every one of them reaching
  the caller as a single `BoardStoreError`.

What it no longer does: version numbers, checksums and a rule about editing an applied file.
Alembic's ``alembic_version`` table is the version, and inventing bookkeeping on top of it is
exactly what the owner refused.

``sqlalchemy`` and ``alembic`` are imported inside the functions that need them, for the reason
``psycopg`` was: the upgrade that installs the dependencies has to be able to start on a venv
that does not have them yet.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.board import store as board_store
from ummanu.board.store import BoardStoreError

#: Alembic's script directory, shipped inside the package.
SCRIPT_LOCATION = Path(__file__).resolve().parent / "migrations"

#: The revision this build of the product expects a store to be at.  It is Alembic's head, which
#: `head_revision()` reads from the script directory; the schema gate (`board.schema_gate`) compares
#: against this literal so that a healthy read never imports Alembic, and `tests/test_board_store.py`
#: holds the two equal.
#: `0028_owner_turns` is the head: PO sessions, turns and feed, the one record of /po
#: request ids, who closed a session and when, the card kinds with their review choice and
#: live-impact flag, the indexes the audit's narrowed reads are served by, the budget pass's
#: candidate index, the extension bag under its neutral key, a PO session's reasoning effort
#: with the model each of its turns resolved to, a sprint's PO session and allowed productions,
#: the PO-executed card kinds `decision` and `operation`, the owner events behind the bell, a PO
#: session's title, the headless card kind `wait`, the owner event kind of a delegated card's
#: returned result, the outbox of the returns a delegated card owes, and a sprint's e2e run budget
#: with the bell kind of a spent per-card e2e cap, and the bell kind of an after-merge e2e run that
#: returns a notice, creation-only sprint local-run exceptions, quoted standing owner decisions,
#: and explicit owner-turn attention with routine legacy notices reclassified.
EXPECTED_SCHEMA_REVISION = "0028_owner_turns"

#: A fixed 64-bit key, so every runner of every checkout contends on the same lock.  Any constant
#: would do; this one is the first 63 bits of sha256("ummanu.board.migrations"), recorded here
#: as a literal rather than recomputed, because a key that changes with a hashing detail is not a
#: fixed key.
ADVISORY_LOCK_KEY = 0x2C5B1F4A6E9D0713


class MigrationFailed(BoardStoreError):
    """One revision failed. It names the revision, what the run had committed before it, and the
    cause as the server raised it (also the exception's ``__cause__``).

    Revisions apply one at a time, each committed before the next starts, so a failure leaves the
    store at the last revision in `applied` (or where it started, when that is empty) and the
    failing one's statements rolled back.
    """

    def __init__(
        self, revision: str, applied: tuple[str, ...], cause: BaseException, owed: tuple[str, ...] = ()
    ) -> None:
        self.revision = revision
        self.applied = applied
        self.owed = owed
        self.cause = _first_line(cause)
        super().__init__(
            f"the board store did not accept the migration run: revision {revision} failed: {self.cause}"
        )


class MigrationRefused(BoardStoreError):
    """An ``admit`` hook refused a pending revision; nothing was applied."""

    def __init__(self, revision: str, reason: str, message: str, owed: tuple[str, ...] = ()) -> None:
        self.revision = revision
        self.reason = reason
        self.owed = owed
        super().__init__(message)


def _first_line(exc: BaseException) -> str:
    """The first line of an exception's text: psycopg appends context lines after it."""
    text = str(getattr(exc, "orig", None) or exc).strip()
    return (text.splitlines() or [type(exc).__name__])[0]


def sqlalchemy_url(credentials: Any) -> Any:
    """A SQLAlchemy URL for one §5.5 role, over the `psycopg` driver (§5.8).

    Built with ``URL.create`` rather than by formatting a string, so a generated password
    containing ``@``, ``/`` or ``:`` produces one URL and not a truncated one.
    """
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+psycopg",
        username=credentials.user,
        password=credentials.password,
        host=credentials.host,
        port=credentials.port,
        database=credentials.dbname,
    )


def alembic_config(
    *,
    connection: Any = None,
    passwords: dict[str, str] | None = None,
    reuse_existing_roles: bool = False,
    script_location: Path | str | None = None,
) -> Any:
    """Alembic's `Config`, built in code and carrying no connection string of its own.

    There is no ``alembic.ini`` in this product on purpose: the only URL an installation has is
    §5.4's, and a second place to write one is a second authority.  The connection and the two
    generated passwords travel in ``attributes``, which is Alembic's supported channel for
    exactly this. `script_location` is the package's own migrations unless a caller names another
    bundle (the release's target, `board.release_migrations`).
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(script_location or SCRIPT_LOCATION))
    if connection is not None:
        config.attributes["connection"] = connection
    if passwords is not None:
        config.attributes["passwords"] = dict(passwords)
    if reuse_existing_roles:
        config.attributes["reuse_existing_roles"] = True
    return config


def script_directory(script_location: Path | str | None = None) -> Any:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(script_location=script_location))


def head_revision() -> str:
    """The revision the shipped script directory ends at."""
    head = script_directory().get_current_head()
    if head is None:
        raise BoardStoreError("the board store ships no migrations at all")
    return head


def current_revision(connection: Any) -> str | None:
    """What the database says it is at, or ``None`` for a database Alembic has never touched."""
    from alembic.migration import MigrationContext

    return MigrationContext.configure(connection).get_current_revision()


def pending(connection: Any, script: Any = None) -> tuple[str, ...]:
    """The revisions this database still owes to `script` (the package's own by default), oldest first."""
    script = script if script is not None else script_directory()
    current = current_revision(connection)
    owed = [revision.revision for revision in script.iterate_revisions("heads", current)]
    owed.reverse()
    return tuple(owed)


def lineage(target: str | None = None) -> tuple[str, ...]:
    """Every revision from the base to `target` (the shipped head by default), in application order.

    The graph is linear, so an installation at one of these revisions owes exactly the ones after it,
    and one at none of them is at a schema this build's graph does not know (`board.schema_gate`).
    """
    script = script_directory()
    walked = [revision.revision for revision in script.iterate_revisions(target or head_revision(), "base")]
    walked.reverse()
    return tuple(walked)


def apply(
    connection: Any,
    *,
    passwords: dict[str, str],
    dry_run: bool = False,
    reuse_existing_roles: bool = False,
    script_location: Path | str | None = None,
    admit: Callable[[Any, tuple[str, ...]], None] | None = None,
) -> tuple[str, ...]:
    """Upgrade one owner connection to head under the advisory lock.  Returns what it applied.

    The lock is session-level, so it spans the per-revision transactions and is released once at
    the end whatever happened in between.  A dry run takes the same lock and reads the same version
    table, and returns what it *would* apply without running a revision.

    The owed revisions are read under the lock, so a second runner that waited finds them applied
    and applies nothing. `admit(script, owed)` is asked next, before any revision runs, and refuses
    by raising (`MigrationRefused`). The revisions then run one at a time, oldest first, each
    committed before the next: a failure raises `MigrationFailed` naming it.

    Whatever fails, the session's transaction is rolled back *before* the unlock, so the unlock is
    not refused by an aborted transaction and the caller sees the failure that happened, not
    "current transaction is aborted". If the rollback or the unlock itself fails after a failure,
    the connection is invalidated (its session, and with it the lock, end when it closes) and the
    original failure is the one raised.
    """
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    locked = False
    failed = False
    try:
        connection.exec_driver_sql("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        connection.commit()
        locked = True
        config = alembic_config(
            connection=connection,
            passwords=passwords,
            reuse_existing_roles=reuse_existing_roles,
            script_location=script_location,
        )
        script = ScriptDirectory.from_config(config)
        owed = pending(connection, script)
        connection.commit()
        if dry_run or not owed:
            return owed
        if admit is not None:
            admit(script, owed)
        applied: list[str] = []
        for revision in owed:
            try:
                # `command.upgrade`, one revision at a time over one script directory, so a failure
                # is known by name and every revision before it is committed.
                with EnvironmentContext(
                    config,
                    script,
                    fn=lambda rev, _context, target=revision: script._upgrade_revs(target, rev),
                    destination_rev=revision,
                ):
                    script.run_env()
                connection.commit()
            except Exception as exc:
                raise MigrationFailed(revision, tuple(applied), exc, owed) from exc
            applied.append(revision)
        return owed
    except BaseException:
        failed = True
        raise
    finally:
        _end_run(connection, locked=locked, failed=failed)


def _end_run(connection: Any, *, locked: bool, failed: bool) -> None:
    """Roll back, then unlock; after a failure, never let the cleanup replace it."""
    try:
        connection.rollback()
        if locked:
            connection.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            connection.commit()
    except Exception:
        if not failed:
            raise
        # The run already failed and that failure is what the caller must see. A session whose
        # cleanup failed is not reused: invalidating it closes it, which ends the lock with it.
        with contextlib.suppress(Exception):
            connection.invalidate()


def passwords_for(config: Any) -> dict[str, str]:
    """§5.5's two generated passwords, as the revision's parameters."""
    return {"app_password": config.app_password, "read_password": config.read_password}


def migrate_instance(
    instance_dir: Path | str,
    *,
    dry_run: bool = False,
    reuse_existing_roles: bool = False,
) -> tuple[str, ...]:
    """Bring one installation's configured board store to the schema this build ships.

    Returns the revisions applied, or — under ``dry_run`` — the revisions that *would* be applied,
    having connected and read but written nothing.

    Every failure leaves as one `BoardStoreError` carrying its reason: a `board-store.env` the
    instance repository tracks, an unparsable one, absent dependencies, a server that will not
    answer, a refused login and a revision that fails all reach the caller in the same shape.
    That is what lets `upgrade.py`'s step report a reason without importing SQLAlchemy — which
    matters, because the same upgrade that installs the dependencies has to start without them.
    """
    config = board_store.resolve(instance_dir)
    try:
        import sqlalchemy as sa
    except ImportError as exc:
        raise BoardStoreError(
            "the board store is configured but SQLAlchemy is not installed; reinstall the "
            "product dependencies before migrating"
        ) from exc
    try:
        import alembic  # noqa: F401
    except ImportError as exc:
        raise BoardStoreError(
            "the board store is configured but Alembic is not installed; reinstall the "
            "product dependencies before migrating"
        ) from exc
    engine = sa.create_engine(sqlalchemy_url(config.for_role("owner")))
    try:
        with engine.connect() as connection:
            return apply(
                connection,
                passwords=passwords_for(config),
                dry_run=dry_run,
                reuse_existing_roles=reuse_existing_roles,
            )
    except sa.exc.SQLAlchemyError as exc:
        raise BoardStoreError(f"the board store did not accept the migration run: {exc}") from exc
    finally:
        engine.dispose()


__all__ = [
    "ADVISORY_LOCK_KEY",
    "EXPECTED_SCHEMA_REVISION",
    "SCRIPT_LOCATION",
    "MigrationFailed",
    "MigrationRefused",
    "alembic_config",
    "apply",
    "current_revision",
    "head_revision",
    "lineage",
    "migrate_instance",
    "passwords_for",
    "pending",
    "script_directory",
    "sqlalchemy_url",
]
