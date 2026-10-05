"""Explicit service input display and per-session sprint comment position.

Released feed text is unchanged, with NULL metadata and expanded readback.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0030_po_input_context"
down_revision = "0029_po_channel"
branch_labels = None
depends_on = None
release_safety = "additive"


def upgrade() -> None:
    op.add_column("po_feed", sa.Column("metadata", JSONB(), nullable=True))
    op.create_check_constraint("po_feed_metadata_is_object", "po_feed",
                               "metadata IS NULL OR jsonb_typeof(metadata) = 'object'")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM po_feed WHERE metadata IS NOT NULL LIMIT 1").first():
        raise RuntimeError("PO input metadata exists; downgrade would lose delivery positions")
    op.drop_constraint("po_feed_metadata_is_object", "po_feed", type_="check")
    op.drop_column("po_feed", "metadata")
