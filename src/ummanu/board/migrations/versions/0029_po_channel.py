"""Typed PO wait on future resumes; released six-field resumes remain untouched.

PO execution assignments and run dispositions use the existing task extension bag.
There is no backfill of fabricated origins, sessions, requests or owner authority.
The native completion's optional E2E disposition section remains comment/event
evidence; optional run receipts and live holders remain inside the existing e2e
JSON text. This migration leaves every released bag and its audit unchanged.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029_po_channel"
down_revision = "0028_owner_turns"
branch_labels = None
depends_on = None
release_safety = "additive"


def upgrade() -> None:
    op.add_column("sprint_resumes", sa.Column("po_request", JSONB(), nullable=True))
    op.create_check_constraint("sprint_resume_po_request_is_object", "sprint_resumes",
                               "po_request IS NULL OR jsonb_typeof(po_request) = 'object'")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM sprint_resumes WHERE po_request IS NOT NULL LIMIT 1").first():
        raise RuntimeError("PO wait records exist; downgrade would lose their owning card")
    op.drop_constraint("sprint_resume_po_request_is_object", "sprint_resumes", type_="check")
    op.drop_column("sprint_resumes", "po_request")
