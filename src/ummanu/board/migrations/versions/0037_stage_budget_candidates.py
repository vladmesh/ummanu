"""Index active-stage restart candidates under the budget query's predicate."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_stage_budget_candidates"
down_revision = "0036_card_ci_review"
branch_labels = None
depends_on = None
release_safety = "additive"

CANDIDATE_PREDICATE = (
    "(intent ->> 'ref') <> '' AND (intent ->> 'ref') NOT LIKE 'sprint:%' AND ("
    "((intent ->> 'kind') IN ('verdict', 'card.verdict') "
    "AND 'review:red' IN ((intent -> 'payload' ->> 'marker'), (intent -> 'data' ->> 'marker')))"
    " OR (intent -> 'transition' ->> 'target') = 'blocked'"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'blocked')"
    " OR ((intent -> 'transition' ->> 'target') = 'ready'"
    " AND (intent -> 'transition' ->> 'source') IN ('assessment', 'in_progress', 'validate', 'ci', 'review'))"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'ready'"
    " AND (intent -> 'payload' ->> 'from') IN ('assessment', 'in_progress', 'validate', 'ci', 'review'))"
    " OR (((intent -> 'transition' ->> 'target') = 'in_progress'"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'in_progress'))"
    " AND (intent ->> 'request_id') LIKE '%gate-red%')"
    " OR ((intent ->> 'kind') = 'created'"
    " AND ((intent -> 'payload' ->> 'budget_event') IN ('hotfix', 'recreated_task')"
    " OR (intent -> 'data' ->> 'budget_event') IN ('hotfix', 'recreated_task'))))"
)


def upgrade() -> None:
    op.drop_index("requests_budget_candidates", table_name="requests")
    op.create_index(
        "requests_budget_candidates", "requests", ["settled_at", "created_at", "request_id"],
        postgresql_where=sa.text("status = 'committed' AND " + CANDIDATE_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("requests_budget_candidates", table_name="requests")
    op.create_index(
        "requests_budget_candidates", "requests", ["settled_at", "created_at", "request_id"],
        postgresql_where=sa.text("status = 'committed' AND " + CANDIDATE_PREDICATE.replace(", 'ci', 'review'", "")),
    )
