"""Addressable Definition of Done items on a sprint, NULL for every existing sprint.

An existing sprint keeps its free-text DoD and reads as unstructured; no item is parsed out of it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031_sprint_dod_items"
down_revision = "0030_po_input_context"
branch_labels = None
depends_on = None
release_safety = "additive"


def upgrade() -> None:
    op.add_column("sprints", sa.Column("dod_items", JSONB(), nullable=True))
    op.create_check_constraint(
        "sprint_dod_items_are_array", "sprints", "dod_items IS NULL OR jsonb_typeof(dod_items) = 'array'"
    )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM sprints WHERE dod_items IS NOT NULL LIMIT 1").first():
        raise RuntimeError("a sprint has Definition of Done items; 0031 cannot be downgraded")
    op.drop_constraint("sprint_dod_items_are_array", "sprints", type_="check")
    op.drop_column("sprints", "dod_items")
