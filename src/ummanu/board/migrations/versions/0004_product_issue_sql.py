"""Indexed Product/Issue board identity, lossless Product metadata, comments and move time."""

from __future__ import annotations

import hashlib
import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ummanu.board.schema import APP_ROLE, READ_ROLE

revision = "0004_product_issue_sql"
down_revision = "0003_task_type_optional"
branch_labels = None
depends_on = None

# frozen copy of record_key as of 8c1ea4cd (ummanu.board.backend): a released revision keeps
# the key formula it backfilled with, whatever the live function later becomes.
_RECORD_KINDS = ("sprint", "product", "issue")
_RECORD_KEY_BASES = {"sprint": 2_000_000_000, "product": 3_000_000_000, "issue": 4_000_000_000}
_RECORD_KEY_SPAN = 1_000_000_000
_SPRINT_NUMBER_KEY_SPAN = _RECORD_KEY_SPAN // 2
_ASCII_NUMBERED_SPRINT_REF = re.compile(r"^sprint:([0-9]+)$")


def record_key(kind: str, identifier: str) -> int:
    """frozen copy of record_key as of 8c1ea4cd: the transport key this revision backfills."""
    if kind not in _RECORD_KINDS:
        raise ValueError(f"a board record names one of {', '.join(_RECORD_KINDS)}, not {kind!r}")
    text = str(identifier).strip()
    if not text:
        raise ValueError(f"a {kind} key needs an identifier")
    match = _ASCII_NUMBERED_SPRINT_REF.fullmatch(text) if kind == "sprint" else None
    if match is not None:
        number = int(match.group(1))
        if text != f"sprint:{number}":
            raise ValueError(f"a numbered Sprint reference must be canonical, not {text!r}")
        if number >= _SPRINT_NUMBER_KEY_SPAN:
            raise ValueError(f"a numbered Sprint must be below {_SPRINT_NUMBER_KEY_SPAN}, not {number}")
        return _RECORD_KEY_BASES[kind] + number
    digest = hashlib.sha256(f"{kind}:{text}".encode()).digest()
    offset = _SPRINT_NUMBER_KEY_SPAN if kind == "sprint" else 0
    span = _SPRINT_NUMBER_KEY_SPAN if kind == "sprint" else _RECORD_KEY_SPAN
    return _RECORD_KEY_BASES[kind] + offset + int.from_bytes(digest[:8], "big") % span


def _populate_keys(table: str, identity: str, kind: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text(f"SELECT {identity} FROM {table}")).scalars().all()
    keys: dict[int, str] = {}
    for value in rows:
        key = record_key(kind, str(value))
        previous = keys.get(key)
        if previous is not None:
            raise RuntimeError(
                f"{kind} board-key collision between {previous!r} and {value!r}: {key}"
            )
        keys[key] = str(value)
        connection.execute(
            sa.text(f"UPDATE {table} SET board_key = :key WHERE {identity} = :identity"),
            {"key": key, "identity": value},
        )


def upgrade() -> None:
    op.add_column("products", sa.Column("board_key", sa.BigInteger(), nullable=True))
    op.add_column(
        "products",
        sa.Column(
            "extensions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("issues", sa.Column("board_key", sa.BigInteger(), nullable=True))
    _populate_keys("products", "product_id", "product")
    _populate_keys("issues", "issue_id", "issue")
    op.alter_column("products", "board_key", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("issues", "board_key", existing_type=sa.BigInteger(), nullable=False)
    op.create_unique_constraint("products_board_key_key", "products", ["board_key"])
    op.create_unique_constraint("issues_board_key_key", "issues", ["board_key"])

    op.create_table(
        "product_comments",
        sa.Column("comment_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "product_id",
            sa.Text(),
            sa.ForeignKey("products.product_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("marker", sa.Text()),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("actor_role", sa.Text()),
        sa.Column("actor_id", sa.Text()),
        sa.Column("request_id", sa.Text()),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("product_ref", sa.Text(), sa.Computed("'product:' || product_id", persisted=True)),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("product_comments_by_product", "product_comments", ["product_id", "created_at"])
    op.create_foreign_key(
        "product_comment_claims_its_request",
        "product_comments",
        "requests",
        ["request_id", "product_ref"],
        ["request_id", "ref"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.add_column(
        "tasks",
        sa.Column("date_moved", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )

    # Keep the late table usable even when a deployment's default privileges were altered.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON product_comments TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE product_comments_comment_id_seq TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON product_comments TO {READ_ROLE}")


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
