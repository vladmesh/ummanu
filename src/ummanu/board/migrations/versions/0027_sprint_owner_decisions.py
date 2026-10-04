"""Standing quoted owner decisions, empty for existing sprints.

Preserve released production permissions, e2e counters and charges verbatim. No quotation
is inferred from those records. SQL charging consults the latest explicit e2e answer.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027_sprint_owner_decisions"
down_revision = "0026_sprint_local_runs"
branch_labels = None
depends_on = None
release_safety = "additive"


def upgrade() -> None:
    op.add_column("sprints", sa.Column("owner_decisions", JSONB(), nullable=False,
                                      server_default=sa.text("'[]'::jsonb")))
    op.create_check_constraint("sprint_owner_decisions_are_array", "sprints",
                               "jsonb_typeof(owner_decisions) = 'array'")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM sprints WHERE owner_decisions <> '[]'::jsonb LIMIT 1").first():
        raise RuntimeError("a sprint has quoted owner decisions; 0027 cannot be downgraded")
    op.drop_constraint("sprint_owner_decisions_are_array", "sprints", type_="check")
    op.drop_column("sprints", "owner_decisions")
