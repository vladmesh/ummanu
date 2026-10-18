"""Allow the PO importer to advance preserved feed identities without granting table ownership."""

from alembic import op

from ummanu.board.schema import APP_ROLE

revision = "0034_po_restore_sequence"
down_revision = "0033_po_session_moved"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"GRANT SELECT, UPDATE ON SEQUENCE po_feed_entry_id_seq TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, UPDATE ON SEQUENCE po_feed_entry_id_seq FROM {APP_ROLE}")
